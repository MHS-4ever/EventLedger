from collections.abc import Callable, Generator
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db_session
from app.main import app
from app.models import Event

TEST_API_KEY = "test-api-key-not-a-secret"
TEST_SOURCE_ID = "test-application"


def stored_event(
    *,
    event_id: UUID | None = None,
    source_id: str = TEST_SOURCE_ID,
) -> Event:
    created_at = datetime.now(timezone.utc)
    return Event(
        id=event_id or uuid4(),
        source_id=source_id,
        idempotency_key=f"retrieval-{uuid4().hex}",
        event_type="order.created",
        payload={
            "order_id": "order-123",
            "items": [{"sku": "item-1", "quantity": 2}],
        },
        request_hash="a" * 64,
        status="processing",
        attempt_count=2,
        last_error="internal failure details",
        created_at=created_at,
        updated_at=created_at,
    )


class ReadOnlyFakeSession:
    def __init__(self, events: list[Event] | None = None) -> None:
        self.events = events or []
        self.statements: list[object] = []
        self.add_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def scalar(self, statement: object) -> Event | None:
        self.statements.append(statement)
        parameter_values = set(statement.compile().params.values())

        for event in self.events:
            if event.id in parameter_values and event.source_id in parameter_values:
                return event
        return None

    def add(self, instance: object) -> None:
        self.add_count += 1

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


@pytest.fixture
def install_read_session() -> Generator[
    Callable[[ReadOnlyFakeSession], None],
    None,
    None,
]:
    def install(session: ReadOnlyFakeSession) -> None:
        def override() -> Generator[ReadOnlyFakeSession, None, None]:
            yield session

        app.dependency_overrides[get_db_session] = override

    yield install
    app.dependency_overrides.pop(get_db_session, None)


client = TestClient(app)


def test_authenticated_owner_can_retrieve_event(
    install_read_session: Callable[[ReadOnlyFakeSession], None],
) -> None:
    event = stored_event()
    session = ReadOnlyFakeSession([event])
    install_read_session(session)

    response = client.get(
        f"/v1/events/{event.id}",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(event.id)
    assert response.json()["payload"] == event.payload
    assert response.json()["status"] == "processing"
    assert response.json()["attempt_count"] == 2

    compiled_parameters = set(session.statements[0].compile().params.values())
    assert event.id in compiled_parameters
    assert TEST_SOURCE_ID in compiled_parameters


@pytest.mark.parametrize("api_key", [None, "invalid-test-key"])
def test_authentication_is_required(
    api_key: str | None,
    install_read_session: Callable[[ReadOnlyFakeSession], None],
) -> None:
    event = stored_event()
    session = ReadOnlyFakeSession([event])
    install_read_session(session)
    headers = {"X-API-Key": api_key} if api_key is not None else {}

    response = client.get(f"/v1/events/{event.id}", headers=headers)

    assert response.status_code == 401
    assert session.statements == []


def test_unknown_event_returns_not_found(
    install_read_session: Callable[[ReadOnlyFakeSession], None],
) -> None:
    session = ReadOnlyFakeSession()
    install_read_session(session)

    response = client.get(
        f"/v1/events/{uuid4()}",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}


def test_event_owned_by_another_source_returns_not_found(
    install_read_session: Callable[[ReadOnlyFakeSession], None],
) -> None:
    event = stored_event(source_id="another-application")
    session = ReadOnlyFakeSession([event])
    install_read_session(session)

    response = client.get(
        f"/v1/events/{event.id}",
        params={"source_id": "another-application"},
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 404
    compiled_parameters = set(session.statements[0].compile().params.values())
    assert TEST_SOURCE_ID in compiled_parameters
    assert "another-application" not in compiled_parameters


def test_invalid_uuid_returns_validation_error(
    install_read_session: Callable[[ReadOnlyFakeSession], None],
) -> None:
    session = ReadOnlyFakeSession()
    install_read_session(session)

    response = client.get(
        "/v1/events/not-a-uuid",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 422
    assert session.statements == []


def test_response_omits_internal_fields(
    install_read_session: Callable[[ReadOnlyFakeSession], None],
) -> None:
    event = stored_event()
    session = ReadOnlyFakeSession([event])
    install_read_session(session)

    response = client.get(
        f"/v1/events/{event.id}",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert set(response.json()) == {
        "id",
        "source_id",
        "event_type",
        "payload",
        "status",
        "attempt_count",
        "created_at",
        "updated_at",
    }
    assert "request_hash" not in response.text
    assert "last_error" not in response.text
    assert "idempotency_key" not in response.text


def test_retrieval_does_not_modify_event_or_history(
    install_read_session: Callable[[ReadOnlyFakeSession], None],
) -> None:
    event = stored_event()
    before = {
        "status": event.status,
        "attempt_count": event.attempt_count,
        "updated_at": event.updated_at,
        "last_error": event.last_error,
    }
    session = ReadOnlyFakeSession([event])
    install_read_session(session)

    response = client.get(
        f"/v1/events/{event.id}",
        headers={"X-API-Key": TEST_API_KEY},
    )

    after = {
        "status": event.status,
        "attempt_count": event.attempt_count,
        "updated_at": event.updated_at,
        "last_error": event.last_error,
    }
    assert response.status_code == 200
    assert after == before
    assert session.add_count == 0
    assert session.commit_count == 0
    assert session.rollback_count == 0
