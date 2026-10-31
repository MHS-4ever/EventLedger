import os
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Event,
    EventProcessingAttempt,
    EventStatusHistory,
    OrderFulfillment,
)
from app.services.order_handler import HandlerOptions
from app.services.processing import (
    claim_next_event,
    finalize_order_success,
    process_claim,
    recover_one_expired_event,
    run_worker_cycle,
)
from app.services.processing_errors import (
    LeaseOwnershipLost,
    ProcessingStateError,
    RetryableFailure,
)
from app.services.processing_types import ClaimedEvent, ProcessingPolicy
from scripts.prepare_test_db import (
    create_test_database_engine,
    load_test_database_settings,
    validate_test_database_settings,
    verify_test_database_connection,
)

RUN_PG_INTEGRATION = os.getenv("EVENTLEDGER_RUN_PG_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.pg_integration,
    pytest.mark.skipif(
        not RUN_PG_INTEGRATION,
        reason="Set EVENTLEDGER_RUN_PG_INTEGRATION=1 to run PostgreSQL tests.",
    ),
]

DEFAULT_POLICY = ProcessingPolicy(
    lease_duration_seconds=30,
    max_attempts=3,
    retry_base_seconds=2,
    retry_max_seconds=8,
)
DEFAULT_HANDLER_OPTIONS = HandlerOptions()


@dataclass(frozen=True)
class WorkerIntegrationContext:
    engine: Engine
    session_factory: sessionmaker[Session]
    source_prefix: str

    @property
    def source_id(self) -> str:
        return f"{self.source_prefix}-primary"

    def source(self, suffix: str) -> str:
        return f"{self.source_prefix}-{suffix}"


@pytest.fixture(scope="module")
def pg_engine() -> Generator[Engine, None, None]:
    try:
        test_settings = load_test_database_settings()
        validate_test_database_settings(test_settings)
    except (ValidationError, RuntimeError):
        pytest.fail(
            "Unsafe or invalid .env.test configuration; refusing PostgreSQL tests."
        )

    engine = create_test_database_engine(test_settings)
    try:
        try:
            with engine.connect() as connection:
                verify_test_database_connection(connection)
                revision = connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
                if revision != "b87f4d92c1a6":
                    pytest.fail(
                        "Isolated PostgreSQL test database is not migrated to "
                        "worker schema revision b87f4d92c1a6."
                    )
        except (RuntimeError, SQLAlchemyError):
            pytest.fail(
                "Unable to verify the isolated PostgreSQL test database."
            )
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def worker_context(
    pg_engine: Engine,
) -> Generator[WorkerIntegrationContext, None, None]:
    source_prefix = f"worker-pg-{uuid4().hex}"
    factory = sessionmaker(
        bind=pg_engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )
    context = WorkerIntegrationContext(pg_engine, factory, source_prefix)
    yield context

    with pg_engine.begin() as connection:
        verify_test_database_connection(connection)
        event_ids = select(Event.id).where(
            Event.source_id.like(f"{source_prefix}%")
        )
        connection.execute(
            delete(OrderFulfillment).where(
                OrderFulfillment.first_event_id.in_(event_ids)
            )
        )
        connection.execute(
            delete(EventProcessingAttempt).where(
                EventProcessingAttempt.event_id.in_(event_ids)
            )
        )
        connection.execute(
            delete(EventStatusHistory).where(
                EventStatusHistory.event_id.in_(event_ids)
            )
        )
        connection.execute(
            delete(Event).where(Event.source_id.like(f"{source_prefix}%"))
        )


def database_now(context: WorkerIntegrationContext) -> datetime:
    with context.session_factory() as session:
        value = session.scalar(select(func.clock_timestamp()))
        assert value is not None
        return value


def create_event(
    context: WorkerIntegrationContext,
    *,
    source_id: str | None = None,
    event_type: str = "order.created",
    payload: dict | None = None,
    status: str = "pending",
    attempt_count: int = 0,
    next_attempt_at: datetime | None = None,
) -> UUID:
    event_id = uuid4()
    source = source_id or context.source_id
    event_payload = (
        payload if payload is not None else {"order_id": f"ORD-{uuid4().hex}"}
    )
    with context.session_factory.begin() as session:
        event = Event(
            id=event_id,
            source_id=source,
            idempotency_key=f"worker-{uuid4().hex}",
            event_type=event_type,
            payload=event_payload,
            request_hash="0" * 64,
            status=status,
            attempt_count=attempt_count,
        )
        if next_attempt_at is not None:
            event.next_attempt_at = next_attempt_at
        session.add(event)
        session.add(EventStatusHistory(event_id=event_id, status=status))
    return event_id


def load_event(context: WorkerIntegrationContext, event_id: UUID) -> Event:
    with context.session_factory() as session:
        event = session.get(Event, event_id)
        assert event is not None
        session.expunge(event)
        return event


def attempts_for(
    context: WorkerIntegrationContext,
    event_id: UUID,
) -> list[EventProcessingAttempt]:
    with context.session_factory() as session:
        return list(
            session.scalars(
                select(EventProcessingAttempt)
                .where(EventProcessingAttempt.event_id == event_id)
                .order_by(EventProcessingAttempt.attempt_number)
            )
        )


def history_statuses(
    context: WorkerIntegrationContext,
    event_id: UUID,
) -> list[str]:
    with context.session_factory() as session:
        return list(
            session.scalars(
                select(EventStatusHistory.status)
                .where(EventStatusHistory.event_id == event_id)
                .order_by(
                    EventStatusHistory.created_at,
                    EventStatusHistory.id,
                )
            )
        )


def make_due(context: WorkerIntegrationContext, event_id: UUID) -> None:
    with context.session_factory.begin() as session:
        session.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(next_attempt_at=func.clock_timestamp())
        )


def expire_lease(context: WorkerIntegrationContext, event_id: UUID) -> None:
    with context.session_factory.begin() as session:
        session.execute(
            update(Event)
            .where(Event.id == event_id, Event.status == "processing")
            .values(
                lease_expires_at=(
                    func.clock_timestamp() - timedelta(seconds=1)
                )
            )
        )


def test_empty_scoped_queue_has_no_writes(
    worker_context: WorkerIntegrationContext,
) -> None:
    result = run_worker_cycle(
        worker_context.session_factory,
        DEFAULT_POLICY,
        DEFAULT_HANDLER_OPTIONS,
        source_id=worker_context.source_id,
    )

    assert result.recovered is None
    assert result.exhausted_event_id is None
    assert result.processing is None


def test_claim_eligibility_and_atomic_records(
    worker_context: WorkerIntegrationContext,
) -> None:
    now = database_now(worker_context)
    eligible_id = create_event(worker_context, next_attempt_at=now)
    create_event(worker_context, status="completed", next_attempt_at=now)
    create_event(worker_context, status="failed", next_attempt_at=now)
    create_event(
        worker_context,
        next_attempt_at=now + timedelta(hours=1),
    )
    create_event(
        worker_context,
        attempt_count=DEFAULT_POLICY.max_attempts,
        next_attempt_at=now,
    )

    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )

    assert claim is not None
    assert claim.event_id == eligible_id
    assert claim.attempt_number == 1
    assert claim.lease_expires_at > now
    event = load_event(worker_context, eligible_id)
    assert event.status == "processing"
    assert event.attempt_count == 1
    assert event.lease_token == claim.lease_token
    attempts = attempts_for(worker_context, eligible_id)
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].lease_token == claim.lease_token
    assert history_statuses(worker_context, eligible_id) == ["pending", "processing"]


def test_claim_rolls_back_event_attempt_and_history_together(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(worker_context)

    class FailingHistorySession(Session):
        def add(self, instance, *args, **kwargs) -> None:
            if (
                isinstance(instance, EventStatusHistory)
                and instance.status == "processing"
            ):
                raise SQLAlchemyError("simulated processing-history failure")
            super().add(instance, *args, **kwargs)

    failing_factory = sessionmaker(
        bind=worker_context.engine,
        class_=FailingHistorySession,
        autoflush=False,
        expire_on_commit=False,
    )

    with pytest.raises(SQLAlchemyError):
        claim_next_event(
            failing_factory,
            DEFAULT_POLICY,
            source_id=worker_context.source_id,
        )

    event = load_event(worker_context, event_id)
    assert event.status == "pending"
    assert event.attempt_count == 0
    assert event.lease_token is None
    assert attempts_for(worker_context, event_id) == []
    assert history_statuses(worker_context, event_id) == ["pending"]


def test_two_workers_compete_for_one_event(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(worker_context)
    barrier = Barrier(2)

    def compete() -> ClaimedEvent | None:
        barrier.wait(timeout=10)
        return claim_next_event(
            worker_context.session_factory,
            DEFAULT_POLICY,
            source_id=worker_context.source_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: compete(), range(2)))

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].event_id == event_id
    assert len(attempts_for(worker_context, event_id)) == 1


def test_workers_claim_different_events(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_ids = {create_event(worker_context), create_event(worker_context)}
    barrier = Barrier(2)

    def compete() -> ClaimedEvent | None:
        barrier.wait(timeout=10)
        return claim_next_event(
            worker_context.session_factory,
            DEFAULT_POLICY,
            source_id=worker_context.source_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: compete(), range(2)))

    assert all(claim is not None for claim in claims)
    assert {claim.event_id for claim in claims if claim is not None} == event_ids


def test_skip_locked_skips_a_row_locked_by_another_transaction(
    worker_context: WorkerIntegrationContext,
) -> None:
    now = database_now(worker_context)
    locked_event_id = create_event(
        worker_context,
        next_attempt_at=now - timedelta(seconds=2),
    )
    available_event_id = create_event(
        worker_context,
        next_attempt_at=now - timedelta(seconds=1),
    )

    with worker_context.session_factory() as locking_session:
        with locking_session.begin():
            locking_session.scalar(
                select(Event)
                .where(Event.id == locked_event_id)
                .with_for_update()
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    claim_next_event,
                    worker_context.session_factory,
                    DEFAULT_POLICY,
                    source_id=worker_context.source_id,
                )
                claim = future.result(timeout=10)

    assert claim is not None
    assert claim.event_id == available_event_id


def test_exhausted_pending_event_is_terminally_failed(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(
        worker_context,
        attempt_count=DEFAULT_POLICY.max_attempts,
    )

    result = run_worker_cycle(
        worker_context.session_factory,
        DEFAULT_POLICY,
        DEFAULT_HANDLER_OPTIONS,
        source_id=worker_context.source_id,
    )

    assert result.exhausted_event_id == event_id
    assert result.processing is None
    event = load_event(worker_context, event_id)
    assert event.status == "failed"
    assert event.attempt_count == DEFAULT_POLICY.max_attempts
    assert history_statuses(worker_context, event_id) == ["pending", "failed"]


def test_successful_order_processing_is_atomic_and_idempotent(
    worker_context: WorkerIntegrationContext,
) -> None:
    order_id = f"ORD-{uuid4().hex}"
    first_event_id = create_event(
        worker_context,
        payload={"order_id": order_id},
    )
    first_claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert first_claim is not None
    first_result = process_claim(
        worker_context.session_factory,
        first_claim,
        DEFAULT_POLICY,
        DEFAULT_HANDLER_OPTIONS,
    )

    assert first_result.status == "completed"
    first_event = load_event(worker_context, first_event_id)
    assert first_event.status == "completed"
    assert first_event.lease_token is None
    assert first_event.last_error is None
    assert attempts_for(worker_context, first_event_id)[0].outcome == "completed"
    assert history_statuses(worker_context, first_event_id) == [
        "pending",
        "processing",
        "completed",
    ]

    second_event_id = create_event(
        worker_context,
        payload={"order_id": order_id},
    )
    second_claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert second_claim is not None
    second_result = process_claim(
        worker_context.session_factory,
        second_claim,
        DEFAULT_POLICY,
        DEFAULT_HANDLER_OPTIONS,
    )
    assert second_result.status == "completed"
    assert load_event(worker_context, second_event_id).status == "completed"

    with worker_context.session_factory() as session:
        fulfillments = list(
            session.scalars(
                select(OrderFulfillment).where(
                    OrderFulfillment.source_id == worker_context.source_id,
                    OrderFulfillment.order_id == order_id,
                )
            )
        )
    assert len(fulfillments) == 1
    assert fulfillments[0].first_event_id == first_event_id


def test_same_order_id_is_independent_across_sources(
    worker_context: WorkerIntegrationContext,
) -> None:
    order_id = f"ORD-{uuid4().hex}"
    sources = [worker_context.source("a"), worker_context.source("b")]
    for source_id in sources:
        create_event(
            worker_context,
            source_id=source_id,
            payload={"order_id": order_id},
        )
        claim = claim_next_event(
            worker_context.session_factory,
            DEFAULT_POLICY,
            source_id=source_id,
        )
        assert claim is not None
        assert process_claim(
            worker_context.session_factory,
            claim,
            DEFAULT_POLICY,
            DEFAULT_HANDLER_OPTIONS,
        ).status == "completed"

    with worker_context.session_factory() as session:
        fulfillment_sources = set(
            session.scalars(
                select(OrderFulfillment.source_id).where(
                    OrderFulfillment.order_id == order_id,
                    OrderFulfillment.source_id.in_(sources),
                )
            )
        )
    assert fulfillment_sources == set(sources)


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("order.created", {"order_id": " invalid"}),
        ("payment.received", {"order_id": "ORD-1"}),
    ],
)
def test_business_contract_errors_are_permanent(
    worker_context: WorkerIntegrationContext,
    event_type: str,
    payload: dict,
) -> None:
    event_id = create_event(
        worker_context,
        event_type=event_type,
        payload=payload,
    )
    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert claim is not None

    result = process_claim(
        worker_context.session_factory,
        claim,
        DEFAULT_POLICY,
        DEFAULT_HANDLER_OPTIONS,
    )

    assert result.status == "failed"
    event = load_event(worker_context, event_id)
    assert event.status == "failed"
    assert attempts_for(worker_context, event_id)[0].outcome == "permanent_failure"
    assert history_statuses(worker_context, event_id)[-1] == "failed"


def test_retryable_failure_schedules_then_successfully_retries(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(
        worker_context,
        payload={
            "order_id": f"ORD-{uuid4().hex}",
            "_demo_failure": "transient_once",
        },
    )
    options = HandlerOptions(failure_simulation_enabled=True)
    first_claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert first_claim is not None
    first_result = process_claim(
        worker_context.session_factory,
        first_claim,
        DEFAULT_POLICY,
        options,
    )

    assert first_result.status == "pending"
    event = load_event(worker_context, event_id)
    first_attempt = attempts_for(worker_context, event_id)[0]
    assert first_attempt.outcome == "retryable_failure"
    assert first_attempt.finished_at is not None
    assert event.next_attempt_at - first_attempt.finished_at == timedelta(seconds=2)
    assert event.last_error == "Simulated transient processing failure"
    assert (
        claim_next_event(
            worker_context.session_factory,
            DEFAULT_POLICY,
            source_id=worker_context.source_id,
        )
        is None
    )

    make_due(worker_context, event_id)
    second_claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert second_claim is not None
    assert second_claim.attempt_number == 2
    assert second_claim.lease_token != first_claim.lease_token
    second_result = process_claim(
        worker_context.session_factory,
        second_claim,
        DEFAULT_POLICY,
        options,
    )

    assert second_result.status == "completed"
    completed = load_event(worker_context, event_id)
    assert completed.attempt_count == 2
    assert completed.last_error is None
    assert history_statuses(worker_context, event_id) == [
        "pending",
        "processing",
        "pending",
        "processing",
        "completed",
    ]


def test_retry_exhaustion_becomes_terminal_failure(
    worker_context: WorkerIntegrationContext,
) -> None:
    policy = ProcessingPolicy(30, 2, 2, 8)
    options = HandlerOptions(failure_simulation_enabled=True)
    event_id = create_event(
        worker_context,
        payload={
            "order_id": f"ORD-{uuid4().hex}",
            "_demo_failure": "transient_always",
        },
    )

    first_claim = claim_next_event(
        worker_context.session_factory,
        policy,
        source_id=worker_context.source_id,
    )
    assert first_claim is not None
    assert process_claim(
        worker_context.session_factory,
        first_claim,
        policy,
        options,
    ).status == "pending"
    make_due(worker_context, event_id)

    second_claim = claim_next_event(
        worker_context.session_factory,
        policy,
        source_id=worker_context.source_id,
    )
    assert second_claim is not None
    assert process_claim(
        worker_context.session_factory,
        second_claim,
        policy,
        options,
    ).status == "failed"

    event = load_event(worker_context, event_id)
    assert event.status == "failed"
    assert event.attempt_count == 2
    assert [attempt.outcome for attempt in attempts_for(worker_context, event_id)] == [
        "retryable_failure",
        "retryable_failure",
    ]


def test_expired_claim_recovery_and_repeated_scan_are_safe(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(worker_context)
    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert claim is not None
    expire_lease(worker_context, event_id)

    recovery = recover_one_expired_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )

    assert recovery is not None
    assert recovery.event_id == event_id
    assert recovery.status == "pending"
    event = load_event(worker_context, event_id)
    assert event.status == "pending"
    assert event.attempt_count == 1
    assert event.lease_token is None
    attempt = attempts_for(worker_context, event_id)[0]
    assert attempt.outcome == "lease_expired"
    assert (
        recover_one_expired_event(
            worker_context.session_factory,
            DEFAULT_POLICY,
            source_id=worker_context.source_id,
        )
        is None
    )
    assert history_statuses(worker_context, event_id) == [
        "pending",
        "processing",
        "pending",
    ]


def test_missing_attempt_is_quarantined_without_starving_healthy_work(
    worker_context: WorkerIntegrationContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    malformed_order_id = f"ORD-MALFORMED-{uuid4().hex}"
    healthy_order_id = f"ORD-HEALTHY-{uuid4().hex}"
    malformed_event_id = create_event(
        worker_context,
        payload={"order_id": malformed_order_id},
    )
    healthy_event_id = create_event(
        worker_context,
        payload={"order_id": healthy_order_id},
    )
    lease_token = uuid4()
    with worker_context.session_factory.begin() as session:
        session.execute(
            update(Event)
            .where(Event.id == malformed_event_id)
            .values(
                status="processing",
                attempt_count=1,
                lease_token=lease_token,
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
        )

    caplog.set_level("ERROR", logger="app.services.processing")
    result = run_worker_cycle(
        worker_context.session_factory,
        DEFAULT_POLICY,
        DEFAULT_HANDLER_OPTIONS,
        source_id=worker_context.source_id,
    )

    assert result.recovered is not None
    assert result.recovered.event_id == malformed_event_id
    assert result.recovered.status == "failed"
    assert result.processing is not None
    assert result.processing.event_id == healthy_event_id
    assert result.processing.status == "completed"
    assert load_event(worker_context, healthy_event_id).status == "completed"

    malformed_event = load_event(worker_context, malformed_event_id)
    assert malformed_event.status == "failed"
    assert malformed_event.lease_token is None
    assert malformed_event.lease_expires_at is None
    assert malformed_event.last_error == "Internal processing state is inconsistent"
    assert history_statuses(worker_context, malformed_event_id) == [
        "pending",
        "failed",
    ]
    assert attempts_for(worker_context, malformed_event_id) == []

    with worker_context.session_factory() as session:
        malformed_fulfillments = session.scalar(
            select(func.count())
            .select_from(OrderFulfillment)
            .where(OrderFulfillment.first_event_id == malformed_event_id)
        )
        healthy_fulfillments = session.scalar(
            select(func.count())
            .select_from(OrderFulfillment)
            .where(OrderFulfillment.first_event_id == healthy_event_id)
        )
    assert malformed_fulfillments == 0
    assert healthy_fulfillments == 1
    assert any(
        "expired_event_quarantined" in record.getMessage()
        and str(malformed_event_id) in record.getMessage()
        for record in caplog.records
    )

    repeated = run_worker_cycle(
        worker_context.session_factory,
        DEFAULT_POLICY,
        DEFAULT_HANDLER_OPTIONS,
        source_id=worker_context.source_id,
    )
    assert repeated.recovered is None
    assert repeated.exhausted_event_id is None
    assert repeated.processing is None
    assert load_event(worker_context, malformed_event_id).status == "failed"
    assert attempts_for(worker_context, malformed_event_id) == []


def test_finished_matching_attempt_is_quarantined_without_rewriting_attempt(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(worker_context)
    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert claim is not None
    with worker_context.session_factory.begin() as session:
        session.execute(
            update(EventProcessingAttempt)
            .where(
                EventProcessingAttempt.event_id == event_id,
                EventProcessingAttempt.lease_token == claim.lease_token,
            )
            .values(
                finished_at=func.clock_timestamp(),
                outcome="lease_expired",
                error="Previously closed attempt",
            )
        )
        session.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1)
            )
        )

    recovery = recover_one_expired_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )

    assert recovery is not None
    assert recovery.event_id == event_id
    assert recovery.status == "failed"
    event = load_event(worker_context, event_id)
    assert event.status == "failed"
    assert event.lease_token is None
    assert event.lease_expires_at is None
    attempts = attempts_for(worker_context, event_id)
    assert len(attempts) == 1
    assert attempts[0].outcome == "lease_expired"
    assert attempts[0].error == "Previously closed attempt"
    assert history_statuses(worker_context, event_id) == [
        "pending",
        "processing",
        "failed",
    ]


def test_expired_final_attempt_is_failed(
    worker_context: WorkerIntegrationContext,
) -> None:
    policy = ProcessingPolicy(30, 1, 2, 8)
    event_id = create_event(worker_context)
    claim = claim_next_event(
        worker_context.session_factory,
        policy,
        source_id=worker_context.source_id,
    )
    assert claim is not None
    expire_lease(worker_context, event_id)

    recovery = recover_one_expired_event(
        worker_context.session_factory,
        policy,
        source_id=worker_context.source_id,
    )

    assert recovery is not None
    assert recovery.status == "failed"
    event = load_event(worker_context, event_id)
    assert event.status == "failed"
    assert event.attempt_count == 1
    assert attempts_for(worker_context, event_id)[0].outcome == "lease_expired"


def test_only_one_worker_recovers_an_expired_claim(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(worker_context)
    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert claim is not None
    expire_lease(worker_context, event_id)
    barrier = Barrier(2)

    def compete():
        barrier.wait(timeout=10)
        return recover_one_expired_event(
            worker_context.session_factory,
            DEFAULT_POLICY,
            source_id=worker_context.source_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: compete(), range(2)))

    assert len([result for result in results if result is not None]) == 1
    assert attempts_for(worker_context, event_id)[0].outcome == "lease_expired"


def test_stale_worker_cannot_finalize_or_write_fulfillment(
    worker_context: WorkerIntegrationContext,
) -> None:
    order_id = f"ORD-{uuid4().hex}"
    event_id = create_event(worker_context, payload={"order_id": order_id})
    stale_claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert stale_claim is not None
    expire_lease(worker_context, event_id)
    recovery = recover_one_expired_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert recovery is not None
    make_due(worker_context, event_id)
    current_claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert current_claim is not None

    with pytest.raises(LeaseOwnershipLost):
        finalize_order_success(
            worker_context.session_factory,
            stale_claim,
            order_id,
        )

    event = load_event(worker_context, event_id)
    assert event.status == "processing"
    assert event.lease_token == current_claim.lease_token
    with worker_context.session_factory() as session:
        fulfillment_count = session.scalar(
            select(func.count())
            .select_from(OrderFulfillment)
            .where(
                OrderFulfillment.source_id == worker_context.source_id,
                OrderFulfillment.order_id == order_id,
            )
        )
    assert fulfillment_count == 0


def test_wrong_or_expired_token_cannot_finalize(
    worker_context: WorkerIntegrationContext,
) -> None:
    order_id = f"ORD-{uuid4().hex}"
    event_id = create_event(worker_context, payload={"order_id": order_id})
    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert claim is not None

    wrong_token_claim = replace(claim, lease_token=uuid4())
    with pytest.raises(LeaseOwnershipLost):
        finalize_order_success(
            worker_context.session_factory,
            wrong_token_claim,
            order_id,
        )

    expire_lease(worker_context, event_id)
    with pytest.raises(LeaseOwnershipLost):
        finalize_order_success(
            worker_context.session_factory,
            claim,
            order_id,
        )

    assert load_event(worker_context, event_id).status == "processing"
    with worker_context.session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OrderFulfillment)
                .where(OrderFulfillment.first_event_id == event_id)
            )
            == 0
        )


def test_nonexpired_and_completed_events_are_not_recovered(
    worker_context: WorkerIntegrationContext,
) -> None:
    processing_id = create_event(worker_context)
    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert claim is not None
    completed_id = create_event(worker_context, status="completed")

    assert (
        recover_one_expired_event(
            worker_context.session_factory,
            DEFAULT_POLICY,
            source_id=worker_context.source_id,
        )
        is None
    )
    assert load_event(worker_context, processing_id).status == "processing"
    assert load_event(worker_context, completed_id).status == "completed"


def test_fulfillment_rejects_cross_source_first_event(
    worker_context: WorkerIntegrationContext,
) -> None:
    order_id = f"ORD-{uuid4().hex}"
    other_source = worker_context.source("other")
    other_event_id = create_event(worker_context, source_id=other_source)
    event_id = create_event(worker_context, payload={"order_id": order_id})
    with worker_context.session_factory.begin() as session:
        session.add(
            OrderFulfillment(
                source_id=worker_context.source_id,
                order_id=order_id,
                first_event_id=other_event_id,
            )
        )
    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert claim is not None

    with pytest.raises(ProcessingStateError, match="different source"):
        finalize_order_success(
            worker_context.session_factory,
            claim,
            order_id,
        )

    assert load_event(worker_context, event_id).status == "processing"


def test_success_finalization_rolls_back_fulfillment_on_history_error(
    worker_context: WorkerIntegrationContext,
) -> None:
    order_id = f"ORD-{uuid4().hex}"
    event_id = create_event(worker_context, payload={"order_id": order_id})
    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert claim is not None

    class FailingCompletionHistorySession(Session):
        def add(self, instance, *args, **kwargs) -> None:
            if (
                isinstance(instance, EventStatusHistory)
                and instance.status == "completed"
            ):
                raise SQLAlchemyError("simulated completion-history failure")
            super().add(instance, *args, **kwargs)

    failing_factory = sessionmaker(
        bind=worker_context.engine,
        class_=FailingCompletionHistorySession,
        autoflush=False,
        expire_on_commit=False,
    )

    with pytest.raises(SQLAlchemyError):
        finalize_order_success(failing_factory, claim, order_id)

    event = load_event(worker_context, event_id)
    assert event.status == "processing"
    assert event.lease_token == claim.lease_token
    assert attempts_for(worker_context, event_id)[0].finished_at is None
    with worker_context.session_factory() as session:
        fulfillment_count = session.scalar(
            select(func.count())
            .select_from(OrderFulfillment)
            .where(OrderFulfillment.first_event_id == event_id)
        )
    assert fulfillment_count == 0


def test_recovery_rolls_back_attempt_and_event_on_history_error(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(worker_context)
    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert claim is not None
    expire_lease(worker_context, event_id)

    class FailingRecoveryHistorySession(Session):
        def add(self, instance, *args, **kwargs) -> None:
            if (
                isinstance(instance, EventStatusHistory)
                and instance.status == "pending"
            ):
                raise SQLAlchemyError("simulated recovery-history failure")
            super().add(instance, *args, **kwargs)

    failing_factory = sessionmaker(
        bind=worker_context.engine,
        class_=FailingRecoveryHistorySession,
        autoflush=False,
        expire_on_commit=False,
    )

    with pytest.raises(SQLAlchemyError):
        recover_one_expired_event(
            failing_factory,
            DEFAULT_POLICY,
            source_id=worker_context.source_id,
        )

    event = load_event(worker_context, event_id)
    assert event.status == "processing"
    assert event.lease_token == claim.lease_token
    assert attempts_for(worker_context, event_id)[0].finished_at is None


def test_malformed_recovery_rolls_back_quarantine_on_history_error(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(worker_context)
    lease_token = uuid4()
    with worker_context.session_factory.begin() as session:
        session.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(
                status="processing",
                attempt_count=1,
                lease_token=lease_token,
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
        )

    class FailingQuarantineHistorySession(Session):
        def add(self, instance, *args, **kwargs) -> None:
            if (
                isinstance(instance, EventStatusHistory)
                and instance.status == "failed"
            ):
                raise SQLAlchemyError("simulated quarantine-history failure")
            super().add(instance, *args, **kwargs)

    failing_factory = sessionmaker(
        bind=worker_context.engine,
        class_=FailingQuarantineHistorySession,
        autoflush=False,
        expire_on_commit=False,
    )

    with pytest.raises(SQLAlchemyError):
        recover_one_expired_event(
            failing_factory,
            DEFAULT_POLICY,
            source_id=worker_context.source_id,
        )

    event = load_event(worker_context, event_id)
    assert event.status == "processing"
    assert event.lease_token == lease_token
    assert event.lease_expires_at is not None
    assert event.last_error is None
    assert history_statuses(worker_context, event_id) == ["pending"]
    assert attempts_for(worker_context, event_id) == []


def test_failure_finalization_rolls_back_on_history_error(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(worker_context)
    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert claim is not None

    class FailingFailureHistorySession(Session):
        def add(self, instance, *args, **kwargs) -> None:
            if (
                isinstance(instance, EventStatusHistory)
                and instance.status == "pending"
            ):
                raise SQLAlchemyError("simulated failure-history error")
            super().add(instance, *args, **kwargs)

    failing_factory = sessionmaker(
        bind=worker_context.engine,
        class_=FailingFailureHistorySession,
        autoflush=False,
        expire_on_commit=False,
    )

    def transient_handler(claim, options):
        del claim, options
        raise RetryableFailure("Controlled transient failure")

    with pytest.raises(SQLAlchemyError):
        process_claim(
            failing_factory,
            claim,
            DEFAULT_POLICY,
            DEFAULT_HANDLER_OPTIONS,
            handler=transient_handler,
        )

    event = load_event(worker_context, event_id)
    assert event.status == "processing"
    assert event.lease_token == claim.lease_token
    attempt = attempts_for(worker_context, event_id)[0]
    assert attempt.finished_at is None
    assert attempt.outcome is None


def test_unexpected_handler_error_is_sanitized_and_permanent(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(worker_context)
    claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert claim is not None

    def unsafe_handler(claim, options):
        del claim, options
        raise RuntimeError("password=do-not-persist")

    result = process_claim(
        worker_context.session_factory,
        claim,
        DEFAULT_POLICY,
        DEFAULT_HANDLER_OPTIONS,
        handler=unsafe_handler,
    )

    assert result.status == "failed"
    event = load_event(worker_context, event_id)
    attempt = attempts_for(worker_context, event_id)[0]
    assert event.last_error == "Unexpected processing error"
    assert attempt.error == "Unexpected processing error"
    assert "do-not-persist" not in event.last_error


def test_worker_cycle_continues_with_later_event_after_permanent_failure(
    worker_context: WorkerIntegrationContext,
) -> None:
    now = database_now(worker_context)
    failed_event_id = create_event(
        worker_context,
        payload={"order_id": " invalid"},
        next_attempt_at=now - timedelta(seconds=2),
    )
    completed_event_id = create_event(
        worker_context,
        next_attempt_at=now - timedelta(seconds=1),
    )

    first_cycle = run_worker_cycle(
        worker_context.session_factory,
        DEFAULT_POLICY,
        DEFAULT_HANDLER_OPTIONS,
        source_id=worker_context.source_id,
    )
    second_cycle = run_worker_cycle(
        worker_context.session_factory,
        DEFAULT_POLICY,
        DEFAULT_HANDLER_OPTIONS,
        source_id=worker_context.source_id,
    )

    assert first_cycle.processing is not None
    assert first_cycle.processing.event_id == failed_event_id
    assert first_cycle.processing.status == "failed"
    assert second_cycle.processing is not None
    assert second_cycle.processing.event_id == completed_event_id
    assert second_cycle.processing.status == "completed"


def test_committed_claim_survives_worker_loss_and_is_recoverable(
    worker_context: WorkerIntegrationContext,
) -> None:
    event_id = create_event(worker_context)
    abandoned_claim = claim_next_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert abandoned_claim is not None

    committed = load_event(worker_context, event_id)
    assert committed.status == "processing"
    assert committed.lease_token == abandoned_claim.lease_token
    expire_lease(worker_context, event_id)

    recovery = recover_one_expired_event(
        worker_context.session_factory,
        DEFAULT_POLICY,
        source_id=worker_context.source_id,
    )
    assert recovery is not None
    assert recovery.status == "pending"
    assert attempts_for(worker_context, event_id)[0].outcome == "lease_expired"
