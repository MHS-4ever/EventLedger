# EventLedger architecture

This document describes the EventLedger application and local delivery model. PostgreSQL is both the durable event store and the worker-coordination mechanism; there is no broker or in-memory queue. For the EC2 blue/green topology, Caddy, CI/CD, AWS services and deployment trade-offs, see [ShipSafe AWS architecture](../shipsafe/docs/AWS_HANDOVER.md#architecture-and-decisions). Local delivery runs `upgrade head`; ShipSafe AWS bootstrap deliberately targets the supported fixed revision.

## Components and dependencies

```mermaid
flowchart TB
    Producer[Webhook producer]
    API[FastAPI API service]
    WorkerA[Worker A]
    WorkerB[Worker B]
    Handler[Order-created handler]
    DB[(PostgreSQL 18)]
    Migration[Alembic migration job]

    Producer -->|HTTP with API key| API
    API -->|SQLAlchemy transactions| DB
    WorkerA -->|claim and finalize| DB
    WorkerB -->|claim and finalize| DB
    WorkerA --> Handler
    WorkerB --> Handler
    Handler -->|fulfillment in finalization transaction| DB
    Migration -->|upgrade head| DB
```

Application code supplies validation, authentication, retry policy, handler classification, and fencing checks. PostgreSQL supplies durable commits, row locks, uniqueness, foreign keys, and check constraints. Compose supplies service wiring, database health gating, a one-shot migration service, process restart policies, and API port binding; it does not change application-level delivery semantics.

## Ingestion and duplicate detection

```mermaid
sequenceDiagram
    participant P as Producer
    participant A as API
    participant D as PostgreSQL

    P->>A: POST /v1/events with authentication and idempotency headers
    A->>A: Authenticate key and derive source_id
    A->>A: Validate request and hash canonical content
    A->>D: INSERT event ON CONFLICT DO NOTHING
    alt New source-scoped key
        A->>D: INSERT pending status history
        A->>D: COMMIT
        A-->>P: 201 event
    else Existing key and matching hash
        A->>D: Read existing event
        A->>D: ROLLBACK read transaction
        A-->>P: 200 replay with replay header
    else Existing key and different hash
        A->>D: Read existing event
        A->>D: ROLLBACK
        A-->>P: 409 idempotency_key_conflict
    end
```

The unique constraint is `(source_id, idempotency_key)`. The request cannot provide `source_id`; it comes from the authenticated server-side mapping.

## Claiming, execution, retry, and finalization

```mermaid
flowchart TD
    Scan[Select eligible pending event]
    Lock[FOR UPDATE SKIP LOCKED]
    Claim[Increment attempt and create lease]
    CommitClaim[Commit claim transaction]
    Execute[Execute handler outside claim transaction]
    Classify{Handler result}
    Verify[Lock event and attempt; verify lease]
    Retry{Retryable and below effective limit?}
    Pending[Set pending and schedule next attempt]
    Failed[Set failed]
    Fulfill[Insert source-scoped fulfillment]
    CommitFinal[Commit fenced finalization]

    Scan --> Lock --> Claim --> CommitClaim --> Execute --> Classify
    Classify -->|success| Verify --> Fulfill --> CommitFinal
    Classify -->|retryable failure| Retry
    Retry -->|yes| Pending --> CommitFinal
    Retry -->|no| Failed --> CommitFinal
    Classify -->|permanent or unexpected failure| Failed
```

Eligible rows satisfy `status = pending`, `next_attempt_at <= clock_timestamp()`, and `attempt_count < effective limit`. Ordering is `next_attempt_at`, then `created_at`, then `id`; this is deterministic selection among currently eligible rows, not a global FIFO guarantee.

`FOR UPDATE SKIP LOCKED` lets concurrent workers skip a row another transaction has claimed. The claim transaction atomically changes the event to `processing`, increments `attempt_count`, creates an unfinished attempt row, assigns a UUID lease token and expiry, and appends status history.

Handler execution occurs after the claim transaction commits. Success and failure are finalized in separate transactions. Finalization locks the event and attempt, matches the claim's lease token and attempt number, and checks that the lease has not expired.

## Lease expiry and crash recovery

```mermaid
sequenceDiagram
    participant W1 as Original worker
    participant D as PostgreSQL
    participant W2 as Recovery worker

    W1->>D: Commit processing claim with lease token T1
    W1->>W1: Execute handler
    Note over W1: Process stops or exceeds lease
    W2->>D: Lock expired event with SKIP LOCKED
    W2->>D: Lock matching unfinished attempt
    W2->>D: Finish attempt as lease_expired
    alt Attempts remain
        W2->>D: Set pending and schedule retry
    else Effective limit reached
        W2->>D: Set failed
    end
    W2->>D: Clear lease and commit
    Note over W1,D: A late T1 finalization fails fencing checks
```

If the expired event lacks its matching unfinished attempt, recovery quarantines it as `failed` with an internal consistency error. There is no lease heartbeat; a valid long-running handler can lose ownership if it exceeds the configured lease duration.

## Manual retry authorization and replay

```mermaid
sequenceDiagram
    participant C as Authenticated source
    participant A as API
    participant D as PostgreSQL

    C->>A: POST event retry with Idempotency-Key
    A->>D: Lock source-scoped event FOR UPDATE
    alt Existing grant with same key
        A->>D: Read stored retry request
        A-->>C: 200 stored acceptance with replay header
    else Existing grant with different key
        A-->>C: 409 manual_retry_limit_reached
    else Eligible failed event
        A->>D: Validate contiguous attempts and latest outcome
        A->>D: Insert unique retry request
        A->>D: Set exact limit to attempt_count plus one
        A->>D: Set pending, append history, and commit
        A-->>C: 202 accepted
    else Missing, foreign, or ineligible
        A-->>C: 404 or 409
    end
```

Eligibility requires a source-owned `failed` event, no active lease, no prior override or retry request, contiguous attempt history matching `attempt_count`, and a latest completed outcome of `retryable_failure` or `lease_expired`. `permanent_failure` is not eligible. The event lock serializes concurrent authorization requests. A unique constraint on `event_manual_retry_requests.event_id` independently enforces one grant per event.

## Data model

```mermaid
erDiagram
    EVENTS ||--o{ EVENT_STATUS_HISTORY : records
    EVENTS ||--o{ EVENT_PROCESSING_ATTEMPTS : has
    EVENTS ||--o| EVENT_MANUAL_RETRY_REQUESTS : permits
    EVENTS ||--o| ORDER_FULFILLMENTS : first_event

    EVENTS {
        uuid id PK
        string source_id
        string idempotency_key
        string event_type
        jsonb payload
        string request_hash
        string status
        int attempt_count
        int attempt_limit_override
        timestamp next_attempt_at
        uuid lease_token
        timestamp lease_expires_at
        text last_error
        timestamp created_at
        timestamp updated_at
    }
    EVENT_STATUS_HISTORY {
        uuid id PK
        uuid event_id FK
        string status
        text message
        timestamp created_at
    }
    EVENT_PROCESSING_ATTEMPTS {
        uuid id PK
        uuid event_id FK
        int attempt_number
        uuid lease_token
        timestamp started_at
        timestamp finished_at
        string outcome
        text error
    }
    EVENT_MANUAL_RETRY_REQUESTS {
        uuid id PK
        uuid event_id FK
        string idempotency_key
        int target_attempt_number
        timestamp accepted_at
    }
    ORDER_FULFILLMENTS {
        string source_id PK
        string order_id PK
        uuid first_event_id FK
        timestamp fulfilled_at
    }
```

Important database rules include:

- Events are unique on `(source_id, idempotency_key)`.
- Event status, nonnegative attempt count, lease-state consistency, and per-event override validity are check-constrained.
- Attempts are unique on `(event_id, attempt_number)` and on `lease_token`; active/completed fields and outcomes are constrained.
- Manual retry requests are unique on `event_id`, with a positive target attempt.
- Fulfillments have primary key `(source_id, order_id)` and unique `first_event_id`.
- Foreign keys connect history, attempts, retry requests, and fulfillment to their event.

`event_status_history.message`, `event_processing_attempts.error`, `events.last_error`, and lease tokens are internal. Public history omits messages, and public attempts derive `error_summary` solely from a fixed outcome mapping.

## Transaction boundaries

- Ingestion: event plus initial history commit together. Duplicate reads roll back before returning.
- Claim: row selection, attempt increment, lease assignment, attempt creation, and processing history commit together.
- Recovery: expired-attempt finalization and event rescheduling or failure commit together.
- Success: fulfillment insert/check, attempt completion, lease cleanup, event completion, and history commit together.
- Failure: attempt outcome, sanitized internal error, retry schedule or terminal state, lease cleanup, and history commit together.
- Manual authorization: event lock, eligibility checks, retry audit row, exact override, pending transition, and history commit together.

The handler itself runs outside a database transaction that spans execution. This avoids holding the claim lock for the full handler duration but means handler code may run more than once around crashes and lease expiry.
