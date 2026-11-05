# Phase 1 test evidence

This file records commands and observed results for the current working tree.
It must not be used to claim AWS deployment or blue/green rollback verification.

## Coverage added

- Legacy `d4e5f6a7b8c9` bootstrap acceptance without a migration.
- Supported contract acceptance independent of Alembic head equality.
- Unknown legacy revision and unsupported/malformed contract rejection.
- Sanitized readiness and worker-preflight failures.
- Worker rejection before polling or signal-handler installation.
- ShipSafe production identity validation with development defaults preserved.
- PostgreSQL tests use only the guarded local `eventledger_test` database on
  port `5433` and temporary transaction-scoped schemas.

## Results

Observed on 2026-11-05 with Python 3.12.4 and pytest 8.4.2:

```text
Focused compatibility/configuration/worker tests:
56 passed

Unit suite (`-m "not pg_integration"`):
180 passed, 106 deselected

PostgreSQL suite (`-m pg_integration`):
106 passed, 180 deselected
```

Docker Desktop 29.9.2 was available. The dedicated `eventledger-test` Compose
PostgreSQL service was started, confirmed healthy, prepared through the guarded
`scripts.prepare_test_db` command, used for the integration suite, and stopped.
No delivery database was addressed.

The first PostgreSQL run produced `104 passed, 2 failed`; both failures were a
syntax error in the new temporary contract-table test fixture. After adding the
missing closing parenthesis, the complete PostgreSQL suite was rerun and all
106 tests passed. No application defect was hidden or reclassified as a skip.

AWS deployment, Caddy traffic switching, application rollback, database
rollback, and blue/green behavior were not executed or verified in Phase 1.
