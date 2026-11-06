from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


LEGACY_BOOTSTRAP_REVISION = "d4e5f6a7b8c9"
SUPPORTED_SCHEMA_CONTRACT_VERSIONS = frozenset({1})


@dataclass(frozen=True, slots=True)
class SchemaCompatibility:
    compatible: bool
    mode: str
    reason: str


def check_schema_compatibility(session: Session) -> SchemaCompatibility:
    """Check the database contract without treating Alembic head as an API.

    Before the contract table exists, this bridge release accepts only the
    known EventLedger baseline. After a later reviewed migration creates the
    contract table, the explicit contract version becomes authoritative.
    Database and SQL errors intentionally propagate so callers can fail closed
    without exposing their details.
    """
    session.execute(text("SELECT 1"))
    contract_table_exists = bool(
        session.scalar(
            text(
                "SELECT to_regclass('eventledger_schema_contract') "
                "IS NOT NULL"
            )
        )
    )

    if contract_table_exists:
        contract_version = session.scalar(
            text(
                "SELECT CASE "
                "WHEN count(*) = 1 AND bool_and(singleton IS TRUE) "
                "THEN min(contract_version) "
                "END "
                "FROM eventledger_schema_contract"
            )
        )
        if contract_version in SUPPORTED_SCHEMA_CONTRACT_VERSIONS:
            return SchemaCompatibility(True, "contract", "supported_contract")
        return SchemaCompatibility(
            False,
            "contract",
            "unsupported_or_malformed_contract",
        )

    current_revision = session.scalar(
        text(
            "SELECT CASE WHEN count(*) = 1 THEN min(version_num) END "
            "FROM alembic_version"
        )
    )
    if current_revision == LEGACY_BOOTSTRAP_REVISION:
        return SchemaCompatibility(True, "legacy", "legacy_bootstrap")
    return SchemaCompatibility(
        False,
        "legacy",
        "unsupported_legacy_revision",
    )
