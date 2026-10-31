import os
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier
from uuid import uuid4

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
from app.models import Event, EventStatusHistory
from app.services.events import compute_request_hash
from scripts.prepare_test_db import (
    create_test_database_engine,
    load_test_database_settings,
    validate_test_database_settings,
    verify_test_database_connection,
)

RUN_PG_INTEGRATION = os.getenv("EVENTLEDGER_RUN_PG_INTEGRATION") == "1"
TEST_API_KEY = "pg-integration-api-key-not-a-secret"

pytestmark = [
    pytest.mark.pg_integration,
    pytest.mark.skipif(
        not RUN_PG_INTEGRATION,
        reason="Set EVENTLEDGER_RUN_PG_INTEGRATION=1 to run PostgreSQL tests.",
    ),
]


@dataclass(frozen=True)
class IntegrationContext:
    engine: Engine
    session_factory: sessionmaker[Session]
    source_id: str
    api_key: str

    def headers(self, idempotency_key: str) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Idempotency-Key": idempotency_key,
        }


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
                events_table, history_table = connection.execute(
                    text(
                        "SELECT to_regclass('public.events'), "
                        "to_regclass('public.event_status_history')"
                    )
                ).one()
                if events_table is None or history_table is None:
                    pytest.fail(
                        "Test schema is missing; run "
                        "python -m scripts.prepare_test_db."
                    )
        except (RuntimeError, SQLAlchemyError):
            pytest.fail(
                "Unable to verify the isolated PostgreSQL test database."
            )

        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def integration_context(
    pg_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[IntegrationContext, None, None]:
    source_id = f"pg-integration-{uuid4().hex}"
    session_factory = sessionmaker(
        bind=pg_engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )

    monkeypatch.setattr(
        app_settings,
        "eventledger_api_key",
        SecretStr(TEST_API_KEY),
    )
    monkeypatch.setattr(app_settings, "eventledger_source_id", source_id)

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
    context = IntegrationContext(
        engine=pg_engine,
        session_factory=session_factory,
        source_id=source_id,
        api_key=TEST_API_KEY,
    )

    yield context

    app.dependency_overrides.pop(get_db_session, None)
    with pg_engine.begin() as connection:
        verify_test_database_connection(connection)
        event_ids = select(Event.id).where(Event.source_id == source_id)
        connection.execute(
            delete(EventStatusHistory).where(
                EventStatusHistory.event_id.in_(event_ids)
            )
        )
        connection.execute(delete(Event).where(Event.source_id == source_id))


def event_rows(
    context: IntegrationContext,
    idempotency_key: str,
) -> list[Event]:
    with context.session_factory() as session:
        return list(
            session.scalars(
                select(Event).where(
                    Event.source_id == context.source_id,
                    Event.idempotency_key == idempotency_key,
                )
            )
        )


def history_count(context: IntegrationContext, event_id) -> int:
    with context.session_factory() as session:
        return len(
            list(
                session.scalars(
                    select(EventStatusHistory.id).where(
                        EventStatusHistory.event_id == event_id
                    )
                )
            )
        )


def concurrent_posts(
    context: IntegrationContext,
    idempotency_key: str,
    bodies: list[dict],
):
    barrier = Barrier(len(bodies))

    def submit(body: dict):
        with TestClient(app) as client:
            barrier.wait(timeout=15)
            return client.post(
                "/v1/events",
                headers=context.headers(idempotency_key),
                json=body,
            )

    with ThreadPoolExecutor(max_workers=len(bodies)) as executor:
        futures = [executor.submit(submit, body) for body in bodies]
        return [future.result(timeout=30) for future in futures]


def test_ten_simultaneous_identical_requests_create_one_event(
    integration_context: IntegrationContext,
) -> None:
    key = f"identical-{uuid4().hex}"
    body = {
        "event_type": "order.created",
        "payload": {"order_id": uuid4().hex, "amount": 42.5},
    }

    responses = concurrent_posts(integration_context, key, [body] * 10)

    assert [response.status_code for response in responses].count(201) == 1
    assert [response.status_code for response in responses].count(200) == 9
    assert all(response.status_code != 500 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1

    events = event_rows(integration_context, key)
    assert len(events) == 1
    assert history_count(integration_context, events[0].id) == 1


def test_simultaneous_conflicting_payloads_keep_one_winner(
    integration_context: IntegrationContext,
) -> None:
    key = f"conflict-{uuid4().hex}"
    first_body = {
        "event_type": "payment.received",
        "payload": {"payment_id": uuid4().hex, "amount": 10},
    }
    second_body = {
        "event_type": "payment.received",
        "payload": {"payment_id": uuid4().hex, "amount": 20},
    }
    bodies = [first_body] * 5 + [second_body] * 5

    responses = concurrent_posts(integration_context, key, bodies)
    status_codes = [response.status_code for response in responses]

    assert status_codes.count(201) == 1
    assert status_codes.count(200) == 4
    assert status_codes.count(409) == 5
    assert 500 not in status_codes

    events = event_rows(integration_context, key)
    assert len(events) == 1
    assert history_count(integration_context, events[0].id) == 1

    winning_hash = events[0].request_hash
    for body, response in zip(bodies, responses, strict=True):
        request_hash = compute_request_hash(body["event_type"], body["payload"])
        if response.status_code == 201:
            assert request_hash == winning_hash
        elif request_hash == winning_hash:
            assert response.status_code == 200
        else:
            assert response.status_code == 409


def test_different_keys_create_independent_events(
    integration_context: IntegrationContext,
) -> None:
    keys = [f"independent-{uuid4().hex}" for _ in range(3)]
    body = {
        "event_type": "order.created",
        "payload": {"order_id": uuid4().hex},
    }

    with TestClient(app) as client:
        responses = [
            client.post(
                "/v1/events",
                headers=integration_context.headers(key),
                json=body,
            )
            for key in keys
        ]

    assert all(response.status_code == 201 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 3
    assert all(len(event_rows(integration_context, key)) == 1 for key in keys)


def test_reordered_json_is_replayed_as_same_event(
    integration_context: IntegrationContext,
) -> None:
    key = f"reordered-{uuid4().hex}"
    first_body = {
        "event_type": "order.created",
        "payload": {
            "order": {"id": uuid4().hex, "paid": True},
            "items": [{"sku": "item-1", "quantity": 2}],
        },
    }
    second_body = {
        "payload": {
            "items": [{"quantity": 2, "sku": "item-1"}],
            "order": {
                "paid": True,
                "id": first_body["payload"]["order"]["id"],
            },
        },
        "event_type": "order.created",
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/events",
            headers=integration_context.headers(key),
            json=first_body,
        )
        second = client.post(
            "/v1/events",
            headers=integration_context.headers(key),
            json=second_body,
        )

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    events = event_rows(integration_context, key)
    assert len(events) == 1
    assert history_count(integration_context, events[0].id) == 1


def test_conflicting_request_does_not_modify_existing_event(
    integration_context: IntegrationContext,
) -> None:
    key = f"unchanged-{uuid4().hex}"
    original_body = {
        "event_type": "order.created",
        "payload": {"order_id": uuid4().hex, "amount": 10},
    }
    conflicting_body = {
        "event_type": "order.created",
        "payload": {"order_id": original_body["payload"]["order_id"], "amount": 99},
    }

    with TestClient(app) as client:
        created = client.post(
            "/v1/events",
            headers=integration_context.headers(key),
            json=original_body,
        )
        before = event_rows(integration_context, key)[0]
        before_state = {
            "id": before.id,
            "payload": before.payload,
            "request_hash": before.request_hash,
            "status": before.status,
            "attempt_count": before.attempt_count,
            "updated_at": before.updated_at,
        }
        conflict = client.post(
            "/v1/events",
            headers=integration_context.headers(key),
            json=conflicting_body,
        )

    after = event_rows(integration_context, key)[0]
    after_state = {
        "id": after.id,
        "payload": after.payload,
        "request_hash": after.request_hash,
        "status": after.status,
        "attempt_count": after.attempt_count,
        "updated_at": after.updated_at,
    }

    assert created.status_code == 201
    assert conflict.status_code == 409
    assert after_state == before_state
    assert history_count(integration_context, after.id) == 1


def test_authentication_is_required_before_database_write(
    integration_context: IntegrationContext,
) -> None:
    key = f"unauthorized-{uuid4().hex}"
    body = {
        "event_type": "order.created",
        "payload": {"order_id": uuid4().hex},
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/events",
            headers={"Idempotency-Key": key},
            json=body,
        )

    assert response.status_code == 401
    assert event_rows(integration_context, key) == []


def test_history_failure_rolls_back_actual_event_insert(
    integration_context: IntegrationContext,
) -> None:
    key = f"rollback-{uuid4().hex}"

    class FailingHistorySession(Session):
        def add(self, instance, *args, **kwargs) -> None:
            if isinstance(instance, EventStatusHistory):
                raise SQLAlchemyError("simulated history persistence failure")
            super().add(instance, *args, **kwargs)

    failing_factory = sessionmaker(
        bind=integration_context.engine,
        class_=FailingHistorySession,
        autoflush=False,
        expire_on_commit=False,
    )

    def failing_dependency() -> Generator[Session, None, None]:
        session = failing_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    previous_override = app.dependency_overrides[get_db_session]
    app.dependency_overrides[get_db_session] = failing_dependency
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/events",
                headers=integration_context.headers(key),
                json={
                    "event_type": "order.created",
                    "payload": {"order_id": uuid4().hex},
                },
            )
    finally:
        app.dependency_overrides[get_db_session] = previous_override

    assert response.status_code == 500
    assert event_rows(integration_context, key) == []

