# Phase 2 test evidence

This document records observed local results. It must not be used as evidence
of AWS deployment, public HTTPS, high availability, or production readiness.

## Automated controller tests

Observed on 2026-11-05:

```text
Python 3.12.4, pytest 8.4.2
ShipSafe controller tests: 12 passed
Complete non-PostgreSQL suite: 192 passed, 106 deselected
PostgreSQL integration suite: 106 passed, 192 deselected
Compose config validation: passed
git diff --check: passed
```

Two early controller-test invocations reported 12 setup errors each because
pytest's Windows `tmp_path` directory was inaccessible in the managed
workspace. No test body ran. The tests were changed to use per-test directories
under ignored `shipsafe/runtime/unit-tests`; all 12 then passed. This was a test
environment issue, not a skipped or reclassified controller failure.

A disposable `python:3.12.4-slim-bookworm` Linux container successfully ran the
`shipsafe.sh --help` entry point, imported the controller, wrote/read atomic
state, and acquired/released the Linux `fcntl` host lock.

## Docker acceptance matrix

The final isolated project was `shipsafe-acceptance`. Eight label-distinct
images were built from Git commit
`2ce832f74df6e20f8bf21412906d13f3f598868b`; their different Docker image IDs
provided distinct local release identities without changing application code.

| Scenario | Result | Observed behavior |
|---|---|---|
| Initial blue bootstrap | Passed | DB healthy, explicit migration to `d4e5f6a7b8c9`, blue/API/proxy/worker verified |
| Healthy blue-to-green | Passed | Green verified before routing; worker rolled afterward |
| Candidate startup failure | Passed | Candidate rejected; active release retained |
| Candidate readiness failure | Passed | Invalid candidate DB host failed closed; active retained |
| Candidate authenticated smoke failure | Passed | Invalid test credential rejected; active retained |
| Post-promotion failure | Passed | Candidate stopped during observation; Caddy reversed to old slot |
| Worker upgrade failure | Passed | Previous worker and API route restored |
| Manual rollback | Passed | Verified blue image restarted and promoted from green |
| Concurrent deployment | Passed | Second process rejected by host lock |
| Interrupted deployment | Passed | Process exited after switch with code 75; `recover` restored route and cleared state |
| Repeated deployment | Passed | Deploying the active Git SHA/image ID was a no-op |
| Database persistence | Passed | Initial synthetic event remained readable after deploys and rollback |
| Schema immutability | Passed | Live `alembic_version` remained `d4e5f6a7b8c9` |

Final active release was `2ce832f74df6-5fc8604d1c92` in blue; green release
`2ce832f74df6-ce8b203a3500` remained a verified previous release. All Compose
services were stopped. The volume
`shipsafe-acceptance_shipsafe_postgres_data` was retained; no `down`, volume
delete, downgrade, or contract-table migration was run.

## Failures discovered and corrected

The first bootstrap attempt exposed two harness/controller portability defects:
Windows subprocess output used CP-1252 instead of UTF-8, and the proxy check ran
immediately after starting Caddy. UTF-8 decoding and a bounded proxy-start wait
were added. That run was recovered without deleting its database volume.

The second bootstrap showed that attaching Caddy only to Docker's
`internal: true` network prevented Docker Desktop from creating a live host port
mapping. The final architecture now uses an internal backend plus a separate
edge network joined only by Caddy. That run was also recovered with its database
volume preserved. The complete third run produced all passing results above.

These tests verify local behavior only. AWS, SSM, ECR, public HTTPS, CloudWatch,
external rollback, and backup restoration were not executed or inferred.
