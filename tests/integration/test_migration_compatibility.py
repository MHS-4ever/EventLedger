import json
import logging
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from scripts.prepare_test_db import (
    create_test_database_engine,
    load_test_database_settings,
    validate_test_database_settings,
    verify_test_database_connection,
)

RUN_PG_INTEGRATION = os.getenv("EVENTLEDGER_RUN_PG_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.pg_integration,
    pytest.mark.skipif(
        not RUN_PG_INTEGRATION,
        reason="Set EVENTLEDGER_RUN_PG_INTEGRATION=1 to run PostgreSQL tests.",
    ),
]


def test_manual_retry_migration_preserves_existing_day2_event() -> None:
    try:
        settings = load_test_database_settings()
        validate_test_database_settings(settings)
    except (ValidationError, RuntimeError):
        pytest.fail(
            "Unsafe or invalid .env.test configuration; refusing PostgreSQL tests."
        )

    engine = create_test_database_engine(settings)
    schema_name = f"migration_compat_{uuid4().hex}"
    event_id = uuid4()
    source_id = f"day2-source-{uuid4().hex}"
    idempotency_key = f"day2-key-{uuid4().hex}"
    payload = {"order_id": f"ORD-{uuid4().hex}"}
    request_hash = "d" * 64
    processing_logger = logging.getLogger("app.services.processing")
    logger_was_disabled = processing_logger.disabled
    connection = engine.connect()
    transaction = connection.begin()
    try:
        verify_test_database_connection(connection)
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema_name}"'))

        alembic_config = Config("alembic.ini")
        alembic_config.attributes["connection"] = connection
        command.upgrade(alembic_config, "b87f4d92c1a6")

        connection.execute(
            text(
                "INSERT INTO events ("
                "id, source_id, idempotency_key, event_type, payload, request_hash"
                ") VALUES ("
                ":id, :source_id, :idempotency_key, :event_type, "
                "CAST(:payload AS jsonb), :request_hash"
                ")"
            ),
            {
                "id": event_id,
                "source_id": source_id,
                "idempotency_key": idempotency_key,
                "event_type": "order.created",
                "payload": json.dumps(payload),
                "request_hash": request_hash,
            },
        )

        command.upgrade(alembic_config, "d4e5f6a7b8c9")

        migrated = connection.execute(
            text(
                "SELECT source_id, idempotency_key, event_type, payload, "
                "request_hash, status, attempt_count, attempt_limit_override "
                "FROM events WHERE id = :id"
            ),
            {"id": event_id},
        ).one()
        assert migrated.source_id == source_id
        assert migrated.idempotency_key == idempotency_key
        assert migrated.event_type == "order.created"
        assert migrated.payload == payload
        assert migrated.request_hash == request_hash
        assert migrated.status == "pending"
        assert migrated.attempt_count == 0
        assert migrated.attempt_limit_override is None
        assert connection.scalar(
            text("SELECT to_regclass('event_manual_retry_requests')")
        ) == "event_manual_retry_requests"
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "d4e5f6a7b8c9"
        )
        assert processing_logger.disabled is logger_was_disabled
    except (RuntimeError, SQLAlchemyError):
        pytest.fail("Unable to verify migration compatibility safely.")
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        try:
            with engine.begin() as cleanup_connection:
                verify_test_database_connection(cleanup_connection)
                cleanup_connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
                )
        finally:
            engine.dispose()
