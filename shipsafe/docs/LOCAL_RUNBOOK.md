# Local deployment and rollback runbook

## Prerequisites

- Linux host with Docker Engine, Docker Compose v2, and Python 3.11 or newer.
- EventLedger image already built or pulled locally.
- An operator-owned runtime environment based on `runtime.env.example`.
- Database schema remains at `d4e5f6a7b8c9`.

On Windows, invoke `controller.py` directly with the repository virtual
environment. The `shipsafe.sh` wrapper targets Linux. Docker Desktop bind mount,
filesystem locking, and signal behavior are useful local evidence but do not
replace a final Amazon Linux verification.

## Bootstrap

```sh
cp shipsafe/runtime.env.example shipsafe/runtime.env
# Replace every CHANGE_ME value; never commit the file.

docker build -t eventledger:shipsafe-bootstrap .
GIT_SHA=$(git rev-parse HEAD)

sh shipsafe/shipsafe.sh \
  --runtime-env shipsafe/runtime.env \
  bootstrap \
  --image eventledger:shipsafe-bootstrap \
  --git-sha "$GIT_SHA"
```

Bootstrap starts PostgreSQL, waits for health, applies only revision
`d4e5f6a7b8c9`, starts blue, starts loopback Caddy, and verifies the worker.
If interrupted, run `recover`; it stops partial application services but keeps
the database container and named volume.

## Deploy

```sh
sh shipsafe/shipsafe.sh \
  --runtime-env shipsafe/runtime.env \
  deploy \
  --image eventledger:candidate \
  --git-sha "$(git rev-parse HEAD)" \
  --ready-timeout 60 \
  --observation-seconds 15 \
  --worker-timeout 60
```

Do not use mutable `latest` as the only identity. The controller resolves the
supplied reference immediately and records the exact image ID.

## Status and recovery

```sh
sh shipsafe/shipsafe.sh --runtime-env shipsafe/runtime.env status
sh shipsafe/shipsafe.sh --runtime-env shipsafe/runtime.env recover
```

Status contains no credentials. A deploy refuses to overwrite an interrupted
operation; recover must complete first.

## Manual rollback

Rollback defaults to the recorded previous release:

```sh
sh shipsafe/shipsafe.sh \
  --runtime-env shipsafe/runtime.env \
  rollback
```

An explicit `--release-id` is allowed only for a manifest that previously
passed both candidate and worker verification and remains compatible with the
legacy database revision. Rollback starts and verifies the target in the
inactive slot; it does not downgrade PostgreSQL.

## Controlled failure injection

Failure injection is rejected unless explicitly enabled for a local test:

```sh
export SHIPSAFE_ENABLE_FAILURE_INJECTION=1
```

Supported deploy modes are `candidate-startup`, `candidate-readiness`,
`candidate-smoke`, `post-promotion`, `worker-startup`, and
`interrupt-after-switch`. The last mode exits the controller process with code
75 after routing changes, leaving an in-progress state specifically for testing
`recover`.

Never enable failure injection on the AWS host during normal operation. The
manual AWS workflow exposes only the separately gated post-promotion test; see
[AWS demonstration](../../docs/SHIPSAFE_ROLLBACK_DEMO.md). The broader controller
failure modes above are local/operator testing capabilities, not workflow inputs.

## Safe stop

Stop named services without removing containers or volumes:

```sh
docker compose \
  --project-name shipsafe-local \
  --env-file shipsafe/runtime/compose.env \
  --env-file shipsafe/runtime.env \
  -f shipsafe/compose.yaml \
  stop
```

Never run `docker compose down -v`. The controller itself never runs `down`,
volume removal, orphan removal, or project-wide forced recreation.
