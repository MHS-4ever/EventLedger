import os
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings as app_settings
from app.db.session import get_db_session
from app.main import app
from app.models import (
    Event,
    EventManualRetryRequest,
    EventProcessingAttempt,
    EventStatusHistory,
    OrderFulfillment,
)
from app.services.events import authorize_manual_retry
from app.services.order_handler import HandlerOptions
from app.services.processing import (
    claim_next_event,
    process_claim,
    recover_one_expired_event,
)
from app.services.processing_types import ProcessingPolicy
from scripts.prepare_test_db import (
    create_test_database_engine,
    load_test_database_settings,
    validate_test_database_settings,
    verify_test_database_connection,
)

RUN_PG_INTEGRATION = os.getenv("EVENTLEDGER_RUN_PG_INTEGRATION") == "1"
TEST_API_KEY = "pg-manual-retry-key-not-a-secret"
POLICY = ProcessingPolicy(30, 3, 2, 8)

pytestmark = [
    pytest.mark.pg_integration,
    pytest.mark.skipif(
        not RUN_PG_INTEGRATION,
        reason="Set EVENTLEDGER_RUN_PG_INTEGRATION=1 to run PostgreSQL tests.",
    ),
]


@dataclass(frozen=True)
class ManualRetryContext:
    engine: Engine
    session_factory: sessionmaker[Session]
    source_prefix: str
    api_key: str

    @property
    def source_id(self) -> str:
        return f"{self.source_prefix}-owner"

    @property
    def foreign_source_id(self) -> str:
        return f"{self.source_prefix}-foreign"

    def headers(self, key: str) -> dict[str, str]:
        return {"X-API-Key": self.api_key, "Idempotency-Key": key}


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
                if revision != "d4e5f6a7b8c9":
                    pytest.fail(
                        "Isolated PostgreSQL test database is not at the "
                        "manual-retry foundation revision."
                    )
        except (RuntimeError, SQLAlchemyError):
            pytest.fail("Unable to verify the isolated PostgreSQL test database.")
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def retry_context(
    pg_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[ManualRetryContext, None, None]:
    source_prefix = f"manual-retry-pg-{uuid4().hex}"
    factory = sessionmaker(
        bind=pg_engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )
    context = ManualRetryContext(
        engine=pg_engine,
        session_factory=factory,
        source_prefix=source_prefix,
        api_key=TEST_API_KEY,
    )
    monkeypatch.setattr(
        app_settings,
        "eventledger_api_key",
        SecretStr(TEST_API_KEY),
    )
    monkeypatch.setattr(app_settings, "eventledger_source_id", context.source_id)

    def test_session_dependency() -> Generator[Session, None, None]:
        session = factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db_session] = test_session_dependency
    yield context

    app.dependency_overrides.pop(get_db_session, None)
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
            delete(EventManualRetryRequest).where(
                EventManualRetryRequest.event_id.in_(event_ids)
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


def create_event(
    context: ManualRetryContext,
    *,
    source_id: str | None = None,
    status: str = "failed",
    attempt_count: int = 3,
    outcomes: list[str | None] | None = None,
    attempt_numbers: list[int] | None = None,
    attempt_limit_override: int | None = None,
    payload: dict | None = None,
) -> UUID:
    event_id = uuid4()
    timestamp = datetime.now(timezone.utc) - timedelta(minutes=5)
    event_payload = payload or {"order_id": f"ORD-{uuid4().hex}"}
    numbers = (
        attempt_numbers
        if attempt_numbers is not None
        else list(range(1, attempt_count + 1))
    )
    attempt_outcomes = outcomes or ["retryable_failure"] * len(numbers)
    with context.session_factory.begin() as session:
        session.add(
            Event(
                id=event_id,
                source_id=source_id or context.source_id,
                idempotency_key=f"manual-retry-{uuid4().hex}",
                event_type="order.created",
                payload=event_payload,
                request_hash="c" * 64,
                status=status,
                attempt_count=attempt_count,
                attempt_limit_override=attempt_limit_override,
                last_error="password=internal-secret",
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        session.add(
            EventStatusHistory(
                event_id=event_id,
                status=status,
                message="Internal terminal failure",
                created_at=timestamp,
            )
        )
        for index, (number, outcome) in enumerate(
            zip(numbers, attempt_outcomes, strict=True)
        ):
            started_at = timestamp + timedelta(seconds=index + 1)
            session.add(
                EventProcessingAttempt(
                    event_id=event_id,
                    attempt_number=number,
                    lease_token=uuid4(),
                    started_at=started_at,
                    finished_at=(
                        started_at + timedelta(seconds=1)
                        if outcome is not None
                        else None
                    ),
                    outcome=outcome,
                    error="token=attempt-secret",
                )
            )
    return event_id


def load_event(context: ManualRetryContext, event_id: UUID) -> Event:
    with context.session_factory() as session:
        event = session.get(Event, event_id)
        assert event is not None
        session.expunge(event)
        return event


def retry_requests(
    context: ManualRetryContext,
    event_id: UUID,
) -> list[EventManualRetryRequest]:
    with context.session_factory() as session:
        return list(
            session.scalars(
                select(EventManualRetryRequest).where(
                    EventManualRetryRequest.event_id == event_id
                )
            )
        )


def attempts(
    context: ManualRetryContext,
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


def history(
    context: ManualRetryContext,
    event_id: UUID,
) -> list[EventStatusHistory]:
    with context.session_factory() as session:
        return list(
            session.scalars(
                select(EventStatusHistory)
                .where(EventStatusHistory.event_id == event_id)
                .order_by(EventStatusHistory.created_at, EventStatusHistory.id)
            )
        )


def post_retry(
    context: ManualRetryContext,
    event_id: UUID,
    key: str,
):
    with TestClient(app) as client:
        return client.post(
            f"/v1/events/{event_id}/retry",
            headers=context.headers(key),
        )


def expire_lease(context: ManualRetryContext, event_id: UUID) -> None:
    with context.session_factory.begin() as session:
        session.execute(
            update(Event)
            .where(Event.id == event_id, Event.status == "processing")
            .values(
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1)
            )
        )


def test_valid_retry_is_atomic_and_preserves_attempt_history(
    retry_context: ManualRetryContext,
) -> None:
    event_id = create_event(retry_context)
    original_attempts = [
        (attempt.id, attempt.attempt_number, attempt.outcome, attempt.error)
        for attempt in attempts(retry_context, event_id)
    ]
    original_history_count = len(history(retry_context, event_id))
    key = f"retry-{uuid4().hex}"

    response = post_retry(retry_context, event_id, key)

    assert response.status_code == 202
    assert response.headers["location"] == f"/v1/events/{event_id}"
    assert "X-Idempotent-Replay" not in response.headers
    body = response.json()
    assert set(body) == {
        "event_id",
        "retry_request_id",
        "status",
        "target_attempt_number",
        "accepted_at",
    }
    assert body["event_id"] == str(event_id)
    assert body["status"] == "accepted"
    assert body["target_attempt_number"] == 4
    assert key not in response.text
    assert "internal-secret" not in response.text
    assert "attempt-secret" not in response.text
    assert "lease_token" not in response.text

    event = load_event(retry_context, event_id)
    assert event.status == "pending"
    assert event.attempt_count == 3
    assert event.attempt_limit_override == 4
    assert event.lease_token is None
    assert event.lease_expires_at is None
    stored_requests = retry_requests(retry_context, event_id)
    assert len(stored_requests) == 1
    assert str(stored_requests[0].id) == body["retry_request_id"]
    assert stored_requests[0].idempotency_key == key
    assert stored_requests[0].target_attempt_number == 4
    assert [
        (attempt.id, attempt.attempt_number, attempt.outcome, attempt.error)
        for attempt in attempts(retry_context, event_id)
    ] == original_attempts
    status_history = history(retry_context, event_id)
    assert len(status_history) == original_history_count + 1
    assert status_history[-1].status == "pending"
    assert status_history[-1].message == (
        "Manual retry accepted for processing attempt 4"
    )


def test_worker_claims_and_completes_authorized_attempt(
    retry_context: ManualRetryContext,
) -> None:
    event_id = create_event(retry_context)
    key = f"complete-{uuid4().hex}"
    accepted = post_retry(retry_context, event_id, key)
    assert accepted.status_code == 202

    claim = claim_next_event(
        retry_context.session_factory,
        POLICY,
        source_id=retry_context.source_id,
    )
    assert claim is not None
    assert claim.event_id == event_id
    assert claim.attempt_number == 4

    processing_replay = post_retry(retry_context, event_id, key)
    assert processing_replay.status_code == 200
    assert processing_replay.json() == accepted.json()
    assert processing_replay.headers["X-Idempotent-Replay"] == "true"

    result = process_claim(
        retry_context.session_factory,
        claim,
        POLICY,
        HandlerOptions(),
    )
    assert result.status == "completed"
    completed_replay = post_retry(retry_context, event_id, key)
    assert completed_replay.status_code == 200
    assert completed_replay.json() == accepted.json()
    assert load_event(retry_context, event_id).status == "completed"
    with retry_context.session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(OrderFulfillment)
            .where(OrderFulfillment.first_event_id == event_id)
        ) == 1


def test_retryable_manual_attempt_is_terminal(
    retry_context: ManualRetryContext,
) -> None:
    event_id = create_event(
        retry_context,
        payload={
            "order_id": f"ORD-{uuid4().hex}",
            "_demo_failure": "transient_always",
        },
    )
    key = f"terminal-{uuid4().hex}"
    accepted = post_retry(retry_context, event_id, key)
    assert accepted.status_code == 202
    policy = replace(POLICY, max_attempts=100)
    claim = claim_next_event(
        retry_context.session_factory,
        policy,
        source_id=retry_context.source_id,
    )
    assert claim is not None
    result = process_claim(
        retry_context.session_factory,
        claim,
        policy,
        HandlerOptions(failure_simulation_enabled=True),
    )

    assert result.status == "failed"
    assert load_event(retry_context, event_id).attempt_count == 4
    assert claim_next_event(
        retry_context.session_factory,
        policy,
        source_id=retry_context.source_id,
    ) is None
    failed_replay = post_retry(retry_context, event_id, key)
    assert failed_replay.status_code == 200
    assert failed_replay.json() == accepted.json()


def test_expired_manual_attempt_is_terminal(
    retry_context: ManualRetryContext,
) -> None:
    event_id = create_event(retry_context)
    assert post_retry(
        retry_context, event_id, f"expired-{uuid4().hex}"
    ).status_code == 202
    policy = replace(POLICY, max_attempts=100)
    claim = claim_next_event(
        retry_context.session_factory,
        policy,
        source_id=retry_context.source_id,
    )
    assert claim is not None
    expire_lease(retry_context, event_id)

    recovery = recover_one_expired_event(
        retry_context.session_factory,
        policy,
        source_id=retry_context.source_id,
    )

    assert recovery is not None
    assert recovery.status == "failed"
    assert load_event(retry_context, event_id).status == "failed"
    assert attempts(retry_context, event_id)[-1].outcome == "lease_expired"


def test_same_key_sequential_replay_returns_original_acceptance(
    retry_context: ManualRetryContext,
) -> None:
    event_id = create_event(retry_context)
    key = f"replay-{uuid4().hex}"

    first = post_retry(retry_context, event_id, key)
    replay = post_retry(retry_context, event_id, key)

    assert first.status_code == 202
    assert replay.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json() == first.json()
    assert len(retry_requests(retry_context, event_id)) == 1
    assert [entry.status for entry in history(retry_context, event_id)].count(
        "pending"
    ) == 1


def test_different_key_duplicate_is_conflict_without_mutation(
    retry_context: ManualRetryContext,
) -> None:
    event_id = create_event(retry_context)
    first = post_retry(retry_context, event_id, f"first-{uuid4().hex}")
    before = load_event(retry_context, event_id)
    before_state = (
        before.status,
        before.attempt_count,
        before.attempt_limit_override,
        before.next_attempt_at,
        len(history(retry_context, event_id)),
    )

    conflict = post_retry(retry_context, event_id, f"second-{uuid4().hex}")

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "manual_retry_limit_reached"
    after = load_event(retry_context, event_id)
    assert (
        after.status,
        after.attempt_count,
        after.attempt_limit_override,
        after.next_attempt_at,
        len(history(retry_context, event_id)),
    ) == before_state
    assert len(retry_requests(retry_context, event_id)) == 1


def test_retry_key_scope_is_per_event(
    retry_context: ManualRetryContext,
) -> None:
    first_event = create_event(retry_context)
    second_event = create_event(retry_context)
    shared_key = f"shared-{uuid4().hex}"

    first = post_retry(retry_context, first_event, shared_key)
    second = post_retry(retry_context, second_event, shared_key)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["retry_request_id"] != second.json()["retry_request_id"]


def concurrent_retries(
    context: ManualRetryContext,
    event_id: UUID,
    keys: list[str],
):
    barrier = Barrier(len(keys))

    def submit(key: str):
        with TestClient(app) as client:
            barrier.wait(timeout=15)
            return client.post(
                f"/v1/events/{event_id}/retry",
                headers=context.headers(key),
            )

    with ThreadPoolExecutor(max_workers=len(keys)) as executor:
        futures = [executor.submit(submit, key) for key in keys]
        return [future.result(timeout=30) for future in futures]


def test_parallel_same_key_creates_one_grant_and_one_replay(
    retry_context: ManualRetryContext,
) -> None:
    event_id = create_event(retry_context)
    key = f"parallel-same-{uuid4().hex}"

    responses = concurrent_retries(retry_context, event_id, [key, key])

    assert sorted(response.status_code for response in responses) == [200, 202]
    assert len({response.json()["retry_request_id"] for response in responses}) == 1
    assert len(retry_requests(retry_context, event_id)) == 1
    assert len(history(retry_context, event_id)) == 2


def test_parallel_different_keys_create_one_grant_and_one_conflict(
    retry_context: ManualRetryContext,
) -> None:
    event_id = create_event(retry_context)
    keys = [f"parallel-{uuid4().hex}", f"parallel-{uuid4().hex}"]

    responses = concurrent_retries(retry_context, event_id, keys)

    assert sorted(response.status_code for response in responses) == [202, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"]["code"] == "manual_retry_limit_reached"
    assert len(retry_requests(retry_context, event_id)) == 1
    assert len(history(retry_context, event_id)) == 2


@pytest.mark.parametrize("event_status", ["pending", "completed"])
def test_nonfailed_event_states_are_conflicts(
    retry_context: ManualRetryContext,
    event_status: str,
) -> None:
    event_id = create_event(
        retry_context,
        status=event_status,
        attempt_count=0,
        outcomes=[],
    )

    response = post_retry(retry_context, event_id, f"state-{uuid4().hex}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "event_state_conflict"
    assert retry_requests(retry_context, event_id) == []


def test_processing_event_is_state_conflict(
    retry_context: ManualRetryContext,
) -> None:
    event_id = create_event(
        retry_context,
        status="pending",
        attempt_count=0,
        outcomes=[],
    )
    claim = claim_next_event(
        retry_context.session_factory,
        POLICY,
        source_id=retry_context.source_id,
    )
    assert claim is not None

    response = post_retry(retry_context, event_id, f"processing-{uuid4().hex}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "event_state_conflict"


@pytest.mark.parametrize(
    ("attempt_count", "numbers", "outcomes", "override"),
    [
        (1, [], [], None),
        (2, [2], ["retryable_failure"], None),
        (1, [1], [None], None),
        (1, [1], ["completed"], None),
        (1, [1], ["permanent_failure"], None),
        (1, [1], ["retryable_failure"], 2),
    ],
)
def test_ineligible_or_malformed_failures_are_rejected(
    retry_context: ManualRetryContext,
    attempt_count: int,
    numbers: list[int],
    outcomes: list[str | None],
    override: int | None,
) -> None:
    event_id = create_event(
        retry_context,
        attempt_count=attempt_count,
        attempt_numbers=numbers,
        outcomes=outcomes,
        attempt_limit_override=override,
    )

    response = post_retry(retry_context, event_id, f"invalid-{uuid4().hex}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "manual_retry_not_allowed"
    assert retry_requests(retry_context, event_id) == []
    assert load_event(retry_context, event_id).status == "failed"


@pytest.mark.parametrize(
    "headers",
    [
        {"X-API-Key": TEST_API_KEY},
        {"X-API-Key": TEST_API_KEY, "Idempotency-Key": ""},
        {"X-API-Key": TEST_API_KEY, "Idempotency-Key": "contains space"},
        {"X-API-Key": TEST_API_KEY, "Idempotency-Key": "x" * 256},
    ],
)
def test_invalid_or_missing_retry_key_is_rejected(
    retry_context: ManualRetryContext,
    headers: dict[str, str],
) -> None:
    event_id = create_event(retry_context)
    with TestClient(app) as client:
        response = client.post(
            f"/v1/events/{event_id}/retry",
            headers=headers,
        )

    assert response.status_code == 422
    assert retry_requests(retry_context, event_id) == []


@pytest.mark.parametrize("api_key", [None, "invalid-api-key"])
def test_retry_authentication_is_required(
    retry_context: ManualRetryContext,
    api_key: str | None,
) -> None:
    event_id = create_event(retry_context)
    headers = {"Idempotency-Key": f"auth-{uuid4().hex}"}
    if api_key is not None:
        headers["X-API-Key"] = api_key

    with TestClient(app) as client:
        response = client.post(
            f"/v1/events/{event_id}/retry",
            headers=headers,
        )

    assert response.status_code == 401
    assert retry_requests(retry_context, event_id) == []


def test_foreign_event_is_indistinguishable_from_missing_event(
    retry_context: ManualRetryContext,
) -> None:
    foreign_event = create_event(
        retry_context,
        source_id=retry_context.foreign_source_id,
    )
    key = f"hidden-{uuid4().hex}"

    foreign = post_retry(retry_context, foreign_event, key)
    missing = post_retry(retry_context, uuid4(), key)

    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "Event not found"}


def test_history_error_rolls_back_event_and_retry_audit(
    retry_context: ManualRetryContext,
) -> None:
    event_id = create_event(retry_context)
    original_history_count = len(history(retry_context, event_id))

    class FailingHistorySession(Session):
        def add(self, instance, *args, **kwargs) -> None:
            if (
                isinstance(instance, EventStatusHistory)
                and instance.message is not None
                and instance.message.startswith("Manual retry accepted")
            ):
                raise SQLAlchemyError("secret database insertion failure")
            super().add(instance, *args, **kwargs)

    failing_factory = sessionmaker(
        bind=retry_context.engine,
        class_=FailingHistorySession,
        autoflush=False,
        expire_on_commit=False,
    )

    def failing_dependency() -> Generator[Session, None, None]:
        session = failing_factory()
        try:
            yield session
        finally:
            session.close()

    previous_override = app.dependency_overrides[get_db_session]
    app.dependency_overrides[get_db_session] = failing_dependency
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/v1/events/{event_id}/retry",
                headers=retry_context.headers(f"rollback-{uuid4().hex}"),
            )
    finally:
        app.dependency_overrides[get_db_session] = previous_override

    assert response.status_code == 500
    assert response.json() == {"detail": "Unable to authorize manual retry"}
    event = load_event(retry_context, event_id)
    assert event.status == "failed"
    assert event.attempt_limit_override is None
    assert retry_requests(retry_context, event_id) == []
    assert len(history(retry_context, event_id)) == original_history_count
    assert "secret" not in response.text


def test_worker_cannot_observe_half_committed_authorization(
    retry_context: ManualRetryContext,
) -> None:
    event_id = create_event(retry_context)
    with retry_context.session_factory() as authorizing_session:
        authorization = authorize_manual_retry(
            authorizing_session,
            event_id=event_id,
            source_id=retry_context.source_id,
            idempotency_key=f"isolation-{uuid4().hex}",
        )
        authorizing_session.flush()

        with ThreadPoolExecutor(max_workers=1) as executor:
            before_commit = executor.submit(
                claim_next_event,
                retry_context.session_factory,
                POLICY,
                source_id=retry_context.source_id,
            ).result(timeout=10)
        assert before_commit is None
        authorizing_session.commit()

    after_commit = claim_next_event(
        retry_context.session_factory,
        POLICY,
        source_id=retry_context.source_id,
    )
    assert authorization.target_attempt_number == 4
    assert after_commit is not None
    assert after_commit.event_id == event_id
    assert after_commit.attempt_number == 4
