from collections.abc import Callable, Generator
from datetime import datetime, timedelta, timezone
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
    event_type: str = "order.created",
    status: str = "pending",
    attempt_count: int = 0,
    created_at: datetime | None = None,
) -> Event:
    timestamp = created_at or datetime.now(timezone.utc)
    return Event(
        id=event_id or uuid4(),
        source_id=source_id,
        idempotency_key=f"listing-{uuid4().hex}",
        event_type=event_type,
        payload={"not": "included in list responses"},
        request_hash="b" * 64,
        status=status,
        attempt_count=attempt_count,
        last_error="internal error details",
        created_at=timestamp,
        updated_at=timestamp,
    )


class FakeScalarListResult:
    def __init__(self, events: list[Event]) -> None:
        self.events = events

    def all(self) -> list[Event]:
        return self.events


class ListingFakeSession:
    def __init__(self, *, events: list[Event] | None = None, total: int = 0) -> None:
        self.events = events or []
        self.total = total
        self.count_statements: list[object] = []
        self.items_statements: list[object] = []

    def scalar(self, statement: object) -> int:
        self.count_statements.append(statement)
        return self.total

    def scalars(self, statement: object) -> FakeScalarListResult:
        self.items_statements.append(statement)
        parameters = compiled_parameters(statement)
        status_filters = {
            value
            for value in parameters
            if value in {"pending", "processing", "completed", "failed"}
        }
        event_type_filters = {
            value
            for value in parameters
            if isinstance(value, str)
            and value != TEST_SOURCE_ID
            and value not in status_filters
        }
        visible_events = [
            event
            for event in self.events
            if event.source_id in parameters
            and (not status_filters or event.status in status_filters)
            and (not event_type_filters or event.event_type in event_type_filters)
        ]
        return FakeScalarListResult(visible_events)


@pytest.fixture
def install_listing_session() -> Generator[
    Callable[[ListingFakeSession], None],
    None,
    None,
]:
    def install(session: ListingFakeSession) -> None:
        def override() -> Generator[ListingFakeSession, None, None]:
            yield session

        app.dependency_overrides[get_db_session] = override

    yield install
    app.dependency_overrides.pop(get_db_session, None)


client = TestClient(app)


def compiled_parameters(statement: object) -> set[object]:
    return set(statement.compile().params.values())


def test_authentication_is_required(
    install_listing_session: Callable[[ListingFakeSession], None],
) -> None:
    session = ListingFakeSession()
    install_listing_session(session)

    missing = client.get("/v1/events")
    invalid = client.get(
        "/v1/events",
        headers={"X-API-Key": "invalid-test-key"},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert session.count_statements == []
    assert session.items_statements == []


def test_events_from_another_source_are_excluded(
    install_listing_session: Callable[[ListingFakeSession], None],
) -> None:
    other_source_event = stored_event(source_id="another-application")
    session = ListingFakeSession(events=[other_source_event], total=0)
    install_listing_session(session)

    response = client.get(
        "/v1/events",
        params={"source_id": "another-application"},
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0
    assert TEST_SOURCE_ID in compiled_parameters(session.count_statements[0])
    assert TEST_SOURCE_ID in compiled_parameters(session.items_statements[0])
    assert "another-application" not in compiled_parameters(
        session.count_statements[0]
    )
    assert "another-application" not in compiled_parameters(
        session.items_statements[0]
    )


def test_empty_results_return_empty_items(
    install_listing_session: Callable[[ListingFakeSession], None],
) -> None:
    session = ListingFakeSession()
    install_listing_session(session)

    response = client.get(
        "/v1/events",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }


def test_default_pagination_and_internal_field_exclusion(
    install_listing_session: Callable[[ListingFakeSession], None],
) -> None:
    event = stored_event(status="processing", attempt_count=2)
    session = ListingFakeSession(events=[event], total=1)
    install_listing_session(session)

    response = client.get(
        "/v1/events",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json()["limit"] == 20
    assert response.json()["offset"] == 0
    assert response.json()["total"] == 1
    assert set(response.json()["items"][0]) == {
        "id",
        "source_id",
        "event_type",
        "status",
        "attempt_count",
        "created_at",
        "updated_at",
    }
    assert "payload" not in response.text
    assert "request_hash" not in response.text
    assert "last_error" not in response.text


def test_limit_and_offset_are_applied_in_database_query(
    install_listing_session: Callable[[ListingFakeSession], None],
) -> None:
    events = [stored_event(), stored_event()]
    session = ListingFakeSession(events=events, total=7)
    install_listing_session(session)

    response = client.get(
        "/v1/events",
        params={"limit": 2, "offset": 3},
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json()["limit"] == 2
    assert response.json()["offset"] == 3
    assert response.json()["total"] == 7
    parameters = compiled_parameters(session.items_statements[0])
    assert 2 in parameters
    assert 3 in parameters


def test_maximum_limit_is_enforced(
    install_listing_session: Callable[[ListingFakeSession], None],
) -> None:
    session = ListingFakeSession()
    install_listing_session(session)

    maximum = client.get(
        "/v1/events",
        params={"limit": 100},
        headers={"X-API-Key": TEST_API_KEY},
    )
    excessive = client.get(
        "/v1/events",
        params={"limit": 101},
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert maximum.status_code == 200
    assert maximum.json()["limit"] == 100
    assert excessive.status_code == 422


@pytest.mark.parametrize(
    "parameters",
    [
        {"limit": 0},
        {"limit": -1},
        {"offset": -1},
        {"limit": "not-an-integer"},
        {"offset": "not-an-integer"},
    ],
)
def test_invalid_pagination_values_are_rejected(
    parameters: dict[str, object],
    install_listing_session: Callable[[ListingFakeSession], None],
) -> None:
    session = ListingFakeSession()
    install_listing_session(session)

    response = client.get(
        "/v1/events",
        params=parameters,
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 422
    assert session.count_statements == []
    assert session.items_statements == []


@pytest.mark.parametrize(
    ("parameters", "expected_values", "event_kwargs"),
    [
        ({"status": "failed"}, {"failed"}, {"status": "failed"}),
        (
            {"event_type": "payment.failed"},
            {"payment.failed"},
            {"event_type": "payment.failed"},
        ),
        (
            {"status": "completed", "event_type": "order.created"},
            {"completed", "order.created"},
            {"status": "completed", "event_type": "order.created"},
        ),
    ],
)
def test_filters_apply_to_count_and_items_queries(
    parameters: dict[str, str],
    expected_values: set[str],
    event_kwargs: dict[str, str],
    install_listing_session: Callable[[ListingFakeSession], None],
) -> None:
    session = ListingFakeSession(events=[stored_event(**event_kwargs)], total=1)
    install_listing_session(session)

    response = client.get(
        "/v1/events",
        params=parameters,
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    count_parameters = compiled_parameters(session.count_statements[0])
    item_parameters = compiled_parameters(session.items_statements[0])
    assert expected_values <= count_parameters
    assert expected_values <= item_parameters
    assert response.json()["total"] == 1
    assert len(response.json()["items"]) == 1


def test_invalid_status_filter_is_rejected(
    install_listing_session: Callable[[ListingFakeSession], None],
) -> None:
    session = ListingFakeSession()
    install_listing_session(session)

    response = client.get(
        "/v1/events",
        params={"status": "unknown"},
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 422
    assert session.count_statements == []
    assert session.items_statements == []


def test_ordering_is_created_at_then_id_descending(
    install_listing_session: Callable[[ListingFakeSession], None],
) -> None:
    timestamp = datetime.now(timezone.utc)
    newer = stored_event(created_at=timestamp + timedelta(seconds=1))
    tied_high_id = stored_event(
        event_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        created_at=timestamp,
    )
    tied_low_id = stored_event(
        event_id=UUID("00000000-0000-0000-0000-000000000001"),
        created_at=timestamp,
    )
    session = ListingFakeSession(
        events=[newer, tied_high_id, tied_low_id],
        total=3,
    )
    install_listing_session(session)

    response = client.get(
        "/v1/events",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    statement_sql = " ".join(str(session.items_statements[0]).split())
    assert "ORDER BY events.created_at DESC, events.id DESC" in statement_sql
    assert [item["id"] for item in response.json()["items"]] == [
        str(newer.id),
        str(tied_high_id.id),
        str(tied_low_id.id),
    ]
