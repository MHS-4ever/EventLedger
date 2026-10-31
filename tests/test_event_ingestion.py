from collections.abc import Callable, Generator
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import app.db.session as session_module
from app.core.config import settings
from app.db.session import get_db_session
from app.main import app
from app.models import Event, EventStatusHistory
from app.services.events import compute_request_hash

TEST_API_KEY = "test-api-key-not-a-secret"
TEST_SOURCE_ID = "test-application"
VALID_HEADERS = {
    "X-API-Key": TEST_API_KEY,
    "Idempotency-Key": "request-123",
}
VALID_BODY = {
    "event_type": "order.created",
    "payload": {"order_id": "order-123", "amount": 42.5},
}


def stored_event(
    *,
    event_id: UUID | None = None,
    source_id: str = TEST_SOURCE_ID,
    idempotency_key: str = "request-123",
    event_type: str = "order.created",
    payload: dict | None = None,
    status: str = "pending",
    attempt_count: int = 0,
) -> Event:
    event_payload = (
        payload
        if payload is not None
        else {"order_id": "order-123", "amount": 42.5}
    )
    created_at = datetime.now(timezone.utc)
    return Event(
        id=event_id or uuid4(),
        source_id=source_id,
        idempotency_key=idempotency_key,
        event_type=event_type,
        payload=event_payload,
        request_hash=compute_request_hash(event_type, event_payload),
        status=status,
        attempt_count=attempt_count,
        created_at=created_at,
        updated_at=created_at,
    )


class FakeScalarResult:
    def __init__(self, value: Event | None) -> None:
        self.value = value

    def one_or_none(self) -> Event | None:
        return self.value


class FakeSession:
    def __init__(
        self,
        *,
        inserted_event: Event | None = None,
        existing_event: Event | None = None,
        execute_error: Exception | None = None,
        commit_error: Exception | None = None,
    ) -> None:
        self.inserted_event = inserted_event
        self.existing_event = existing_event
        self.execute_error = execute_error
        self.commit_error = commit_error
        self.added: list[object] = []
        self.statement_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def scalars(self, statement: object) -> FakeScalarResult:
        self.statement_count += 1
        if self.execute_error is not None:
            raise self.execute_error
        return FakeScalarResult(self.inserted_event)

    def scalar(self, statement: object) -> Event | None:
        self.statement_count += 1
        if self.execute_error is not None:
            raise self.execute_error
        return self.existing_event

    def add(self, instance: object) -> None:
        self.added.append(instance)

    def commit(self) -> None:
        self.commit_count += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.close_count += 1


class FakeDriverError(Exception):
    def __init__(self, sqlstate: str, constraint_name: str) -> None:
        super().__init__("simulated database error")
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(constraint_name=constraint_name)


@pytest.fixture
def install_session_override() -> Generator[
    Callable[[FakeSession], None],
    None,
    None,
]:
    def install(session: FakeSession) -> None:
        def override() -> Generator[FakeSession, None, None]:
            yield session

        app.dependency_overrides[get_db_session] = override

    yield install
    app.dependency_overrides.pop(get_db_session, None)


client = TestClient(app)


def test_request_hash_is_canonical_and_sensitive_to_request_values() -> None:
    first_payload = {
        "order": {"id": "order-123", "paid": True},
        "items": [{"sku": "item-1", "quantity": 2}],
    }
    reordered_payload = {
        "items": [{"quantity": 2, "sku": "item-1"}],
        "order": {"paid": True, "id": "order-123"},
    }
    changed_payload = {
        "order": {"id": "order-123", "paid": False},
        "items": [{"sku": "item-1", "quantity": 2}],
    }

    original_hash = compute_request_hash("order.created", first_payload)

    assert original_hash == compute_request_hash(
        "order.created", reordered_payload
    )
    assert original_hash != compute_request_hash("order.created", changed_payload)
    assert original_hash != compute_request_hash("order.updated", first_payload)
    assert len(original_hash) == 64
    assert original_hash == original_hash.lower()


def test_session_dependency_rolls_back_and_closes_on_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    session = FakeSession()
    monkeypatch.setattr(session_module, "SessionLocal", lambda: session)
    dependency = get_db_session()

    assert next(dependency) is session
    with pytest.raises(RuntimeError, match="simulated request failure"):
        dependency.throw(RuntimeError("simulated request failure"))

    assert session.rollback_count == 1
    assert session.close_count == 1


def test_first_request_creates_event_and_history(
    install_session_override: Callable[[FakeSession], None],
) -> None:
    event = stored_event()
    session = FakeSession(inserted_event=event)
    install_session_override(session)

    response = client.post(
        "/v1/events",
        headers=VALID_HEADERS,
        json=VALID_BODY,
    )

    assert response.status_code == 201
    assert response.json() == {
        "id": str(event.id),
        "source_id": TEST_SOURCE_ID,
        "event_type": "order.created",
        "status": "pending",
        "created_at": event.created_at.isoformat().replace("+00:00", "Z"),
    }
    assert session.commit_count == 1
    assert session.rollback_count == 0
    assert len(session.added) == 1

    history = session.added[0]
    assert isinstance(history, EventStatusHistory)
    assert history.event_id == event.id
    assert history.status == "pending"


def test_identical_retry_returns_original_event_with_http_200(
    install_session_override: Callable[[FakeSession], None],
) -> None:
    original_event = stored_event()
    session = FakeSession(existing_event=original_event)
    install_session_override(session)

    response = client.post(
        "/v1/events",
        headers=VALID_HEADERS,
        json=VALID_BODY,
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(original_event.id)
    assert response.headers["X-Idempotent-Replay"] == "true"
    assert session.added == []
    assert session.commit_count == 0


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("order.created", {"order_id": "order-999", "amount": 42.5}),
        ("order.updated", {"order_id": "order-123", "amount": 42.5}),
    ],
)
def test_changed_request_with_same_key_returns_conflict(
    event_type: str,
    payload: dict,
    install_session_override: Callable[[FakeSession], None],
) -> None:
    original_event = stored_event()
    session = FakeSession(existing_event=original_event)
    install_session_override(session)

    response = client.post(
        "/v1/events",
        headers=VALID_HEADERS,
        json={"event_type": event_type, "payload": payload},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "idempotency_key_conflict",
            "message": (
                "Idempotency key was already used for a different request"
            ),
        }
    }
    assert session.added == []
    assert session.commit_count == 0


def test_reordered_equivalent_payload_is_an_idempotent_replay(
    install_session_override: Callable[[FakeSession], None],
) -> None:
    stored_payload = {
        "order": {"id": "order-123", "paid": True},
        "items": [{"sku": "item-1", "quantity": 2}],
    }
    reordered_payload = {
        "items": [{"quantity": 2, "sku": "item-1"}],
        "order": {"paid": True, "id": "order-123"},
    }
    original_event = stored_event(payload=stored_payload)
    session = FakeSession(existing_event=original_event)
    install_session_override(session)

    response = client.post(
        "/v1/events",
        headers=VALID_HEADERS,
        json={"event_type": "order.created", "payload": reordered_payload},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(original_event.id)
    assert session.added == []


def test_replay_does_not_change_history_or_processing_fields(
    install_session_override: Callable[[FakeSession], None],
) -> None:
    original_event = stored_event(status="processing", attempt_count=3)
    original_updated_at = original_event.updated_at
    session = FakeSession(existing_event=original_event)
    install_session_override(session)

    response = client.post(
        "/v1/events",
        headers=VALID_HEADERS,
        json=VALID_BODY,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    assert original_event.attempt_count == 3
    assert original_event.status == "processing"
    assert original_event.updated_at == original_updated_at
    assert session.added == []
    assert session.commit_count == 0


def test_same_key_is_allowed_for_a_different_authenticated_source(
    install_session_override: Callable[[FakeSession], None],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "eventledger_source_id", "another-application")
    event = stored_event(source_id="another-application")
    session = FakeSession(inserted_event=event)
    install_session_override(session)

    response = client.post(
        "/v1/events",
        headers=VALID_HEADERS,
        json=VALID_BODY,
    )

    assert response.status_code == 201
    assert response.json()["source_id"] == "another-application"


def test_new_keys_produce_distinct_events(
    install_session_override: Callable[[FakeSession], None],
) -> None:
    first_event = stored_event(idempotency_key="request-1")
    first_session = FakeSession(inserted_event=first_event)
    install_session_override(first_session)
    first_response = client.post(
        "/v1/events",
        headers={**VALID_HEADERS, "Idempotency-Key": "request-1"},
        json=VALID_BODY,
    )

    second_event = stored_event(idempotency_key="request-2")
    second_session = FakeSession(inserted_event=second_event)
    install_session_override(second_session)
    second_response = client.post(
        "/v1/events",
        headers={**VALID_HEADERS, "Idempotency-Key": "request-2"},
        json=VALID_BODY,
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert first_response.json()["id"] != second_response.json()["id"]


@pytest.mark.parametrize("api_key", [None, "invalid-test-key"])
def test_authentication_is_required(
    api_key: str | None,
    install_session_override: Callable[[FakeSession], None],
) -> None:
    session = FakeSession(inserted_event=stored_event())
    install_session_override(session)
    headers = {"Idempotency-Key": "request-123"}
    if api_key is not None:
        headers["X-API-Key"] = api_key

    response = client.post("/v1/events", headers=headers, json=VALID_BODY)

    assert response.status_code == 401
    assert session.statement_count == 0


def test_invalid_request_body_is_rejected(
    install_session_override: Callable[[FakeSession], None],
) -> None:
    session = FakeSession(inserted_event=stored_event())
    install_session_override(session)

    response = client.post(
        "/v1/events",
        headers=VALID_HEADERS,
        json={"event_type": "", "payload": []},
    )

    assert response.status_code == 422
    assert session.statement_count == 0


def test_missing_idempotency_key_is_rejected(
    install_session_override: Callable[[FakeSession], None],
) -> None:
    session = FakeSession(inserted_event=stored_event())
    install_session_override(session)

    response = client.post(
        "/v1/events",
        headers={"X-API-Key": TEST_API_KEY},
        json=VALID_BODY,
    )

    assert response.status_code == 422
    assert session.statement_count == 0


@pytest.mark.parametrize(
    "idempotency_key",
    ["", "   ", "contains whitespace", "x" * 256],
)
def test_invalid_idempotency_key_is_rejected(
    idempotency_key: str,
    install_session_override: Callable[[FakeSession], None],
) -> None:
    session = FakeSession(inserted_event=stored_event())
    install_session_override(session)

    response = client.post(
        "/v1/events",
        headers={
            "X-API-Key": TEST_API_KEY,
            "Idempotency-Key": idempotency_key,
        },
        json=VALID_BODY,
    )

    assert response.status_code == 422
    assert session.statement_count == 0


def test_history_failure_rolls_back_new_event_transaction(
    install_session_override: Callable[[FakeSession], None],
) -> None:
    event = stored_event()
    session = FakeSession(
        inserted_event=event,
        commit_error=SQLAlchemyError("simulated history failure"),
    )
    install_session_override(session)

    response = client.post(
        "/v1/events",
        headers=VALID_HEADERS,
        json=VALID_BODY,
    )

    assert response.status_code == 500
    assert session.commit_count == 1
    assert session.rollback_count == 1
    assert len(session.added) == 1
    assert isinstance(session.added[0], EventStatusHistory)
    assert response.json() == {"detail": "Unable to create event"}


def test_unrelated_database_error_is_not_converted_to_idempotent_replay(
    install_session_override: Callable[[FakeSession], None],
) -> None:
    driver_error = FakeDriverError("23505", "some_other_unique_constraint")
    unrelated_error = IntegrityError("INSERT ...", {}, driver_error)
    session = FakeSession(execute_error=unrelated_error)
    install_session_override(session)

    response = client.post(
        "/v1/events",
        headers=VALID_HEADERS,
        json=VALID_BODY,
    )

    assert response.status_code == 500
    assert session.rollback_count == 1
    assert response.json() == {"detail": "Unable to create event"}
