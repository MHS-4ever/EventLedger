import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    Event,
    EventManualRetryRequest,
    EventProcessingAttempt,
    EventStatusHistory,
)
from app.schemas.event import ProcessingAttemptErrorSummary

ATTEMPT_ERROR_SUMMARIES: dict[str, ProcessingAttemptErrorSummary] = {
    "retryable_failure": "Retryable processing failure",
    "permanent_failure": "Permanent processing failure",
    "lease_expired": "Processing lease expired",
}

ManualRetryConflictCode = Literal[
    "event_state_conflict",
    "manual_retry_not_allowed",
    "manual_retry_limit_reached",
]


@dataclass(frozen=True, slots=True)
class ManualRetryAuthorization:
    event_id: UUID
    retry_request_id: UUID
    target_attempt_number: int
    accepted_at: datetime
    replayed: bool


class ManualRetryEventNotFound(Exception):
    """The source-scoped event does not exist."""


class ManualRetryConflict(Exception):
    def __init__(self, code: ManualRetryConflictCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def compute_request_hash(
    event_type: str,
    payload: dict[str, JsonValue],
) -> str:
    canonical_request = json.dumps(
        {"event_type": event_type, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()


def public_attempt_error_summary(
    outcome: str | None,
) -> ProcessingAttemptErrorSummary | None:
    return ATTEMPT_ERROR_SUMMARIES.get(outcome)


def _retry_authorization_from_record(
    request: EventManualRetryRequest,
    *,
    replayed: bool,
) -> ManualRetryAuthorization:
    return ManualRetryAuthorization(
        event_id=request.event_id,
        retry_request_id=request.id,
        target_attempt_number=request.target_attempt_number,
        accepted_at=request.accepted_at,
        replayed=replayed,
    )


def authorize_manual_retry(
    session: Session,
    *,
    event_id: UUID,
    source_id: str,
    idempotency_key: str,
) -> ManualRetryAuthorization:
    event = session.scalar(
        select(Event)
        .where(
            Event.id == event_id,
            Event.source_id == source_id,
        )
        .with_for_update()
    )
    if event is None:
        raise ManualRetryEventNotFound

    existing_request = session.scalar(
        select(EventManualRetryRequest).where(
            EventManualRetryRequest.event_id == event.id
        )
    )
    if existing_request is not None:
        if existing_request.idempotency_key == idempotency_key:
            return _retry_authorization_from_record(
                existing_request,
                replayed=True,
            )
        raise ManualRetryConflict(
            "manual_retry_limit_reached",
            "A manual retry has already been granted for this event",
        )

    if event.status != "failed":
        raise ManualRetryConflict(
            "event_state_conflict",
            "Only failed events can be manually retried",
        )
    if event.lease_token is not None or event.lease_expires_at is not None:
        raise ManualRetryConflict(
            "manual_retry_not_allowed",
            "Event processing state is not eligible for manual retry",
        )
    if event.attempt_limit_override is not None:
        raise ManualRetryConflict(
            "manual_retry_not_allowed",
            "Event retry authorization state is inconsistent",
        )
    if event.attempt_count <= 0:
        raise ManualRetryConflict(
            "manual_retry_not_allowed",
            "Event has no completed processing attempt to retry",
        )

    attempt_count, first_attempt_number, latest_attempt_number = session.execute(
        select(
            func.count(EventProcessingAttempt.id),
            func.min(EventProcessingAttempt.attempt_number),
            func.max(EventProcessingAttempt.attempt_number),
        ).where(EventProcessingAttempt.event_id == event.id)
    ).one()
    if (
        attempt_count != event.attempt_count
        or first_attempt_number != 1
        or latest_attempt_number != event.attempt_count
    ):
        raise ManualRetryConflict(
            "manual_retry_not_allowed",
            "Event attempt history is inconsistent",
        )

    latest_attempt = session.scalar(
        select(EventProcessingAttempt)
        .where(
            EventProcessingAttempt.event_id == event.id,
            EventProcessingAttempt.attempt_number == event.attempt_count,
        )
        .with_for_update()
    )
    if (
        latest_attempt is None
        or latest_attempt.finished_at is None
        or latest_attempt.outcome not in {"retryable_failure", "lease_expired"}
    ):
        raise ManualRetryConflict(
            "manual_retry_not_allowed",
            "The terminal processing failure is not eligible for manual retry",
        )

    database_now = session.scalar(select(func.clock_timestamp()))
    if database_now is None:
        raise SQLAlchemyError("PostgreSQL did not return a current timestamp")

    target_attempt_number = event.attempt_count + 1
    request = EventManualRetryRequest(
        id=uuid4(),
        event_id=event.id,
        idempotency_key=idempotency_key,
        target_attempt_number=target_attempt_number,
        accepted_at=database_now,
    )
    event.attempt_limit_override = target_attempt_number
    event.status = "pending"
    event.next_attempt_at = database_now
    event.lease_token = None
    event.lease_expires_at = None
    event.updated_at = database_now
    session.add(request)
    session.add(
        EventStatusHistory(
            event_id=event.id,
            status="pending",
            message=(
                "Manual retry accepted for processing attempt "
                f"{target_attempt_number}"
            ),
            created_at=database_now,
        )
    )
    return _retry_authorization_from_record(request, replayed=False)

