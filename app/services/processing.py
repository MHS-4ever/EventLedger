import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.models import (
    Event,
    EventProcessingAttempt,
    EventStatusHistory,
    OrderFulfillment,
)
from app.services.order_handler import HandlerOptions, validate_order_created
from app.services.processing_errors import (
    LeaseOwnershipLost,
    PermanentFailure,
    ProcessingStateError,
    RetryableFailure,
)
from app.services.processing_types import (
    ClaimedEvent,
    FailureResult,
    ProcessingPolicy,
    ProcessingResult,
    RecoveryResult,
    WorkerCycleResult,
)

logger = logging.getLogger(__name__)

MAX_PERSISTED_ERROR_LENGTH = 500


def sanitize_error(message: str) -> str:
    normalized = " ".join(message.split())
    if not normalized:
        return "Processing failed"
    return normalized[:MAX_PERSISTED_ERROR_LENGTH]


def effective_attempt_limit(event: Event, policy: ProcessingPolicy) -> int:
    limit = event.attempt_limit_override
    if limit is None:
        return policy.max_attempts
    if limit < 1 or limit < event.attempt_count:
        raise ProcessingStateError("Event attempt limit override is invalid")
    return limit


def _effective_attempt_limit_expression(
    policy: ProcessingPolicy,
) -> ColumnElement[int]:
    return func.coalesce(Event.attempt_limit_override, policy.max_attempts)


def _database_now(session: Session) -> datetime:
    value = session.scalar(select(func.clock_timestamp()))
    if value is None:
        raise ProcessingStateError("PostgreSQL did not return a current timestamp")
    return value


def _active_attempt(
    session: Session,
    claim: ClaimedEvent,
) -> EventProcessingAttempt:
    attempt = session.scalar(
        select(EventProcessingAttempt)
        .where(
            EventProcessingAttempt.event_id == claim.event_id,
            EventProcessingAttempt.attempt_number == claim.attempt_number,
            EventProcessingAttempt.lease_token == claim.lease_token,
            EventProcessingAttempt.finished_at.is_(None),
        )
        .with_for_update()
    )
    if attempt is None:
        raise ProcessingStateError("Active processing attempt record is missing")
    return attempt


def _owned_event(
    session: Session,
    claim: ClaimedEvent,
) -> Event:
    event = session.scalar(
        select(Event).where(Event.id == claim.event_id).with_for_update()
    )
    database_now = _database_now(session)
    if (
        event is None
        or event.status != "processing"
        or event.lease_token != claim.lease_token
        or event.lease_expires_at is None
        or event.lease_expires_at <= database_now
    ):
        raise LeaseOwnershipLost("Processing lease is no longer owned or valid")
    return event


def _ensure_lease_still_valid(
    session: Session,
    event: Event,
    claim: ClaimedEvent,
) -> datetime:
    database_now = _database_now(session)
    if (
        event.status != "processing"
        or event.lease_token != claim.lease_token
        or event.lease_expires_at is None
        or event.lease_expires_at <= database_now
    ):
        raise LeaseOwnershipLost("Processing lease expired during finalization")
    return database_now


def claim_next_event(
    session_factory: sessionmaker[Session],
    policy: ProcessingPolicy,
    *,
    source_id: str | None = None,
) -> ClaimedEvent | None:
    claim: ClaimedEvent | None = None
    with session_factory.begin() as session:
        filters = [
            Event.status == "pending",
            Event.next_attempt_at <= func.clock_timestamp(),
            Event.attempt_count < _effective_attempt_limit_expression(policy),
        ]
        if source_id is not None:
            filters.append(Event.source_id == source_id)
        event = session.scalar(
            select(Event)
            .where(*filters)
            .order_by(Event.next_attempt_at, Event.created_at, Event.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event is None:
            return None

        attempt_limit = effective_attempt_limit(event, policy)
        if event.attempt_count >= attempt_limit:
            raise ProcessingStateError("Claimed event has exhausted its attempt limit")

        database_now = _database_now(session)
        lease_token = uuid4()
        attempt_number = event.attempt_count + 1
        lease_expires_at = database_now + timedelta(
            seconds=policy.lease_duration_seconds
        )

        event.status = "processing"
        event.attempt_count = attempt_number
        event.lease_token = lease_token
        event.lease_expires_at = lease_expires_at
        event.updated_at = database_now

        session.add(
            EventProcessingAttempt(
                event_id=event.id,
                attempt_number=attempt_number,
                lease_token=lease_token,
            )
        )
        session.add(
            EventStatusHistory(
                event_id=event.id,
                status="processing",
                message=f"Processing attempt {attempt_number} claimed",
            )
        )
        claim = ClaimedEvent(
            event_id=event.id,
            source_id=event.source_id,
            event_type=event.event_type,
            payload=dict(event.payload),
            attempt_number=attempt_number,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
        )

    if claim is not None:
        logger.info(
            "event_claimed event_id=%s attempt=%s",
            claim.event_id,
            claim.attempt_number,
        )
    return claim


def recover_one_expired_event(
    session_factory: sessionmaker[Session],
    policy: ProcessingPolicy,
    *,
    source_id: str | None = None,
) -> RecoveryResult | None:
    result: RecoveryResult | None = None
    quarantined = False
    with session_factory.begin() as session:
        filters = [
            Event.status == "processing",
            Event.lease_expires_at <= func.clock_timestamp(),
        ]
        if source_id is not None:
            filters.append(Event.source_id == source_id)
        event = session.scalar(
            select(Event)
            .where(*filters)
            .order_by(Event.lease_expires_at, Event.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event is None:
            return None
        if event.lease_token is None or event.lease_expires_at is None:
            raise ProcessingStateError("Expired processing event has no lease")

        database_now = _database_now(session)
        if event.lease_expires_at > database_now:
            return None

        attempt = session.scalar(
            select(EventProcessingAttempt)
            .where(
                EventProcessingAttempt.event_id == event.id,
                EventProcessingAttempt.attempt_number == event.attempt_count,
                EventProcessingAttempt.lease_token == event.lease_token,
                EventProcessingAttempt.finished_at.is_(None),
            )
            .with_for_update()
        )
        if attempt is None:
            error = "Internal processing state is inconsistent"
            event.status = "failed"
            event.lease_token = None
            event.lease_expires_at = None
            event.last_error = error
            event.updated_at = database_now
            session.add(
                EventStatusHistory(
                    event_id=event.id,
                    status="failed",
                    message=error,
                )
            )
            result = RecoveryResult(
                event_id=event.id,
                attempt_number=event.attempt_count,
                status="failed",
            )
            quarantined = True
        else:
            error = "Processing lease expired before finalization"
            attempt.finished_at = database_now
            attempt.outcome = "lease_expired"
            attempt.error = error

            if event.attempt_count < effective_attempt_limit(event, policy):
                next_status = "pending"
                event.next_attempt_at = database_now + timedelta(
                    seconds=policy.retry_delay_seconds(event.attempt_count)
                )
            else:
                next_status = "failed"

            event.status = next_status
            event.lease_token = None
            event.lease_expires_at = None
            event.last_error = error
            event.updated_at = database_now
            session.add(
                EventStatusHistory(
                    event_id=event.id,
                    status=next_status,
                    message=error,
                )
            )
            result = RecoveryResult(
                event_id=event.id,
                attempt_number=event.attempt_count,
                status=next_status,
            )

    if result is not None:
        if quarantined:
            logger.error(
                "expired_event_quarantined event_id=%s attempt=%s "
                "reason=missing_unfinished_attempt",
                result.event_id,
                result.attempt_number,
            )
        else:
            logger.info(
                "expired_lease_recovered event_id=%s attempt=%s status=%s",
                result.event_id,
                result.attempt_number,
                result.status,
            )
    return result


def fail_one_exhausted_pending_event(
    session_factory: sessionmaker[Session],
    policy: ProcessingPolicy,
    *,
    source_id: str | None = None,
) -> UUID | None:
    event_id: UUID | None = None
    with session_factory.begin() as session:
        filters = [
            Event.status == "pending",
            Event.attempt_count >= _effective_attempt_limit_expression(policy),
        ]
        if source_id is not None:
            filters.append(Event.source_id == source_id)
        event = session.scalar(
            select(Event)
            .where(*filters)
            .order_by(Event.next_attempt_at, Event.created_at, Event.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event is None:
            return None

        effective_attempt_limit(event, policy)

        database_now = _database_now(session)
        error = "Maximum processing attempts exhausted"
        event.status = "failed"
        event.lease_token = None
        event.lease_expires_at = None
        event.last_error = error
        event.updated_at = database_now
        session.add(
            EventStatusHistory(
                event_id=event.id,
                status="failed",
                message=error,
            )
        )
        event_id = event.id

    return event_id


def finalize_order_success(
    session_factory: sessionmaker[Session],
    claim: ClaimedEvent,
    order_id: str,
) -> None:
    with session_factory.begin() as session:
        event = _owned_event(session, claim)
        attempt = _active_attempt(session, claim)

        session.execute(
            insert(OrderFulfillment)
            .values(
                source_id=event.source_id,
                order_id=order_id,
                first_event_id=event.id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    OrderFulfillment.source_id,
                    OrderFulfillment.order_id,
                ]
            )
        )
        fulfillment = session.scalar(
            select(OrderFulfillment)
            .where(
                OrderFulfillment.source_id == event.source_id,
                OrderFulfillment.order_id == order_id,
            )
            .with_for_update()
        )
        if fulfillment is None:
            raise ProcessingStateError("Order fulfillment was not persisted")

        first_event_source = session.scalar(
            select(Event.source_id).where(Event.id == fulfillment.first_event_id)
        )
        if first_event_source != event.source_id:
            raise ProcessingStateError(
                "Order fulfillment first event belongs to a different source"
            )

        database_now = _ensure_lease_still_valid(session, event, claim)
        attempt.finished_at = database_now
        attempt.outcome = "completed"
        attempt.error = None
        event.status = "completed"
        event.lease_token = None
        event.lease_expires_at = None
        event.last_error = None
        event.updated_at = database_now
        session.add(
            EventStatusHistory(
                event_id=event.id,
                status="completed",
                message="Processing completed",
            )
        )


def finalize_processing_failure(
    session_factory: sessionmaker[Session],
    claim: ClaimedEvent,
    policy: ProcessingPolicy,
    *,
    retryable: bool,
    message: str,
) -> FailureResult:
    result: FailureResult | None = None
    with session_factory.begin() as session:
        event = _owned_event(session, claim)
        attempt = _active_attempt(session, claim)
        database_now = _ensure_lease_still_valid(session, event, claim)
        error = sanitize_error(message)

        attempt.finished_at = database_now
        attempt.outcome = "retryable_failure" if retryable else "permanent_failure"
        attempt.error = error

        if retryable and event.attempt_count < effective_attempt_limit(
            event,
            policy,
        ):
            next_status = "pending"
            event.next_attempt_at = database_now + timedelta(
                seconds=policy.retry_delay_seconds(event.attempt_count)
            )
        else:
            next_status = "failed"

        event.status = next_status
        event.lease_token = None
        event.lease_expires_at = None
        event.last_error = error
        event.updated_at = database_now
        session.add(
            EventStatusHistory(
                event_id=event.id,
                status=next_status,
                message=error,
            )
        )
        result = FailureResult(
            event_id=event.id,
            attempt_number=event.attempt_count,
            status=next_status,
        )

    if result is None:
        raise ProcessingStateError("Failure finalization produced no result")
    return result


def process_claim(
    session_factory: sessionmaker[Session],
    claim: ClaimedEvent,
    policy: ProcessingPolicy,
    handler_options: HandlerOptions,
    handler: Callable[[ClaimedEvent, HandlerOptions], str] = validate_order_created,
) -> ProcessingResult:
    logger.info(
        "processing_started event_id=%s attempt=%s",
        claim.event_id,
        claim.attempt_number,
    )
    try:
        try:
            order_id = handler(claim, handler_options)
        except RetryableFailure as error:
            failure = finalize_processing_failure(
                session_factory,
                claim,
                policy,
                retryable=True,
                message=str(error),
            )
            logger.info(
                "retryable_failure_finalized event_id=%s attempt=%s status=%s",
                claim.event_id,
                claim.attempt_number,
                failure.status,
            )
            return ProcessingResult(
                claim.event_id,
                claim.attempt_number,
                failure.status,
            )
        except PermanentFailure as error:
            failure = finalize_processing_failure(
                session_factory,
                claim,
                policy,
                retryable=False,
                message=str(error),
            )
            logger.info(
                "permanent_failure_finalized event_id=%s attempt=%s",
                claim.event_id,
                claim.attempt_number,
            )
            return ProcessingResult(
                claim.event_id,
                claim.attempt_number,
                failure.status,
            )
        except Exception as error:
            logger.error(
                "unexpected_handler_error event_id=%s attempt=%s error_type=%s",
                claim.event_id,
                claim.attempt_number,
                type(error).__name__,
            )
            failure = finalize_processing_failure(
                session_factory,
                claim,
                policy,
                retryable=False,
                message="Unexpected processing error",
            )
            return ProcessingResult(
                claim.event_id,
                claim.attempt_number,
                failure.status,
            )

        finalize_order_success(session_factory, claim, order_id)
    except LeaseOwnershipLost:
        logger.warning(
            "processing_ownership_lost event_id=%s attempt=%s",
            claim.event_id,
            claim.attempt_number,
        )
        return ProcessingResult(
            claim.event_id,
            claim.attempt_number,
            "ownership_lost",
        )
    logger.info(
        "processing_completed event_id=%s attempt=%s",
        claim.event_id,
        claim.attempt_number,
    )
    return ProcessingResult(claim.event_id, claim.attempt_number, "completed")


def run_worker_cycle(
    session_factory: sessionmaker[Session],
    policy: ProcessingPolicy,
    handler_options: HandlerOptions,
    *,
    source_id: str | None = None,
) -> WorkerCycleResult:
    recovered = recover_one_expired_event(
        session_factory,
        policy,
        source_id=source_id,
    )
    exhausted_event_id = fail_one_exhausted_pending_event(
        session_factory,
        policy,
        source_id=source_id,
    )
    claim = claim_next_event(session_factory, policy, source_id=source_id)
    processing = (
        process_claim(session_factory, claim, policy, handler_options)
        if claim is not None
        else None
    )
    return WorkerCycleResult(recovered, exhausted_event_id, processing)
