from app.db.compatibility import (
    LEGACY_BOOTSTRAP_REVISION,
    check_schema_compatibility,
)


class CompatibilitySession:
    def __init__(
        self,
        *,
        contract_exists: bool = False,
        contract_version: int | None = None,
        revision: str | None = LEGACY_BOOTSTRAP_REVISION,
    ) -> None:
        self.contract_exists = contract_exists
        self.contract_version = contract_version
        self.revision = revision
        self.queries: list[str] = []

    def execute(self, statement: object) -> None:
        self.queries.append(str(statement))

    def scalar(self, statement: object) -> str | int | bool | None:
        sql = str(statement)
        self.queries.append(sql)
        if "to_regclass" in sql:
            return self.contract_exists
        if "eventledger_schema_contract" in sql:
            return self.contract_version
        return self.revision


def test_legacy_bootstrap_accepts_only_known_revision() -> None:
    compatible = check_schema_compatibility(CompatibilitySession())
    incompatible = check_schema_compatibility(
        CompatibilitySession(revision="unknown-revision")
    )

    assert compatible.compatible
    assert compatible.mode == "legacy"
    assert compatible.reason == "legacy_bootstrap"
    assert not incompatible.compatible
    assert incompatible.reason == "unsupported_legacy_revision"


def test_contract_is_authoritative_after_table_exists() -> None:
    session = CompatibilitySession(
        contract_exists=True,
        contract_version=1,
        revision="unknown-revision",
    )

    compatibility = check_schema_compatibility(session)

    assert compatibility.compatible
    assert compatibility.mode == "contract"
    assert compatibility.reason == "supported_contract"
    assert not any("alembic_version" in query for query in session.queries)


def test_unknown_or_malformed_contract_fails_closed() -> None:
    for version in (None, 0, 2, 999):
        compatibility = check_schema_compatibility(
            CompatibilitySession(
                contract_exists=True,
                contract_version=version,
            )
        )

        assert not compatibility.compatible
        assert compatibility.mode == "contract"
        assert compatibility.reason == "unsupported_or_malformed_contract"
