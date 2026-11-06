# ShipSafe release compatibility

## Phase 1 bridge

EventLedger originally treated the single Alembic head packaged in an image as
its runtime database contract. That makes an old blue slot unready as soon as a
shared database is migrated for green, even when the migration is additive.
Application rollback then becomes impossible without a database downgrade.

The Phase 1 bridge separates migration history from runtime compatibility while
requiring no database change before deployment:

- If `eventledger_schema_contract` does not exist, only Alembic revision
  `d4e5f6a7b8c9` is accepted. This is the known legacy bootstrap state.
- If the contract table exists, exactly one valid row must contain a contract
  version supported by the image. This bridge supports contract version `1`.
- Once the table exists, its contract is authoritative; a different future
  Alembic revision can remain compatible.
- Missing, stale, multiple-head, malformed, unknown, and unsupported states
  fail closed. The API returns the existing sanitized `503` body.
- The worker runs the same check once before polling while retaining its normal
  SIGINT/SIGTERM handlers. Failure exits with code `2`; no event is claimed.

`SHIPSAFE_PRODUCTION_MODE=true` adds startup validation for
`EVENTLEDGER_API_KEY` and `EVENTLEDGER_SOURCE_ID`. It defaults to false so local
development and the existing test configuration retain their prior behavior.

## Required staged ordering

1. Deploy this bridge release while the database remains at
   `d4e5f6a7b8c9`. Do not run a migration first.
2. Verify every API and worker instance is a bridge-capable image.
3. Retire the legacy exact-head image as an automatic rollback target.
4. In a later reviewed phase, add an expand-only Alembic migration that creates
   `eventledger_schema_contract` with one row: `singleton=TRUE`,
   `contract_version=1`.
5. Run that migration as a separate, serialized release step. Verify bridge API
   readiness and worker preflight before promotion.
6. Keep contract version `1` across additive migrations that preserve the same
   runtime contract. Do not bump it merely because the Alembic revision changes.
7. Bump the contract only after all rollback candidates support the new version.
   Destructive cleanup belongs in a later contract phase after its rollback
   window closes.

The future migration is intentionally not included in Phase 1. Creating it now
could make the legacy exact-head release unready before the bridge has replaced
it.

## Rollback limits

Image rollback never downgrades PostgreSQL. A rollback controller must check the
target image's supported contract range against the live database before it
starts or routes traffic. Rollback is blocked if the ranges do not overlap.

API slots may coexist only while both support the live contract. Workers are
rolled separately: keep the old worker during candidate API checks, stop/drain
it after API promotion, start the candidate worker, and restore the old worker
only when it supports the live contract. Existing leases, fencing, idempotency,
and retry behavior are unchanged; zero-downtime worker rollback is not claimed.

No destructive downgrade, blue/green rollback, or AWS deployment was performed
or verified in Phase 1.
