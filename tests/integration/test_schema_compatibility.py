import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.compatibility import check_schema_compatibility
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


def test_bridge_accepts_unmodified_legacy_database_revision() -> None:
    try:
        settings = load_test_database_settings()
        validate_test_database_settings(settings)
    except (ValidationError, RuntimeError):
        pytest.fail(
            "Unsafe or invalid .env.test configuration; refusing PostgreSQL tests."
        )

    engine = create_test_database_engine(settings)
    schema_name = f"compat_legacy_{uuid4().hex}"
    connection = engine.connect()
    transaction = connection.begin()
    try:
        verify_test_database_connection(connection)
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema_name}"'))
        alembic_config = Config("alembic.ini")
        alembic_config.attributes["connection"] = connection
        command.upgrade(alembic_config, "d4e5f6a7b8c9")

        compatibility = check_schema_compatibility(connection)  # type: ignore[arg-type]

        assert compatibility.compatible
        assert compatibility.mode == "legacy"
        assert compatibility.reason == "legacy_bootstrap"
    except (RuntimeError, SQLAlchemyError):
        pytest.fail("Unable to verify legacy bridge compatibility safely.")
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("contract_version", "expected_compatible"),
    [(1, True), (2, False)],
)
def test_contract_version_controls_future_runtime_compatibility(
    contract_version: int,
    expected_compatible: bool,
) -> None:
    try:
        settings = load_test_database_settings()
        validate_test_database_settings(settings)
    except (ValidationError, RuntimeError):
        pytest.fail(
            "Unsafe or invalid .env.test configuration; refusing PostgreSQL tests."
        )

    engine = create_test_database_engine(settings)
    schema_name = f"compat_contract_{uuid4().hex}"
    connection = engine.connect()
    transaction = connection.begin()
    try:
        verify_test_database_connection(connection)
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema_name}"'))
        connection.execute(
            text(
                "CREATE TABLE alembic_version ("
                "version_num varchar(32) PRIMARY KEY)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO alembic_version (version_num) "
                "VALUES ('future-revision')"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE eventledger_schema_contract ("
                "singleton boolean PRIMARY KEY CHECK (singleton IS TRUE), "
                "contract_version integer NOT NULL "
                "CHECK (contract_version > 0))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO eventledger_schema_contract "
                "(singleton, contract_version) VALUES (TRUE, :version)"
            ),
            {"version": contract_version},
        )

        compatibility = check_schema_compatibility(connection)  # type: ignore[arg-type]

        assert compatibility.compatible is expected_compatible
        assert compatibility.mode == "contract"
    except SQLAlchemyError:
        pytest.fail("Unable to verify schema-contract compatibility safely.")
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        engine.dispose()
