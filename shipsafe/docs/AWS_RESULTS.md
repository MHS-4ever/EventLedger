# Recorded live AWS results

The owner supplied these verified live observations for the final handover.
The documentation review additionally read GitHub job/step conclusions for all
three linked runs. It did not rerun deployments, query live controller state,
probe the endpoint or perform teardown. The historical endpoint was
`https://shipsafe.mhasnain.me`, account `542468167362`, Region `us-east-1`.

## Evidence by source

| Evidence | Recorded outcome | What it establishes |
|---|---|---|
| [ShipSafe release #3](https://github.com/MHS-4ever/EventLedger/actions/runs/42249695993) | Normal deployment succeeded; checks/test and publish succeeded. Owner verified public `/ready` HTTP 200. | A successful AWS release plus an externally observed ready API at that time. |
| [ShipSafe release #5](https://github.com/MHS-4ever/EventLedger/actions/runs/42354112839) | `deploy`, `post-promotion`, source `4a500698c487ed98e933124d63721f2263304d8a`; CI succeeded, publish intentionally failed. Recorded controller error: `proxy readiness check failed`. | Deliberate failure after candidate promotion; a red workflow is expected. |
| Controller state after #5 (owner-verified) | Failed candidate `4a500698c487-1152b810e8f4`; active `b0cafc241369-c0fe57fcfe36`; active slot `green`; worker `b0cafc241369-c0fe57fcfe36`; no pending deployment. | Previous working API restored, original worker retained and controller operation cleared. |
| Public HTTPS after recovery (owner-verified) | `/ready` returned HTTP 200. | External readiness after recovery, independently of controller state. It does not measure the interruption duration. |
| [ShipSafe release #6](https://github.com/MHS-4ever/EventLedger/actions/runs/42354791446) | Same full source commit as #5, `failure_mode=none`; CI and publish passed; SSM deployment and public HTTPS verification succeeded; workflow duration **4 minutes 8 seconds** (owner-recorded). | Normal retry of the same failed candidate source worked. Duration is one workflow observation, not a benchmark or recovery-time measurement. |

The #6 job metadata confirms successful production Compose/Caddy validation,
isolated PostgreSQL tests, local container acceptance and publish steps. Those
CI steps execute isolated tests on the runner; they are not additional live AWS
failure demonstrations. Publisher success means its SSM wait and public HTTPS
checks returned successfully according to the implemented code. It is distinct
from the separate owner-observed HTTP response after #5.

## Observed sequence and interpretation

1. A working AWS deployment was available before the test.
2. #5 verified and promoted the new candidate, then deliberately stopped its API.
   Proxy readiness failed, activating the controller's existing rollback path.
3. The state and public-response observations above verified recovery to the
   prior release on green. The worker retained its prior release because failure
   happened before worker replacement.
4. #6 retried the same SHA without injection. The retained image/archive identity
   could be reused; successful publication/deployment did not require a new SHA.

This demonstrates one controlled application-failure scenario. It does not prove
zero downtime: stopping the promoted candidate can briefly interrupt API traffic.
It does not test host loss, database corruption, every interrupted phase, backup
restore, long-term uptime, load capacity or universal rollback success. Manual
rollback is implemented and covered locally; the supplied live evidence does
not establish a separate live manual-rollback run.

## Automated evidence and remaining artifacts

The rollback-gating implementation review recorded **79 focused pytest passes**
and **5 mocked-provider Terraform test passes**, plus Terraform validation and
format checks. Relevant tests cover normal publication, input rejection,
invocation-only injection, API reversal, original-worker retention and same-SHA
normal retry. See [release tests](../../tests/test_shipsafe_release.py),
[controller tests](../../tests/test_shipsafe_controller.py),
[rendered SSM tests](../../tests/test_shipsafe_readiness.py) and
[Terraform tests](../infra/main/tests/readiness.tftest.hcl). These counts belong
to that implementation review, not a new application test run for this docs PR.
Earlier phase reports and the README's dated local test counts remain historical.

Raw post-failure SSM command IDs, full controller-state exports, timestamped HTTP
captures, notification receipts and backup/restore evidence were not supplied as
tracked artifacts. The supplied error/state/HTTPS/duration observations are
attributed to the owner rather than fabricated as newly collected logs. Capture
sanitized originals before CloudWatch/GitHub retention expires if a durable audit
package is needed. Do not commit secrets, state files, dumps or raw environment output.

See [AWS handover](AWS_HANDOVER.md), [repeatable test procedure](../../docs/SHIPSAFE_ROLLBACK_DEMO.md)
and [cleanup checklist](AWS_TEARDOWN.md).
