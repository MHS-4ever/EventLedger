import os
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql.sqltypes import DateTime

from app.models import (
    Event,
    EventManualRetryRequest,
    EventProcessingAttempt,
    OrderFulfillment,
)
from scripts.prepare_test_db import (
    create_test_database_engine,
    load_test_database_settings,
    validate_test_database_settings,
    verify_test_database_connection,
)

RUN_PG_INTEGRATION = os.getenv("EVENTLEDGER_RUN_PG_INTEGRATION") == "1"
EXPECTED_REVISION = "d4e5f6a7b8c9"

pytestmark = [
    pytest.mark.pg_integration,
    pytest.mark.skipif(
        not RUN_PG_INTEGRATION,
        reason="Set EVENTLEDGER_RUN_PG_INTEGRATION=1 to run PostgreSQL tests.",
    ),
]


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
        except (RuntimeError, SQLAlchemyError):
            pytest.fail(
                "Unable to verify the isolated PostgreSQL test database."
            )

        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_connection(pg_engine: Engine) -> Generator[Connection, None, None]:
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        try:
            verify_test_database_connection(connection)
            yield connection
        finally:
            if transaction.is_active:
                transaction.rollback()


def constraint_map(connection: Connection, table_name: str) -> dict[str, str]:
    return {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspect(connection).get_check_constraints(table_name)
    }


def unique_constraint_map(
    connection: Connection,
    table_name: str,
) -> dict[str, tuple[str, ...]]:
    return {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspect(connection).get_unique_constraints(table_name)
    }


def assert_integrity_error(connection: Connection, statement: object) -> None:
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(statement)


def event_values(
    *,
    status: str = "pending",
    lease_token: UUID | None = None,
    lease_expires_at: datetime | None = None,
) -> dict[str, object]:
    unique_value = uuid4().hex
    return {
        "id": uuid4(),
        "source_id": f"worker-schema-{unique_value}",
        "idempotency_key": unique_value,
        "event_type": "order.created",
        "payload": {"order_id": unique_value},
        "request_hash": "0" * 64,
        "status": status,
        "attempt_count": 0,
        "lease_token": lease_token,
        "lease_expires_at": lease_expires_at,
    }


def insert_parent_event(connection: Connection) -> UUID:
    event_id = uuid4()
    values = event_values()
    values["id"] = event_id
    connection.execute(Event.__table__.insert().values(**values))
    return event_id


def attempt_values(
    event_id: UUID,
    *,
    attempt_number: int = 1,
    lease_token: UUID | None = None,
    finished_at: datetime | None = None,
    outcome: str | None = None,
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "event_id": event_id,
        "attempt_number": attempt_number,
        "lease_token": lease_token or uuid4(),
        "finished_at": finished_at,
        "outcome": outcome,
    }


def fulfillment_values(
    event_id: UUID,
    *,
    source_id: str | None = None,
    order_id: str | None = None,
) -> dict[str, object]:
    return {
        "source_id": source_id or f"source-{uuid4().hex}",
        "order_id": order_id if order_id is not None else f"order-{uuid4().hex}",
        "first_event_id": event_id,
    }


def index_by_name(connection: Connection, table_name: str) -> dict[str, dict]:
    return {
        index["name"]: index for index in inspect(connection).get_indexes(table_name)
    }


def table_names(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names(schema="public"))


def test_migrated_schema_revision_tables_and_event_columns(
    db_connection: Connection,
) -> None:
    revision = db_connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == EXPECTED_REVISION
    assert {
        "events",
        "event_manual_retry_requests",
        "event_status_history",
        "event_processing_attempts",
        "order_fulfillments",
    } <= table_names(db_connection)

    columns = {
        column["name"]: column
        for column in inspect(db_connection).get_columns("events")
    }

    next_attempt_at = columns["next_attempt_at"]
    assert isinstance(next_attempt_at["type"], DateTime)
    assert next_attempt_at["type"].timezone
    assert not next_attempt_at["nullable"]
    assert "now()" in str(next_attempt_at["default"]).lower()

    assert isinstance(columns["lease_token"]["type"], PG_UUID)
    assert columns["lease_token"]["nullable"]
    assert isinstance(columns["lease_expires_at"]["type"], DateTime)
    assert columns["lease_expires_at"]["type"].timezone
    assert columns["lease_expires_at"]["nullable"]
    assert columns["attempt_limit_override"]["nullable"]

    existing_values = event_values()
    db_connection.execute(Event.__table__.insert().values(**existing_values))
    assert db_connection.scalar(
        select(Event.attempt_limit_override).where(
            Event.id == existing_values["id"]
        )
    ) is None


def test_migrated_event_indexes_and_constraints(
    db_connection: Connection,
) -> None:
    indexes = index_by_name(db_connection, "events")
    pending_index = indexes["ix_events_pending_claim"]
    expired_index = indexes["ix_events_expired_lease"]

    assert tuple(pending_index["column_names"]) == (
        "next_attempt_at",
        "created_at",
        "id",
    )
    pending_predicate = str(
        pending_index["dialect_options"]["postgresql_where"]
    ).lower()
    assert "status" in pending_predicate
    assert "pending" in pending_predicate

    assert tuple(expired_index["column_names"]) == ("lease_expires_at", "id")
    expired_predicate = str(
        expired_index["dialect_options"]["postgresql_where"]
    ).lower()
    assert "status" in expired_predicate
    assert "processing" in expired_predicate

    checks = constraint_map(db_connection, "events")
    assert "ck_events_status_allowed" in checks
    assert "ck_events_attempt_count_nonnegative" in checks
    assert "ck_events_attempt_limit_override_valid" in checks
    assert "ck_events_lease_state_consistent" in checks
    assert "uq_events_source_id_idempotency_key" in unique_constraint_map(
        db_connection,
        "events",
    )
    assert "ix_events_status_created_at" in indexes

    history_checks = constraint_map(db_connection, "event_status_history")
    assert "ck_event_status_history_status_allowed" in history_checks
    history_foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspect(db_connection).get_foreign_keys(
            "event_status_history"
        )
    }
    assert history_foreign_keys[
        "fk_event_status_history_event_id_events"
    ]["referred_table"] == "events"
    assert "ix_event_status_history_event_id_created_at" in index_by_name(
        db_connection,
        "event_status_history",
    )


def test_migrated_manual_retry_request_table_schema(
    db_connection: Connection,
) -> None:
    inspector = inspect(db_connection)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("event_manual_retry_requests")
    }

    assert isinstance(columns["id"]["type"], PG_UUID)
    assert isinstance(columns["event_id"]["type"], PG_UUID)
    assert not columns["event_id"]["nullable"]
    assert columns["idempotency_key"]["type"].length == 255
    assert not columns["idempotency_key"]["nullable"]
    assert not columns["target_attempt_number"]["nullable"]
    assert isinstance(columns["accepted_at"]["type"], DateTime)
    assert columns["accepted_at"]["type"].timezone
    assert not columns["accepted_at"]["nullable"]
    assert "now()" in str(columns["accepted_at"]["default"]).lower()

    foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys(
            "event_manual_retry_requests"
        )
    }
    foreign_key = foreign_keys[
        "fk_event_manual_retry_requests_event_id_events"
    ]
    assert foreign_key["referred_table"] == "events"
    assert tuple(foreign_key["constrained_columns"]) == ("event_id",)

    unique_constraints = unique_constraint_map(
        db_connection,
        "event_manual_retry_requests",
    )
    assert unique_constraints[
        "uq_event_manual_retry_requests_event_id"
    ] == ("event_id",)

    checks = constraint_map(db_connection, "event_manual_retry_requests")
    assert (
        "ck_event_manual_retry_requests_idempotency_key_valid" in checks
    )
    assert (
        "ck_event_manual_retry_requests_target_attempt_number_positive"
        in checks
    )


def test_manual_retry_constraints_are_enforced(
    db_connection: Connection,
) -> None:
    requests = EventManualRetryRequest.__table__
    event_id = insert_parent_event(db_connection)
    db_connection.execute(
        requests.insert().values(
            id=uuid4(),
            event_id=event_id,
            idempotency_key="retry-request-1",
            target_attempt_number=1,
        )
    )

    assert_integrity_error(
        db_connection,
        requests.insert().values(
            id=uuid4(),
            event_id=event_id,
            idempotency_key="retry-request-2",
            target_attempt_number=2,
        ),
    )

    for invalid_key in ("", "contains space", "contains\ttab"):
        other_event_id = insert_parent_event(db_connection)
        assert_integrity_error(
            db_connection,
            requests.insert().values(
                id=uuid4(),
                event_id=other_event_id,
                idempotency_key=invalid_key,
                target_attempt_number=1,
            ),
        )

    for invalid_target in (0, -1):
        other_event_id = insert_parent_event(db_connection)
        assert_integrity_error(
            db_connection,
            requests.insert().values(
                id=uuid4(),
                event_id=other_event_id,
                idempotency_key=f"retry-{uuid4().hex}",
                target_attempt_number=invalid_target,
            ),
        )

    assert_integrity_error(
        db_connection,
        requests.insert().values(
            id=uuid4(),
            event_id=uuid4(),
            idempotency_key="missing-parent",
            target_attempt_number=1,
        ),
    )


def test_event_attempt_limit_override_constraint_is_enforced(
    db_connection: Connection,
) -> None:
    events = Event.__table__
    for invalid_limit in (0, -1):
        assert_integrity_error(
            db_connection,
            events.insert().values(
                **event_values(),
                attempt_limit_override=invalid_limit,
            ),
        )

    values = event_values()
    values["attempt_count"] = 3
    assert_integrity_error(
        db_connection,
        events.insert().values(
            **values,
            attempt_limit_override=2,
        ),
    )

    db_connection.execute(
        events.insert().values(
            **event_values(),
            attempt_limit_override=1,
        )
    )


def test_migrated_attempt_table_schema(db_connection: Connection) -> None:
    inspector = inspect(db_connection)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("event_processing_attempts")
    }
    assert isinstance(columns["id"]["type"], PG_UUID)
    assert isinstance(columns["event_id"]["type"], PG_UUID)
    assert not columns["event_id"]["nullable"]
    assert isinstance(columns["lease_token"]["type"], PG_UUID)
    assert not columns["lease_token"]["nullable"]
    assert isinstance(columns["started_at"]["type"], DateTime)
    assert columns["started_at"]["type"].timezone
    assert "now()" in str(columns["started_at"]["default"]).lower()
    assert isinstance(columns["finished_at"]["type"], DateTime)
    assert columns["finished_at"]["type"].timezone
    assert columns["finished_at"]["nullable"]
    assert columns["outcome"]["type"].length == 32
    assert columns["outcome"]["nullable"]

    foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys("event_processing_attempts")
    }
    assert foreign_keys[
        "fk_event_processing_attempts_event_id_events"
    ]["referred_table"] == "events"
    assert tuple(
        foreign_keys["fk_event_processing_attempts_event_id_events"][
            "constrained_columns"
        ]
    ) == ("event_id",)

    unique_constraints = unique_constraint_map(
        db_connection,
        "event_processing_attempts",
    )
    assert unique_constraints[
        "uq_event_processing_attempts_event_id_attempt_number"
    ] == ("event_id", "attempt_number")
    assert unique_constraints["uq_event_processing_attempts_lease_token"] == (
        "lease_token",
    )

    checks = constraint_map(db_connection, "event_processing_attempts")
    assert "ck_event_processing_attempts_attempt_number_positive" in checks
    assert "ck_event_processing_attempts_outcome_allowed" in checks
    assert "ck_event_processing_attempts_completion_fields_consistent" in checks


def test_migrated_fulfillment_table_schema(db_connection: Connection) -> None:
    inspector = inspect(db_connection)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("order_fulfillments")
    }
    assert columns["source_id"]["type"].length == 100
    assert not columns["source_id"]["nullable"]
    assert columns["order_id"]["type"].length == 255
    assert not columns["order_id"]["nullable"]
    assert isinstance(columns["first_event_id"]["type"], PG_UUID)
    assert not columns["first_event_id"]["nullable"]
    assert isinstance(columns["fulfilled_at"]["type"], DateTime)
    assert columns["fulfilled_at"]["type"].timezone
    assert "now()" in str(columns["fulfilled_at"]["default"]).lower()

    primary_key = inspector.get_pk_constraint("order_fulfillments")
    assert primary_key["name"] == "pk_order_fulfillments"
    assert tuple(primary_key["constrained_columns"]) == ("source_id", "order_id")

    unique_constraints = unique_constraint_map(
        db_connection,
        "order_fulfillments",
    )
    assert unique_constraints["uq_order_fulfillments_first_event_id"] == (
        "first_event_id",
    )
    foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys("order_fulfillments")
    }
    assert foreign_keys[
        "fk_order_fulfillments_first_event_id_events"
    ]["referred_table"] == "events"
    assert tuple(
        foreign_keys["fk_order_fulfillments_first_event_id_events"][
            "constrained_columns"
        ]
    ) == ("first_event_id",)
    checks = constraint_map(db_connection, "order_fulfillments")
    assert "ck_order_fulfillments_order_id_valid" in checks


def test_event_lease_constraint_enforcement(db_connection: Connection) -> None:
    events = Event.__table__

    pending_values = event_values()
    db_connection.execute(events.insert().values(**pending_values))
    next_attempt_at = db_connection.scalar(
        text("SELECT next_attempt_at FROM events WHERE id = :event_id"),
        {"event_id": pending_values["id"]},
    )
    assert next_attempt_at is not None
    assert_integrity_error(
        db_connection,
        events.insert().values(**event_values(status="processing")),
    )

    lease_token = uuid4()
    lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db_connection.execute(
        events.insert().values(
            **event_values(
                status="processing",
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
            )
        )
    )

    for status in ("completed", "failed"):
        assert_integrity_error(
            db_connection,
            events.insert().values(
                **event_values(
                    status=status,
                    lease_token=uuid4(),
                    lease_expires_at=lease_expires_at,
                )
            ),
        )


def test_attempt_constraints_are_enforced(db_connection: Connection) -> None:
    attempts = EventProcessingAttempt.__table__
    event_id = insert_parent_event(db_connection)
    baseline_lease_token = uuid4()
    db_connection.execute(
        attempts.insert().values(
            **attempt_values(event_id, lease_token=baseline_lease_token)
        )
    )

    for invalid_attempt_number in (0, -1):
        assert_integrity_error(
            db_connection,
            attempts.insert().values(
                **attempt_values(
                    event_id,
                    attempt_number=invalid_attempt_number,
                )
            ),
        )

    assert_integrity_error(
        db_connection,
        attempts.insert().values(
            **attempt_values(event_id, attempt_number=1)
        ),
    )
    assert_integrity_error(
        db_connection,
        attempts.insert().values(
            **attempt_values(
                event_id,
                attempt_number=2,
                lease_token=baseline_lease_token,
            )
        ),
    )
    assert_integrity_error(
        db_connection,
        attempts.insert().values(
            **attempt_values(
                event_id,
                attempt_number=2,
                finished_at=datetime.now(timezone.utc),
                outcome="invalid_outcome",
            )
        ),
    )
    assert_integrity_error(
        db_connection,
        attempts.insert().values(
            **attempt_values(
                event_id,
                attempt_number=2,
                finished_at=datetime.now(timezone.utc),
            )
        ),
    )
    assert_integrity_error(
        db_connection,
        attempts.insert().values(
            **attempt_values(
                event_id,
                attempt_number=2,
                outcome="completed",
            )
        ),
    )


@pytest.mark.parametrize(
    "invalid_order_id",
    ["", "   ", " leading", "trailing ", "\tleading", "trailing\n"],
)
def test_fulfillment_order_id_constraint_is_enforced(
    db_connection: Connection,
    invalid_order_id: str,
) -> None:
    event_id = insert_parent_event(db_connection)
    statement = OrderFulfillment.__table__.insert().values(
        **fulfillment_values(event_id, order_id=invalid_order_id)
    )
    assert_integrity_error(db_connection, statement)


def test_fulfillment_uniqueness_and_source_scope(
    db_connection: Connection,
) -> None:
    fulfillments = OrderFulfillment.__table__
    source_id = f"source-{uuid4().hex}"
    other_source_id = f"source-{uuid4().hex}"
    order_id = f"order-{uuid4().hex}"

    first_event_id = insert_parent_event(db_connection)
    other_source_event_id = insert_parent_event(db_connection)
    duplicate_business_event_id = insert_parent_event(db_connection)

    db_connection.execute(
        fulfillments.insert().values(
            **fulfillment_values(
                first_event_id,
                source_id=source_id,
                order_id=order_id,
            )
        )
    )
    db_connection.execute(
        fulfillments.insert().values(
            **fulfillment_values(
                other_source_event_id,
                source_id=other_source_id,
                order_id=order_id,
            )
        )
    )

    assert_integrity_error(
        db_connection,
        fulfillments.insert().values(
            **fulfillment_values(
                duplicate_business_event_id,
                source_id=source_id,
                order_id=order_id,
            )
        ),
    )
    assert_integrity_error(
        db_connection,
        fulfillments.insert().values(
            **fulfillment_values(
                first_event_id,
                source_id=source_id,
                order_id=f"order-{uuid4().hex}",
            )
        ),
    )
