from collections.abc import Callable, Generator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db_session
from app.main import app
from app.models import EventStatusHistory

TEST_API_KEY = "test-api-key-not-a-secret"
TEST_SOURCE_ID = "test-application"


def history_entry(
    event_id: UUID,
    *,
    history_id: UUID | None = None,
    status: str = "pending",
    created_at: datetime | None = None,
) -> EventStatusHistory:
    return EventStatusHistory(
        id=history_id or uuid4(),
        event_id=event_id,
        status=status,
        message="raw internal status message",
        created_at=created_at or datetime.now(timezone.utc),
    )


class FakeHistoryResult:
    def __init__(self, entries: list[EventStatusHistory]) -> None:
        self.entries = entries

    def all(self) -> list[EventStatusHistory]:
        return self.entries


class HistoryFakeSession:
    def __init__(
        self,
        event_id: UUID,
        *,
        authorized: bool = True,
        entries: list[EventStatusHistory] | None = None,
        total: int = 0,
    ) -> None:
        self.event_id = event_id
        self.authorized = authorized
        self.entries = entries or []
        self.total = total
        self.scalar_statements: list[object] = []
        self.items_statements: list[object] = []
        self.add_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def scalar(self, statement: object):
        self.scalar_statements.append(statement)
        if len(self.scalar_statements) == 1:
            parameters = compiled_parameters(statement)
            if (
                self.authorized
                and self.event_id in parameters
                and TEST_SOURCE_ID in parameters
            ):
                return self.event_id
            return None
        return self.total

    def scalars(self, statement: object) -> FakeHistoryResult:
        self.items_statements.append(statement)
        return FakeHistoryResult(self.entries)

    def add(self, instance: object) -> None:
        self.add_count += 1

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


@pytest.fixture
def install_history_session() -> Generator[
    Callable[[HistoryFakeSession], None],
    None,
    None,
]:
    def install(session: HistoryFakeSession) -> None:
        def override() -> Generator[HistoryFakeSession, None, None]:
            yield session

        app.dependency_overrides[get_db_session] = override

    yield install
    app.dependency_overrides.pop(get_db_session, None)


client = TestClient(app)


def compiled_parameters(statement: object) -> set[object]:
    return set(statement.compile().params.values())


def test_authenticated_request_retrieves_history(
    install_history_session: Callable[[HistoryFakeSession], None],
) -> None:
    event_id = uuid4()
    entry = history_entry(event_id)
    session = HistoryFakeSession(event_id, entries=[entry], total=1)
    install_history_session(session)

    response = client.get(
        f"/v1/events/{event_id}/history",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json()["event_id"] == str(event_id)
    assert response.json()["total"] == 1
    assert response.json()["items"] == [
        {
            "id": str(entry.id),
            "status": "pending",
            "created_at": entry.created_at.isoformat().replace("+00:00", "Z"),
        }
    ]


@pytest.mark.parametrize("api_key", [None, "invalid-test-key"])
def test_authentication_is_required(
    api_key: str | None,
    install_history_session: Callable[[HistoryFakeSession], None],
) -> None:
    event_id = uuid4()
    session = HistoryFakeSession(event_id)
    install_history_session(session)
    headers = {"X-API-Key": api_key} if api_key is not None else {}

    response = client.get(f"/v1/events/{event_id}/history", headers=headers)

    assert response.status_code == 401
    assert session.scalar_statements == []
    assert session.items_statements == []


@pytest.mark.parametrize(
    "client_supplied_source",
    [None, "another-application"],
    ids=["nonexistent", "other-source"],
)
def test_missing_or_other_source_event_returns_not_found(
    client_supplied_source: str | None,
    install_history_session: Callable[[HistoryFakeSession], None],
) -> None:
    event_id = uuid4()
    session = HistoryFakeSession(event_id, authorized=False)
    install_history_session(session)

    response = client.get(
        f"/v1/events/{event_id}/history",
        params=(
            {"source_id": client_supplied_source}
            if client_supplied_source is not None
            else {}
        ),
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}
    assert len(session.scalar_statements) == 1
    assert session.items_statements == []
    parameters = compiled_parameters(session.scalar_statements[0])
    assert TEST_SOURCE_ID in parameters
    if client_supplied_source is not None:
        assert client_supplied_source not in parameters


def test_invalid_uuid_returns_validation_error(
    install_history_session: Callable[[HistoryFakeSession], None],
) -> None:
    session = HistoryFakeSession(uuid4())
    install_history_session(session)

    response = client.get(
        "/v1/events/not-a-uuid/history",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 422
    assert session.scalar_statements == []
    assert session.items_statements == []


def test_history_is_ordered_oldest_first_with_uuid_tiebreaker(
    install_history_session: Callable[[HistoryFakeSession], None],
) -> None:
    event_id = uuid4()
    timestamp = datetime.now(timezone.utc)
    oldest = history_entry(event_id, created_at=timestamp - timedelta(seconds=1))
    tied_low = history_entry(
        event_id,
        history_id=UUID("00000000-0000-0000-0000-000000000001"),
        status="processing",
        created_at=timestamp,
    )
    tied_high = history_entry(
        event_id,
        history_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        status="completed",
        created_at=timestamp,
    )
    session = HistoryFakeSession(
        event_id,
        entries=[oldest, tied_low, tied_high],
        total=3,
    )
    install_history_session(session)

    response = client.get(
        f"/v1/events/{event_id}/history",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [
        str(oldest.id),
        str(tied_low.id),
        str(tied_high.id),
    ]
    statement_sql = " ".join(str(session.items_statements[0]).split())
    assert (
        "ORDER BY event_status_history.created_at ASC, "
        "event_status_history.id ASC"
    ) in statement_sql


def test_pagination_and_total_are_returned_and_applied(
    install_history_session: Callable[[HistoryFakeSession], None],
) -> None:
    event_id = uuid4()
    entries = [history_entry(event_id), history_entry(event_id)]
    session = HistoryFakeSession(event_id, entries=entries, total=7)
    install_history_session(session)

    response = client.get(
        f"/v1/events/{event_id}/history",
        params={"limit": 2, "offset": 3},
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json()["limit"] == 2
    assert response.json()["offset"] == 3
    assert response.json()["total"] == 7
    query_parameters = compiled_parameters(session.items_statements[0])
    assert event_id in query_parameters
    assert 2 in query_parameters
    assert 3 in query_parameters


@pytest.mark.parametrize(
    "parameters",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 101},
        {"offset": -1},
        {"limit": "not-an-integer"},
        {"offset": "not-an-integer"},
    ],
)
def test_invalid_pagination_values_are_rejected(
    parameters: dict[str, object],
    install_history_session: Callable[[HistoryFakeSession], None],
) -> None:
    event_id = uuid4()
    session = HistoryFakeSession(event_id)
    install_history_session(session)

    response = client.get(
        f"/v1/events/{event_id}/history",
        params=parameters,
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 422
    assert session.scalar_statements == []
    assert session.items_statements == []


def test_authorized_event_with_no_history_returns_empty_page(
    install_history_session: Callable[[HistoryFakeSession], None],
) -> None:
    event_id = uuid4()
    session = HistoryFakeSession(event_id)
    install_history_session(session)

    response = client.get(
        f"/v1/events/{event_id}/history",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json() == {
        "event_id": str(event_id),
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }


def test_response_hides_messages_and_retrieval_is_read_only(
    install_history_session: Callable[[HistoryFakeSession], None],
) -> None:
    event_id = uuid4()
    entry = history_entry(event_id, status="failed")
    before = {
        "status": entry.status,
        "message": entry.message,
        "created_at": entry.created_at,
    }
    session = HistoryFakeSession(event_id, entries=[entry], total=1)
    install_history_session(session)

    response = client.get(
        f"/v1/events/{event_id}/history",
        headers={"X-API-Key": TEST_API_KEY},
    )

    after = {
        "status": entry.status,
        "message": entry.message,
        "created_at": entry.created_at,
    }
    assert response.status_code == 200
    assert set(response.json()["items"][0]) == {"id", "status", "created_at"}
    assert "message" not in response.text
    assert "raw internal status message" not in response.text
    assert after == before
    assert session.add_count == 0
    assert session.commit_count == 0
    assert session.rollback_count == 0
