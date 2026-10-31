from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

from app.db.base import Base
from app.models import Event, EventStatusHistory


def test_model_tables_are_registered() -> None:
    assert Event.__table__ is Base.metadata.tables["events"]
    assert EventStatusHistory.__table__ is Base.metadata.tables[
        "event_status_history"
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
    index_columns = {
        tuple(column.name for column in index.columns) for index in table.indexes
    }

    assert ("source_id", "idempotency_key") in unique_columns
    assert any("status IN" in expression for expression in check_expressions)
    assert "attempt_count >= 0" in check_expressions
    assert ("status", "created_at") in index_columns


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
    assert columns.last_error.nullable
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
