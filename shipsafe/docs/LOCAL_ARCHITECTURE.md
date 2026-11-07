# Local single-host architecture

This page describes local Compose acceptance. For the deployed AWS topology,
HTTPS, EBS and CI/CD trust boundaries, see [AWS handover](AWS_HANDOVER.md).

## Components

ShipSafe uses one Docker Compose project on one Linux host:

```text
127.0.0.1:${SHIPSAFE_PROXY_PORT}
              |
           Caddy :8080
              |
       +------+------+
       |             |
  api-blue:8000  api-green:8000
       |             |
       +------+------+
              |
        PostgreSQL db:5432
              |
   named volume: shipsafe_postgres_data

worker --------------------> PostgreSQL
controller ---> Docker Compose, Caddy reload, runtime state
```

Only Caddy publishes a host port, and it binds to `127.0.0.1`. PostgreSQL,
both API slots, the worker, and Caddy's administration endpoint have no host
port. Application services share one Compose-scoped `backend` network marked
`internal`. Caddy joins that network for upstream access and a separate
Compose-scoped `edge` network so Docker can implement the loopback host
mapping. No other service joins `edge`. Caddy administration listens on
`localhost:2019` inside its own container and is reached only with
`docker compose exec`.

`api-blue` and `api-green` are independent services. Compose image variables
allow two image identities to coexist. The controller uses
`up -d --no-deps --force-recreate` only for the inactive slot; it never runs a
project-wide recreate, orphan cleanup, `down`, or `down -v`.

## Persistence and schema

PostgreSQL owns the Compose named volume `shipsafe_postgres_data`. API, worker,
proxy, and image replacement do not recreate or remove this volume. Bootstrap
runs one explicit migration job targeting `d4e5f6a7b8c9`; ordinary deploy and
rollback commands run no migration. The Phase 1 contract-table migration is
not part of Phase 2.

The runtime environment file is supplied by the operator as a second Compose
`--env-file`. Its contents are never copied to release state or printed by the
controller. Container environments still contain their required credentials,
which are visible to a sufficiently privileged Docker administrator.

## Release lifecycle

1. Resolve the supplied image to its Docker `sha256:` image ID.
2. Record Git SHA plus image ID and create an immutable-by-policy local tag.
3. Acquire the host lock and reserve the inactive slot in atomic JSON state.
4. Start only the inactive API and wait, with a bound, for `/health` and
   `/ready` from inside that container.
5. Run the read-only authenticated `/v1/auth/check` smoke test.
6. Render a slot-specific Caddy candidate, validate it inside Caddy, atomically
   replace the active file, and request a graceful reload.
7. Observe loopback proxy readiness and authenticated behavior for a bounded
   window. On failure, reload the old route while the old API remains alive.
8. Stop the old worker with its 35-second grace period, start the replacement,
   and verify it with a uniquely identified synthetic `order.created` event.
9. If worker verification fails, restore the previous worker and proxy route.
10. Atomically mark the candidate active and retain the old API as the previous
    slot for rollback.

The worker probe creates a durable event and fulfillment whose order ID begins
`SHIPSAFE-PROBE-`. It is intentionally synthetic and idempotency-keyed, but it
is not a read-only database operation. Use a dedicated ShipSafe source identity
where separation from business records matters.

## Interrupted commands

`state.json` records the in-progress phase before each externally visible
transition. A new deploy refuses to proceed while that record exists. `recover`
restores the previous route and worker for interrupted deployments, or resets
an interrupted bootstrap while preserving PostgreSQL and its volume.

State and manifests use write-fsync-replace atomic updates. The lock is an OS
file lock, so it is released if the controller process exits; the file itself
may remain and is harmless.
