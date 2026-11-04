# EventLedger project summary

## Problem

Webhook endpoints must acknowledge durable receipt while handling duplicate delivery, concurrent processing, transient failures, and worker crashes. Running the business operation directly inside the HTTP request couples availability and makes recovery difficult.

## Solution

EventLedger persists source-authenticated events in PostgreSQL and processes them in an independent worker. PostgreSQL provides the durable queue, row-level claim coordination, attempt audit trail, retry schedule, lease state, and local fulfillment uniqueness.

## Architecture

- FastAPI accepts and queries events.
- A server-configured API key determines the source boundary.
- PostgreSQL stores events, status history, processing attempts, manual retry grants, and order fulfillments.
- Independent workers claim rows with `FOR UPDATE SKIP LOCKED`.
- Alembic manages the schema.
- Docker Compose starts PostgreSQL, runs migrations once, then starts API and worker services.

## Important technical decisions

- Source-scoped database uniqueness makes ingestion idempotency concurrency-safe.
- Claim transactions are short; handler execution happens after the claim commits.
- Lease tokens fence stale workers during finalization.
- Attempt numbers only increase, and history is never reset for manual retry.
- A manual retry stores an exact one-attempt override and one unique audit row.
- Public attempt errors come from a fixed mapping rather than stored exception text.
- Fulfillment uniqueness is transactional within the same PostgreSQL database.

## Challenges and recovery mechanisms

- Retryable failures use bounded exponential scheduling.
- Permanent failures stop immediately.
- Expired leases become recorded attempt outcomes and may be rescheduled within the effective limit.
- Malformed expired state is quarantined instead of repeatedly blocking queue work.
- Manual retry is available once for a terminal retryable or lease-expired outcome.
- Fencing prevents a late worker from overwriting state after lease loss.

## Objectively tested

On 2026-11-04, the Python 3.12 suite reported 271 passing tests, including guarded PostgreSQL integration tests. The separate Docker acceptance run verified authentication and source isolation, ingestion replay and conflict, filtering and pagination, automatic retry and exhaustion, permanent failure, manual retry and replay, forced worker crash recovery, fulfillment uniqueness, PostgreSQL restart persistence, readiness, migration state, non-root processes, an internal-only database port, and fresh startup on an independent volume.

These results are functional evidence from local test and Docker environments. They are not load benchmarks, cloud deployment evidence, or a production-service claim.

## Not implemented

TLS termination, rate limiting, key rotation, multiple managed identities, an operator role, a secret manager, backups, monitoring and alerting, lease heartbeats, strict FIFO scheduling, cloud orchestration, and exactly-once external side effects remain outside the project.

## Interview explanation

EventLedger is a FastAPI and PostgreSQL event-processing backend designed to demonstrate reliable webhook handling. The API durably stores source-authenticated events and uses a database uniqueness constraint for concurrency-safe idempotency. Independent workers claim eligible rows with `FOR UPDATE SKIP LOCKED`, record monotonic attempts, and receive time-bounded lease tokens. Finalization is fenced so a stale worker cannot overwrite newer state. Retryable failures use bounded backoff, permanent failures stop, expired leases recover, and an eligible failed event can receive exactly one audited manual retry without deleting history. The included order handler writes a source-scoped fulfillment in the same transaction as successful finalization. It deliberately does not claim exactly-once external effects: handlers may run more than once, so external integrations still require their own idempotency.
