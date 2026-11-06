# Reliability and security

This document states what the current EventLedger implementation does and, equally importantly, what it does not guarantee.

## Reliability properties

### Durable ingestion

An accepted new event and its initial `pending` history row are committed to PostgreSQL before the API returns `201`. A process failure before commit does not create a partially accepted event. Durability still depends on the PostgreSQL deployment, storage, backup, and recovery configuration; Compose alone is not a backup strategy.

### Source-scoped ingestion idempotency

`(source_id, idempotency_key)` is unique in PostgreSQL. The source is derived from the authenticated API key rather than supplied by the request. A canonical SHA-256 hash of `event_type` and payload distinguishes a valid replay from conflicting reuse:

- Matching content returns the existing event and does not enqueue another event.
- Different content returns `409 idempotency_key_conflict`.
- The same textual key could be used independently by different sources if multi-source configuration is added later.

This is request deduplication in this database. It is not a global idempotency guarantee across separate deployments or databases.

### Atomic claims and concurrent workers

Each claim uses a PostgreSQL transaction and `SELECT ... FOR UPDATE SKIP LOCKED`. The transaction atomically increments `attempt_count`, creates an attempt row, assigns a lease, changes status, and adds history. Concurrent workers skip locked candidates rather than claiming the same row.

The selection order is `next_attempt_at`, `created_at`, then `id`, but locked rows are skipped and recovery is processed before new claims in each worker cycle. EventLedger therefore provides no strict FIFO processing guarantee.

### Processing leases and fencing

A claim carries a random lease token and database-derived expiry. Finalization locks the event and active attempt and verifies status, token, attempt number, unfinished state, and an unexpired lease. A stale worker cannot commit completion or failure after ownership is lost.

Recovery finalizes an expired attempt as `lease_expired`, clears ownership, and either schedules another attempt or fails the event at its effective limit. Inconsistent expired state without a matching unfinished attempt is quarantined as failed so it does not block normal queue work.

There is no lease heartbeat. A handler that legitimately runs longer than `WORKER_LEASE_DURATION_SECONDS` can lose its lease and be executed again. Operators must configure the lease longer than expected handler time or implement heartbeats in future work.

### Bounded automatic retries

Retryable failures are rescheduled with bounded exponential delay. `WORKER_MAX_ATTEMPTS` controls ordinary events. Permanent and unexpected failures become terminal immediately. Expired leases consume attempts.

Retries are bounded, so EventLedger does not guarantee eventual success. An event can remain `failed` after its attempts are exhausted, after a permanent failure, or after its single manual retry fails.

### One manual retry grant

An authenticated source can authorize one extra attempt only when:

- The source owns the event.
- The event is terminally `failed` with no active lease.
- No manual grant or attempt-limit override already exists.
- Attempt rows are contiguous and agree with `attempt_count`.
- The latest outcome is `retryable_failure` or `lease_expired`.

Authorization stores one audit row and sets an exact per-event limit of `attempt_count + 1`. It does not reset the count or remove history. A permanent business-validation failure is not eligible. A unique `event_id` constraint and row lock prevent multiple grants under concurrent requests.

Same-key retry replay returns the stored acceptance even after the worker changes event state. A different key cannot create another grant.

### Local fulfillment uniqueness

The included `order.created` handler validates an `order_id`. Successful finalization inserts `order_fulfillments` under primary key `(source_id, order_id)` and a unique `first_event_id`. The fulfillment insert, ownership validation, attempt completion, event completion, and history transition commit in one PostgreSQL transaction.

This protects this local PostgreSQL fulfillment record from duplication. It does not make a remote payment, email, HTTP call, or other external effect exactly-once.

## Execution and delivery semantics

The worker commits a claim before invoking the handler and finalizes afterward. If a worker stops after the handler runs but before finalization, recovery may run the handler again. A lease can also expire during a slow handler. Therefore:

- Handler code may execute more than once.
- Handlers that perform external effects need their own idempotency strategy with the external system.
- External effects are not exactly-once.
- Bounded recovery exists, but unconditional at-least-once execution and eventual success are not promised.
- A database or operational outage can delay all progress.

## Authentication and source isolation

The current configuration supports one static `EVENTLEDGER_API_KEY` mapped to one static `EVENTLEDGER_SOURCE_ID`. Comparison uses a constant-time digest comparison. Every `/v1` route authenticates, and event lookups include the authenticated source. Missing and foreign event identifiers both return `404`.

Current limitations:

- There is no API-key registry or multiple-key management interface.
- There is no key rotation workflow, expiry, revocation list, or per-key permission set.
- There is no separate administrator or operator identity.
- Whoever holds the source key can read that source's event payloads and authorize eligible retries.
- API keys must be supplied and protected outside the repository.

## Sensitive data and error handling

Settings represent database passwords and API keys as secret values and construct SQLAlchemy URLs without logging them. The delivery environment file is ignored by Git and excluded from Docker build context.

Public attempt responses expose fixed summaries derived from outcome values. They do not expose stored exception text, `events.last_error`, attempt `error`, or lease tokens. Public status history omits its internal `message`. Unexpected handler errors are logged by exception type and persisted as the generic text `Unexpected processing error`.

Remaining concerns:

- Ingestion and manual-retry idempotency keys are stored in plaintext in PostgreSQL.
- Event payloads are stored in plaintext and may contain business-sensitive data.
- Internal error fields and history messages may contain operational detail and require database-access controls.
- Application logs contain event IDs, attempt numbers, statuses, and worker identifiers.
- There is no payload-level encryption or field redaction framework.

## Health and readiness

`/health` is process liveness only and does not contact PostgreSQL. A healthy response does not mean ingestion or processing can use the database.

`/ready` executes `SELECT 1` and evaluates the ShipSafe schema-compatibility bridge. Without a contract table, only the known legacy bootstrap revision `d4e5f6a7b8c9` is accepted. After a later reviewed migration creates the contract table, its explicit version is authoritative instead of exact Alembic-head equality. It returns a safe `503 {"status":"not_ready"}` on database, table, revision, contract, or internal readiness failure. It does not reveal connection strings, SQL text, exceptions, or credentials.

The worker evaluates the same compatibility check before polling and exits without claiming an event when the schema is incompatible. Readiness confirms connectivity and runtime schema compatibility, not overall worker health, queue progress, disk capacity, replication health, or downstream availability.

## Deployment limitations

The supplied Compose configuration is a reproducible local delivery setup, not a complete production platform. The repository does not implement:

- TLS termination or certificate management.
- Edge authentication controls, explicit request-size controls, rate limiting, or abuse protection.
- Automatic API-key or database-credential rotation.
- A production secret manager.
- Database backups, restore testing, replication, failover, or point-in-time recovery.
- Centralized logs, metrics, traces, dashboards, monitoring, or alerting.
- Dead-letter administration or a general event-requeue operator interface.
- Lease heartbeat or cancellation of a stale handler already executing.
- A broker, partitioning strategy, priority queues, or strict FIFO scheduling.
- Horizontal orchestration, autoscaling, rolling deployment, or cloud infrastructure.
- Real-world load, soak, latency, or capacity benchmarks.

PostgreSQL is not published to the host in `compose.delivery.yaml`, and the API binds to host loopback. Exposure beyond the local machine requires an explicitly designed secure ingress path.

## Operational guidance

- Keep `WORKER_FAILURE_SIMULATION_ENABLED=false` for regular workers. Delivery Compose forces this value.
- Use a dedicated Compose project name and environment file for each isolated delivery instance.
- Run migrations as a one-shot job before API and worker startup.
- Stop services without deleting the volume when data must be retained.
- Back up and test restoration before treating stored events as operational records.
- Ensure custom handlers are idempotent with respect to every external system they affect.
- Monitor failed events, expired leases, retry exhaustion, migration state, and worker progress in any future production deployment.
