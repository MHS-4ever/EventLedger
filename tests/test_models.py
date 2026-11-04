from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

from app.db.base import Base
from app.models import (
    Event,
    EventManualRetryRequest,
    EventProcessingAttempt,
    EventStatusHistory,
    OrderFulfillment,
)


def test_model_tables_are_registered() -> None:
    assert Event.__table__ is Base.metadata.tables["events"]
    assert EventManualRetryRequest.__table__ is Base.metadata.tables[
        "event_manual_retry_requests"
    ]
    assert EventStatusHistory.__table__ is Base.metadata.tables[
        "event_status_history"
    ]
    assert EventProcessingAttempt.__table__ is Base.metadata.tables[
        "event_processing_attempts"
    ]
    assert OrderFulfillment.__table__ is Base.metadata.tables[
        "order_fulfillments"
    ]


def test_event_constraints_and_index() -> None:
    table = Event.__table__
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    check_expressions = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    indexes = {index.name: index for index in table.indexes}
    index_columns = {
        tuple(column.name for column in index.columns) for index in indexes.values()
    }

    assert ("source_id", "idempotency_key") in unique_columns
    assert any("status IN" in expression for expression in check_expressions)
    assert "attempt_count >= 0" in check_expressions
    assert any(
        "attempt_limit_override > 0" in expression
        and "attempt_limit_override >= attempt_count" in expression
        for expression in check_expressions
    )
    assert any(
        "lease_token IS NOT NULL" in expression
        and "lease_expires_at IS NOT NULL" in expression
        and "lease_token IS NULL" in expression
        and "lease_expires_at IS NULL" in expression
        for expression in check_expressions
    )
    assert ("status", "created_at") in index_columns
    assert tuple(
        column.name for column in indexes["ix_events_pending_claim"].columns
    ) == ("next_attempt_at", "created_at", "id")
    assert str(
        indexes["ix_events_pending_claim"].dialect_options["postgresql"]["where"]
    ) == "status = 'pending'"
    assert tuple(
        column.name for column in indexes["ix_events_expired_lease"].columns
    ) == ("lease_expires_at", "id")
    assert str(
        indexes["ix_events_expired_lease"].dialect_options["postgresql"]["where"]
    ) == "status = 'processing'"


def test_event_important_columns() -> None:
    columns = Event.__table__.c

    assert columns.id.primary_key
    assert isinstance(columns.id.type, PG_UUID)
    assert columns.source_id.type.length == 100
    assert columns.idempotency_key.type.length == 255
    assert columns.event_type.type.length == 100
    assert isinstance(columns.payload.type, JSONB)
    assert not columns.payload.nullable
    assert columns.request_hash.type.length == 64
    assert not columns.status.nullable
    assert not columns.attempt_count.nullable
    assert columns.attempt_limit_override.nullable
    assert columns.last_error.nullable
    assert columns.next_attempt_at.type.timezone
    assert not columns.next_attempt_at.nullable
    assert columns.next_attempt_at.server_default is not None
    assert isinstance(columns.lease_token.type, PG_UUID)
    assert columns.lease_token.nullable
    assert columns.lease_expires_at.type.timezone
    assert columns.lease_expires_at.nullable
    assert columns.created_at.type.timezone
    assert columns.created_at.server_default is not None
    assert columns.updated_at.type.timezone
    assert columns.updated_at.server_default is not None
    assert columns.updated_at.onupdate is not None


def test_event_history_columns_constraints_and_index() -> None:
    table = EventStatusHistory.__table__
    foreign_key = next(iter(table.c.event_id.foreign_keys))
    check_expressions = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    index_columns = {
        tuple(column.name for column in index.columns) for index in table.indexes
    }

    assert table.c.id.primary_key
    assert isinstance(table.c.id.type, PG_UUID)
    assert foreign_key.target_fullname == "events.id"
    assert not table.c.event_id.nullable
    assert any("status IN" in expression for expression in check_expressions)
    assert table.c.message.nullable
    assert table.c.created_at.type.timezone
    assert table.c.created_at.server_default is not None
    assert ("event_id", "created_at") in index_columns


def test_manual_retry_request_columns_and_constraints() -> None:
    table = EventManualRetryRequest.__table__
    columns = table.c
    foreign_key = next(iter(columns.event_id.foreign_keys))
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    check_expressions = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert columns.id.primary_key
    assert isinstance(columns.id.type, PG_UUID)
    assert isinstance(columns.event_id.type, PG_UUID)
    assert not columns.event_id.nullable
    assert foreign_key.target_fullname == "events.id"
    assert columns.idempotency_key.type.length == 255
    assert not columns.idempotency_key.nullable
    assert not columns.target_attempt_number.nullable
    assert columns.accepted_at.type.timezone
    assert not columns.accepted_at.nullable
    assert columns.accepted_at.server_default is not None
    assert ("event_id",) in unique_columns
    assert any(
        "char_length(idempotency_key) BETWEEN 1 AND 255" in expression
        and "idempotency_key !~ '[[:space:]]'" in expression
        for expression in check_expressions
    )
    assert "target_attempt_number > 0" in check_expressions


def test_processing_attempt_columns_constraints_and_indexes() -> None:
    table = EventProcessingAttempt.__table__
    columns = table.c
    foreign_key = next(iter(columns.event_id.foreign_keys))
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    check_expressions = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert columns.id.primary_key
    assert isinstance(columns.id.type, PG_UUID)
    assert isinstance(columns.event_id.type, PG_UUID)
    assert not columns.event_id.nullable
    assert foreign_key.target_fullname == "events.id"
    assert not columns.attempt_number.nullable
    assert isinstance(columns.lease_token.type, PG_UUID)
    assert not columns.lease_token.nullable
    assert columns.started_at.type.timezone
    assert not columns.started_at.nullable
    assert columns.started_at.server_default is not None
    assert columns.finished_at.type.timezone
    assert columns.finished_at.nullable
    assert columns.outcome.type.length == 32
    assert columns.outcome.nullable
    assert columns.error.nullable

    assert ("event_id", "attempt_number") in unique_columns
    assert ("lease_token",) in unique_columns
    assert "attempt_number > 0" in check_expressions
    assert any(
        all(
            outcome in expression
            for outcome in (
                "completed",
                "retryable_failure",
                "permanent_failure",
                "lease_expired",
            )
        )
        for expression in check_expressions
    )
    assert any(
        "finished_at IS NULL AND outcome IS NULL" in expression
        and "finished_at IS NOT NULL AND outcome IS NOT NULL" in expression
        for expression in check_expressions
    )
    assert table.indexes == set()


def test_order_fulfillment_columns_and_constraints() -> None:
    table = OrderFulfillment.__table__
    columns = table.c
    foreign_key = next(iter(columns.first_event_id.foreign_keys))
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    check_expressions = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert tuple(column.name for column in table.primary_key.columns) == (
        "source_id",
        "order_id",
    )
    assert columns.source_id.type.length == 100
    assert not columns.source_id.nullable
    assert columns.order_id.type.length == 255
    assert not columns.order_id.nullable
    assert isinstance(columns.first_event_id.type, PG_UUID)
    assert not columns.first_event_id.nullable
    assert foreign_key.target_fullname == "events.id"
    assert columns.fulfilled_at.type.timezone
    assert not columns.fulfilled_at.nullable
    assert columns.fulfilled_at.server_default is not None

    assert ("first_event_id",) in unique_columns
    assert any(
        "length(btrim(order_id)) > 0" in expression
        and "order_id = btrim(order_id)" in expression
        and "order_id !~ '^[[:space:]]|[[:space:]]$'" in expression
        for expression in check_expressions
    )
