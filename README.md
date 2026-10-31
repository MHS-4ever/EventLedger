# EventLedger

EventLedger is a reliable webhook and event-processing backend built with Python and FastAPI. This initial phase provides a minimal API with a health-check endpoint.

## Local development

Python 3.11 or newer is required. In PowerShell, run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Start the development server:

```powershell
python -m uvicorn app.main:app --reload
```

The health endpoint is available at `http://127.0.0.1:8000/health`.

Run the tests:

```powershell
python -m pytest
```

## Day 2 worker architecture

Event ingestion and processing are deliberately separate. FastAPI authenticates,
validates, and durably stores events as `pending`; it never waits for a business
handler. A standalone synchronous worker uses PostgreSQL as the durable queue:

```powershell
python -m app.worker
```

For debugging, `--once` performs one maintenance cycle (at most one expired-lease
recovery and one exhausted-pending transition) and processes at most one newly
claimed event before exiting:

```powershell
python -m app.worker --once
```

The worker claims one due event in a short transaction using `FOR UPDATE SKIP
LOCKED`. That transaction changes the event to `processing`, increments its
durable attempt count, assigns a fresh lease token and expiry, and inserts both
an attempt record and processing-history row. It commits before handler code
runs, so multiple processes may compete safely without holding database locks
during business processing.

Every success, failure, and recovery transaction locks the event and verifies
the exact lease token. Finalization also verifies lease validity using
PostgreSQL `clock_timestamp()`. A stale or over-time worker cannot complete the
event or write fulfillment state. There is intentionally no lease heartbeat:
handlers must finish within the configured lease, and an over-time handler is
fenced out and recovered later.

Expired claims are recovered with `SKIP LOCKED`. The abandoned attempt is closed
as `lease_expired`. If the configured total-attempt budget remains, the event is
returned to `pending` with a durable retry time; otherwise it becomes `failed`.
Recovery itself does not increment `attempt_count`. A new count is consumed only
when a later claim commits.

Retryable failures use deterministic capped exponential backoff. With the
defaults, attempt 1 retries after 2 seconds, attempt 2 after 4 seconds, and three
total claims are permitted. Permanent contract failures—such as an unsupported
event type or invalid `order_id`—become terminal immediately.

### Worker settings

All settings are optional and validated:

| Environment variable | Default | Meaning |
|---|---:|---|
| `WORKER_POLL_INTERVAL_SECONDS` | `1` | Empty-queue/database-error poll delay |
| `WORKER_LEASE_DURATION_SECONDS` | `30` | Processing lease duration |
| `WORKER_MAX_ATTEMPTS` | `3` | Total claims, including the first |
| `WORKER_RETRY_BASE_SECONDS` | `2` | First retry delay |
| `WORKER_RETRY_MAX_SECONDS` | `60` | Backoff cap |
| `WORKER_IDENTIFIER` | generated | Optional log identity |
| `WORKER_FAILURE_SIMULATION_ENABLED` | `false` | Enables demo-only payload controls |
| `WORKER_SIMULATION_MAX_SLOW_SECONDS` | `30` | Upper bound for simulated slow work |

Changing `WORKER_MAX_ATTEMPTS` affects already queued events because the limit is
worker configuration, while each event's consumed count is durable. Lowering it
can cause an already-exhausted pending event to be terminally failed.

### Demonstration fulfillment

The only supported handler is `order.created`. It requires
`payload.order_id` to be a nonempty string of at most 255 characters without
surrounding Unicode whitespace. Generic ingestion remains generic; this contract
is checked by the worker.

Fulfillment identity is exactly `(source_id, order_id)`, where `source_id` comes
from the trusted event row. Different incoming idempotency keys may create
different events for the same business order, but PostgreSQL stores only one
fulfillment and preserves its `first_event_id`. Another source may independently
use the same `order_id`.

Claims are persisted and can be recovered after lease expiry. A handler may
execute more than once after failures or expired leases, but the retry budget is
bounded: repeated crashes or errors can make an event terminally fail. A
completed fulfillment row is transactionally idempotent inside this PostgreSQL
database. This does not guarantee exactly-once external side effects or eventual
success. No payment, shipping, email, or other external operation is performed.

### Failure simulation

Failure simulation must never be enabled in production. The worker emits a
startup warning whenever it is enabled.

Simulation fields have no effect unless
`WORKER_FAILURE_SIMULATION_ENABLED=true`:

- `_demo_failure: "transient_once"` fails attempt 1, then succeeds.
- `_demo_failure: "transient_always"` exhausts the retry budget.
- `_demo_failure: "permanent"` fails immediately.
- `_demo_failure: "slow"` sleeps on every attempt.
- `_demo_failure: "slow_once"` sleeps only on attempt 1, which supports a
  crash-recovery-then-success demonstration.
- Both slow modes use the bounded numeric
  `_demo_slow_seconds` value.

Simulation uses the durable attempt number and stores no in-memory counters.

Ctrl+C or a supported termination signal stops new claims after the current
bounded operation. A forced kill cannot run cleanup; its committed claim remains
`processing` until lease recovery.

Status transitions remain available through the existing history API. Internal
attempt records live in `event_processing_attempts`, and database-only business
state lives in `order_fulfillments`; neither exposes lease tokens or errors in a
public endpoint.

See `MANUAL_TESTING_DAY2.md` for guarded migration, integration-test, concurrency,
retry, and crash-recovery procedures.
