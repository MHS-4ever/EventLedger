import os
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings as app_settings
from app.db.session import get_db_session
from app.main import app
from app.models import Event, EventProcessingAttempt, EventStatusHistory
from scripts.prepare_test_db import (
    create_test_database_engine,
    load_test_database_settings,
    validate_test_database_settings,
    verify_test_database_connection,
)

RUN_PG_INTEGRATION = os.getenv("EVENTLEDGER_RUN_PG_INTEGRATION") == "1"
TEST_API_KEY = "pg-read-api-key-not-a-secret"

pytestmark = [
    pytest.mark.pg_integration,
    pytest.mark.skipif(
        not RUN_PG_INTEGRATION,
        reason="Set EVENTLEDGER_RUN_PG_INTEGRATION=1 to run PostgreSQL tests.",
    ),
]


@dataclass(frozen=True)
class ReadApiContext:
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

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}


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
                        "current manual-retry foundation schema revision."
                    )
        except (RuntimeError, SQLAlchemyError):
            pytest.fail("Unable to verify the isolated PostgreSQL test database.")
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def read_api_context(
    pg_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[ReadApiContext, None, None]:
    source_prefix = f"read-api-pg-{uuid4().hex}"
    session_factory = sessionmaker(
        bind=pg_engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )
    context = ReadApiContext(
        engine=pg_engine,
        session_factory=session_factory,
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
        session = session_factory()
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
    context: ReadApiContext,
    *,
    source_id: str | None = None,
    event_id: UUID | None = None,
    event_type: str = "order.created",
    status: str = "pending",
    created_at: datetime | None = None,
    history_entries: int = 1,
) -> UUID:
    identifier = event_id or uuid4()
    timestamp = created_at or datetime.now(timezone.utc)
    with context.session_factory.begin() as session:
        session.add(
            Event(
                id=identifier,
                source_id=source_id or context.source_id,
                idempotency_key=f"read-api-{uuid4().hex}",
                event_type=event_type,
                payload={"order_id": f"ORD-{uuid4().hex}"},
                request_hash="a" * 64,
                status=status,
                attempt_count=0,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        for index in range(history_entries):
            session.add(
                EventStatusHistory(
                    event_id=identifier,
                    status=status,
                    message=f"internal history message {index}",
                    created_at=timestamp + timedelta(microseconds=index),
                )
            )
    return identifier


def create_attempt(
    context: ReadApiContext,
    event_id: UUID,
    attempt_number: int,
    *,
    outcome: str | None = None,
    error: str | None = None,
) -> UUID:
    attempt_id = uuid4()
    started_at = datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc) + timedelta(
        minutes=attempt_number
    )
    with context.session_factory.begin() as session:
        session.add(
            EventProcessingAttempt(
                id=attempt_id,
                event_id=event_id,
                attempt_number=attempt_number,
                lease_token=uuid4(),
                started_at=started_at,
                finished_at=(
                    started_at + timedelta(seconds=1)
                    if outcome is not None
                    else None
                ),
                outcome=outcome,
                error=error,
            )
        )
    return attempt_id


def test_listing_combines_filters_with_inclusive_and_exclusive_boundaries(
    read_api_context: ReadApiContext,
) -> None:
    start = datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)
    at_start = create_event(
        read_api_context,
        status="failed",
        created_at=start,
    )
    inside = create_event(
        read_api_context,
        status="failed",
        created_at=start + timedelta(minutes=30),
    )
    create_event(read_api_context, status="failed", created_at=end)
    create_event(
        read_api_context,
        event_type="payment.failed",
        status="failed",
        created_at=start + timedelta(minutes=10),
    )
    create_event(
        read_api_context,
        source_id=read_api_context.foreign_source_id,
        status="failed",
        created_at=start + timedelta(minutes=20),
    )

    with TestClient(app) as client:
        response = client.get(
            "/v1/events",
            params={
                "status": "failed",
                "event_type": "order.created",
                "created_from": "2026-11-04T10:00:00+05:00",
                "created_to": "2026-11-04T11:00:00+05:00",
            },
            headers=read_api_context.headers,
        )

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert [item["id"] for item in response.json()["items"]] == [
        str(inside),
        str(at_start),
    ]


def test_readiness_uses_real_postgresql_compatibility_state(
    read_api_context: ReadApiContext,
) -> None:
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_listing_ordering_and_pagination_are_deterministic(
    read_api_context: ReadApiContext,
) -> None:
    timestamp = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
    high = create_event(
        read_api_context,
        event_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        created_at=timestamp,
    )
    low = create_event(
        read_api_context,
        event_id=UUID("00000000-0000-0000-0000-000000000001"),
        created_at=timestamp,
    )

    with TestClient(app) as client:
        first = client.get(
            "/v1/events",
            params={"limit": 1, "offset": 0},
            headers=read_api_context.headers,
        )
        second = client.get(
            "/v1/events",
            params={"limit": 1, "offset": 1},
            headers=read_api_context.headers,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["total"] == 2
    assert second.json()["total"] == 2
    assert first.json()["items"][0]["id"] == str(high)
    assert second.json()["items"][0]["id"] == str(low)


@pytest.mark.parametrize(
    "parameters",
    [
        {"created_from": "2026-11-04T05:00:00"},
        {"created_to": "malformed"},
        {
            "created_from": "2026-11-04T05:00:00Z",
            "created_to": "2026-11-04T10:00:00+05:00",
        },
        {
            "created_from": "2026-11-04T06:00:00Z",
            "created_to": "2026-11-04T05:00:00Z",
        },
        {"event_type": "Order.Created"},
    ],
)
def test_listing_rejects_invalid_filter_arguments(
    parameters: dict[str, str],
    read_api_context: ReadApiContext,
) -> None:
    with TestClient(app) as client:
        response = client.get(
            "/v1/events",
            params=parameters,
            headers=read_api_context.headers,
        )

    assert response.status_code == 422


def test_event_detail_uses_real_postgresql_source_isolation(
    read_api_context: ReadApiContext,
) -> None:
    owned_event = create_event(read_api_context)
    foreign_event = create_event(
        read_api_context,
        source_id=read_api_context.foreign_source_id,
    )

    with TestClient(app) as client:
        owned = client.get(
            f"/v1/events/{owned_event}",
            headers=read_api_context.headers,
        )
        foreign = client.get(
            f"/v1/events/{foreign_event}",
            headers=read_api_context.headers,
        )
        missing = client.get(
            f"/v1/events/{uuid4()}",
            headers=read_api_context.headers,
        )

    assert owned.status_code == 200
    assert owned.json()["id"] == str(owned_event)
    assert owned.json()["source_id"] == read_api_context.source_id
    assert "payload" in owned.json()
    assert "last_error" not in owned.text
    assert "lease_token" not in owned.text
    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "Event not found"}


def test_event_history_uses_real_postgresql_ordering_and_pagination(
    read_api_context: ReadApiContext,
) -> None:
    event_id = create_event(
        read_api_context,
        status="completed",
        history_entries=0,
    )
    timestamp = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
    oldest_id = uuid4()
    tied_low_id = UUID("00000000-0000-0000-0000-000000000011")
    tied_high_id = UUID("ffffffff-ffff-ffff-ffff-fffffffffff1")
    with read_api_context.session_factory.begin() as session:
        session.add_all(
            [
                EventStatusHistory(
                    id=oldest_id,
                    event_id=event_id,
                    status="pending",
                    message="secret oldest message",
                    created_at=timestamp - timedelta(seconds=1),
                ),
                EventStatusHistory(
                    id=tied_low_id,
                    event_id=event_id,
                    status="processing",
                    message="secret processing message",
                    created_at=timestamp,
                ),
                EventStatusHistory(
                    id=tied_high_id,
                    event_id=event_id,
                    status="completed",
                    message="secret completion message",
                    created_at=timestamp,
                ),
            ]
        )

    with TestClient(app) as client:
        response = client.get(
            f"/v1/events/{event_id}/history",
            params={"limit": 2, "offset": 1},
            headers=read_api_context.headers,
        )

    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert response.json()["limit"] == 2
    assert response.json()["offset"] == 1
    assert [item["id"] for item in response.json()["items"]] == [
        str(tied_low_id),
        str(tied_high_id),
    ]
    assert "secret" not in response.text
    assert "message" not in response.text


def test_attempt_api_returns_empty_page_independent_of_status_history(
    read_api_context: ReadApiContext,
) -> None:
    event_id = create_event(read_api_context, history_entries=3)

    with TestClient(app) as client:
        response = client.get(
            f"/v1/events/{event_id}/attempts",
            headers=read_api_context.headers,
        )

    assert response.status_code == 200
    assert response.json() == {
        "event_id": str(event_id),
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }


def test_attempt_api_serializes_safely_in_descending_pages(
    read_api_context: ReadApiContext,
) -> None:
    event_id = create_event(read_api_context, status="failed")
    secret = "postgres-password=never-return-this"
    active_id = create_attempt(read_api_context, event_id, 3)
    failed_id = create_attempt(
        read_api_context,
        event_id,
        2,
        outcome="permanent_failure",
        error=secret,
    )
    completed_id = create_attempt(
        read_api_context,
        event_id,
        1,
        outcome="completed",
    )

    with TestClient(app) as client:
        first_page = client.get(
            f"/v1/events/{event_id}/attempts",
            params={"limit": 2, "offset": 0},
            headers=read_api_context.headers,
        )
        second_page = client.get(
            f"/v1/events/{event_id}/attempts",
            params={"limit": 2, "offset": 2},
            headers=read_api_context.headers,
        )

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert first_page.json()["total"] == 3
    assert second_page.json()["total"] == 3
    assert [item["id"] for item in first_page.json()["items"]] == [
        str(active_id),
        str(failed_id),
    ]
    assert first_page.json()["items"][0]["finished_at"] is None
    assert first_page.json()["items"][0]["outcome"] is None
    assert first_page.json()["items"][0]["error_summary"] is None
    assert (
        first_page.json()["items"][1]["error_summary"]
        == "Permanent processing failure"
    )
    assert second_page.json()["items"][0]["id"] == str(completed_id)
    assert second_page.json()["items"][0]["error_summary"] is None
    combined_json = first_page.text + second_page.text
    assert secret not in combined_json
    assert "lease_token" not in combined_json


def test_attempt_api_hides_foreign_events_like_missing_events(
    read_api_context: ReadApiContext,
) -> None:
    foreign_event = create_event(
        read_api_context,
        source_id=read_api_context.foreign_source_id,
    )

    with TestClient(app) as client:
        foreign = client.get(
            f"/v1/events/{foreign_event}/attempts",
            headers=read_api_context.headers,
        )
        missing = client.get(
            f"/v1/events/{uuid4()}/attempts",
            headers=read_api_context.headers,
        )

    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "Event not found"}


def test_attempt_api_requires_authentication(
    read_api_context: ReadApiContext,
) -> None:
    event_id = create_event(read_api_context)

    with TestClient(app) as client:
        missing = client.get(f"/v1/events/{event_id}/attempts")
        invalid = client.get(
            f"/v1/events/{event_id}/attempts",
            headers={"X-API-Key": "invalid"},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401


@pytest.mark.parametrize(
    "parameters",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"limit": "invalid"},
    ],
)
def test_attempt_api_rejects_invalid_pagination(
    parameters: dict[str, object],
    read_api_context: ReadApiContext,
) -> None:
    event_id = create_event(read_api_context)

    with TestClient(app) as client:
        response = client.get(
            f"/v1/events/{event_id}/attempts",
            params=parameters,
            headers=read_api_context.headers,
        )

    assert response.status_code == 422
