# Day 3 Docker demonstration and manual testing

This runbook targets Windows 11, Windows PowerShell 5.1, and Docker Desktop. It uses a dedicated Compose project and never requires access to the development database or guarded integration-test database.

The commands are a reproducible procedure. The final [verified results](#actual-verified-results) are observations from the completed 2026-11-04 acceptance run; they are not a claim that a future run has succeeded before you perform it. No demonstration video has been recorded yet.

## 1. Prerequisites and isolation

From the repository root:

```powershell
Set-Location D:\Personal\Builder\01_EventLedger
docker version
docker compose version

$project = "eventledger-d3demo"
$compose = @(
    "--project-name", $project,
    "--env-file", ".env.delivery",
    "-f", "compose.delivery.yaml"
)
```

Keep this exact `$compose` variable in the PowerShell session. It ensures every subsequent Compose command targets the dedicated delivery project. Do not substitute the development `compose.yaml` or test `compose.test.yaml`.

Check for an existing delivery project before creating anything:

```powershell
docker compose @compose ps -a
```

If it already contains data you need, continue with it rather than creating or deleting its volume.

## 2. Create the delivery environment file

```powershell
Copy-Item .env.delivery.example .env.delivery
notepad .env.delivery
```

Replace every `CHANGE_ME` value. Do not paste a real secret into a command line, chat, screenshot, or committed file.

PowerShell 5.1 does not provide `RandomNumberGenerator.Fill` or `Convert.ToHexString`. This compatible helper can generate a value in memory:

```powershell
function New-HexSecret([int]$ByteCount = 32) {
    $bytes = New-Object byte[] $ByteCount
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

$generatedApiKey = New-HexSecret
$generatedDatabasePassword = New-HexSecret
# Transfer these directly into .env.delivery, then clear them:
# $generatedApiKey = $null
# $generatedDatabasePassword = $null
```

Do not display these variables while recording a demonstration.

## 3. Confirm Git ignores the environment file

```powershell
git check-ignore -v .env.delivery
git status --short
```

The first command should identify the `.env.*` rule in `.gitignore`. `.env.delivery` must not appear as an untracked file.

## 4. Validate Compose without printing secrets

```powershell
docker compose @compose config --quiet
if ($LASTEXITCODE -ne 0) { throw "Compose validation failed" }
```

Do not run `docker compose config` without `--quiet` while recording; expanded output contains configured environment values.

## 5. Build the application image

```powershell
docker compose @compose build
docker image inspect eventledger:day3-delivery --format '{{.Id}}'
```

The multi-stage image installs the project into a Python 3.12 virtual environment and runs API, migration, and worker commands as the non-root `eventledger` user. Secrets are supplied only at container runtime.

## 6. Start PostgreSQL

```powershell
docker compose @compose up -d db
docker compose @compose ps db
```

Wait until the database reports `healthy`. It should show only `5432/tcp`, with no host mapping.

## 7. Run the one-shot migration service

```powershell
docker compose @compose up migrate
docker compose @compose ps -a migrate
```

The migration service should exit with code `0`. It waits for the database health check and runs `python -m alembic upgrade head` once.

## 8. Launch API and worker

```powershell
docker compose @compose up -d api worker
docker compose @compose ps -a
```

Expected services:

- `db`: running and healthy
- `migrate`: exited successfully
- `api`: running and eventually healthy at `127.0.0.1:8001`
- `worker`: running independently

The regular worker receives `WORKER_FAILURE_SIMULATION_ENABLED=false` from Compose regardless of the environment file.

## 9. Inspect status and logs

```powershell
docker compose @compose ps -a
docker compose @compose logs --tail 100 api worker migrate db
```

Do not use `docker compose logs` on unrelated projects. Normal logs include event IDs and attempt information, so review them before sharing publicly.

## 10. Check liveness and readiness

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8001/ready
```

Expected ready-state bodies:

```text
status
------
ok

status
------
ready
```

`/health` is process liveness. `/ready` also verifies PostgreSQL connectivity and the exact Alembic head packaged in the application.

## 11. Load the API key without displaying it

```powershell
$apiKeyLine = Get-Content .env.delivery |
    Where-Object { $_ -match '^EVENTLEDGER_API_KEY=' } |
    Select-Object -First 1
$apiKey = ($apiKeyLine -replace '^EVENTLEDGER_API_KEY=', '').Trim()
if ([string]::IsNullOrWhiteSpace($apiKey)) { throw "API key is missing" }
$authHeaders = @{ "X-API-Key" = $apiKey }
```

Do not type `$apiKey` by itself or interpolate it into diagnostic output.

Authentication check:

```powershell
Invoke-RestMethod `
    -Uri http://127.0.0.1:8001/v1/auth/check `
    -Headers $authHeaders
```

## 12. Submit and observe a real event

```powershell
$orderId = "DAY3-DEMO-$([guid]::NewGuid().ToString('N').Substring(0, 12))"
$ingestionKey = "ingest-$([guid]::NewGuid().ToString('N'))"
$eventBody = @{
    event_type = "order.created"
    payload = @{ order_id = $orderId }
} | ConvertTo-Json -Depth 5 -Compress
$ingestionHeaders = @{
    "X-API-Key" = $apiKey
    "Idempotency-Key" = $ingestionKey
}

$event = Invoke-RestMethod `
    -Method Post `
    -Uri http://127.0.0.1:8001/v1/events `
    -Headers $ingestionHeaders `
    -ContentType application/json `
    -Body $eventBody
$eventId = $event.id
$event
```

Poll until terminal without assuming success:

```powershell
$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    $detail = Invoke-RestMethod `
        -Uri "http://127.0.0.1:8001/v1/events/$eventId" `
        -Headers $authHeaders
} while ($detail.status -notin @("completed", "failed") -and
         (Get-Date) -lt $deadline)
$detail
```

## 13. Inspect attempts, history, and fulfillment

```powershell
Invoke-RestMethod `
    -Uri "http://127.0.0.1:8001/v1/events/$eventId/attempts" `
    -Headers $authHeaders | ConvertTo-Json -Depth 6

Invoke-RestMethod `
    -Uri "http://127.0.0.1:8001/v1/events/$eventId/history" `
    -Headers $authHeaders | ConvertTo-Json -Depth 6
```

Query PostgreSQL without exposing its password:

```powershell
$dbUser = (docker compose @compose exec -T db printenv POSTGRES_USER).Trim()
$dbName = (docker compose @compose exec -T db printenv POSTGRES_DB).Trim()
$sql = "SELECT source_id, order_id, first_event_id, fulfilled_at FROM order_fulfillments WHERE order_id = '$orderId';"
$sql | docker compose @compose exec -T db `
    psql -X -U $dbUser -d $dbName -P pager=off --csv -v ON_ERROR_STOP=1
```

`$orderId` is generated by this run and contains only safe hexadecimal characters after its prefix. Do not substitute untrusted text into SQL.

## 14. Demonstrate ingestion replay and conflict

Identical replay:

```powershell
$replay = Invoke-WebRequest -UseBasicParsing `
    -Method Post `
    -Uri http://127.0.0.1:8001/v1/events `
    -Headers $ingestionHeaders `
    -ContentType application/json `
    -Body $eventBody
$replay.StatusCode
$replay.Headers['X-Idempotent-Replay']
$replay.Content
```

Expected: status `200`, replay header `true`, and the original event ID.

Conflicting reuse:

```powershell
$conflictingBody = @{
    event_type = "order.created"
    payload = @{ order_id = "CONFLICT-$([guid]::NewGuid().ToString('N'))" }
} | ConvertTo-Json -Depth 5 -Compress
try {
    Invoke-RestMethod -Method Post `
        -Uri http://127.0.0.1:8001/v1/events `
        -Headers $ingestionHeaders `
        -ContentType application/json `
        -Body $conflictingBody
    throw "Expected an idempotency conflict"
} catch {
    [int]$_.Exception.Response.StatusCode
    $_.ErrorDetails.Message
}
```

Expected: `409` with code `idempotency_key_conflict`.

## 15. Controlled automatic-failure demonstrations

Failure controls are intentionally ignored by the normal worker. Stop only that delivery worker, create one event at a time, and use short-lived one-off workers with simulation explicitly enabled:

```powershell
docker compose @compose stop worker

function New-DemoEvent([string]$Mode) {
    $demoOrder = "DEMO-$Mode-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    $demoBody = @{
        event_type = "order.created"
        payload = @{ order_id = $demoOrder; _demo_failure = $Mode }
    } | ConvertTo-Json -Depth 6 -Compress
    Invoke-RestMethod -Method Post `
        -Uri http://127.0.0.1:8001/v1/events `
        -Headers @{
            "X-API-Key" = $apiKey
            "Idempotency-Key" = "demo-$([guid]::NewGuid().ToString('N'))"
        } `
        -ContentType application/json `
        -Body $demoBody
}
```

Transient failure followed by success:

```powershell
$transient = New-DemoEvent "transient_once"
docker compose @compose run --rm --no-deps -T `
    -e WORKER_FAILURE_SIMULATION_ENABLED=true `
    -e WORKER_RETRY_BASE_SECONDS=1 `
    -e WORKER_RETRY_MAX_SECONDS=1 `
    worker python -m app.worker --once --log-level INFO
Start-Sleep -Seconds 2
docker compose @compose run --rm --no-deps -T `
    -e WORKER_FAILURE_SIMULATION_ENABLED=true `
    -e WORKER_RETRY_BASE_SECONDS=1 `
    -e WORKER_RETRY_MAX_SECONDS=1 `
    worker python -m app.worker --once --log-level INFO
Invoke-RestMethod `
    -Uri "http://127.0.0.1:8001/v1/events/$($transient.id)/attempts" `
    -Headers $authHeaders | ConvertTo-Json -Depth 6
```

Permanent failure:

```powershell
$permanent = New-DemoEvent "permanent"
docker compose @compose run --rm --no-deps -T `
    -e WORKER_FAILURE_SIMULATION_ENABLED=true `
    worker python -m app.worker --once --log-level INFO
```

Three-attempt exhaustion:

```powershell
$exhaustion = New-DemoEvent "transient_always"
1..3 | ForEach-Object {
    docker compose @compose run --rm --no-deps -T `
        -e WORKER_FAILURE_SIMULATION_ENABLED=true `
        -e WORKER_RETRY_BASE_SECONDS=1 `
        -e WORKER_RETRY_MAX_SECONDS=1 `
        worker python -m app.worker --once --log-level INFO
    if ($_ -lt 3) { Start-Sleep -Seconds 2 }
}
```

Inspect each event's detail, attempts, and history. The expected outcomes are respectively `retryable_failure, completed`; one `permanent_failure`; and three `retryable_failure` rows ending in `failed`.

Restore the regular worker when finished:

```powershell
docker compose @compose start worker
```

## 16. Demonstrate manual retry and replay

Create an event that exhausts an intentionally one-attempt temporary policy:

```powershell
docker compose @compose stop worker
$manual = New-DemoEvent "transient_once"
docker compose @compose run --rm --no-deps -T `
    -e WORKER_FAILURE_SIMULATION_ENABLED=true `
    -e WORKER_MAX_ATTEMPTS=1 `
    worker python -m app.worker --once --log-level INFO

$retryKey = "retry-$([guid]::NewGuid().ToString('N'))"
$retryHeaders = @{
    "X-API-Key" = $apiKey
    "Idempotency-Key" = $retryKey
}
$accepted = Invoke-WebRequest -UseBasicParsing -Method Post `
    -Uri "http://127.0.0.1:8001/v1/events/$($manual.id)/retry" `
    -Headers $retryHeaders
$accepted.StatusCode
$accepted.Headers['Location']
$accepted.Content

$retryReplay = Invoke-WebRequest -UseBasicParsing -Method Post `
    -Uri "http://127.0.0.1:8001/v1/events/$($manual.id)/retry" `
    -Headers $retryHeaders
$retryReplay.StatusCode
$retryReplay.Headers['X-Idempotent-Replay']

docker compose @compose start worker
```

Expected: the first request returns `202` targeting attempt 2; same-key replay returns `200` and replay header `true`; the regular worker completes exactly attempt 2. A fresh retry key for the same event returns `409 manual_retry_limit_reached`.

## 17. Demonstrate crash and lease-expiry recovery

This procedure kills only a temporary container created for the dedicated delivery project:

```powershell
docker compose @compose stop worker
$crashOrder = "CRASH-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$crashBody = @{
    event_type = "order.created"
    payload = @{
        order_id = $crashOrder
        _demo_failure = "slow_once"
        _demo_slow_seconds = 15
    }
} | ConvertTo-Json -Depth 6 -Compress
$crashEvent = Invoke-RestMethod -Method Post `
    -Uri http://127.0.0.1:8001/v1/events `
    -Headers @{
        "X-API-Key" = $apiKey
        "Idempotency-Key" = "crash-$([guid]::NewGuid().ToString('N'))"
    } `
    -ContentType application/json `
    -Body $crashBody

$crashName = "$project-crash-worker"
if (docker ps -aq --filter "name=^/$crashName$") {
    throw "Refusing to reuse an existing container name: $crashName"
}
docker compose @compose run -d --name $crashName --no-deps `
    -e WORKER_FAILURE_SIMULATION_ENABLED=true `
    -e WORKER_LEASE_DURATION_SECONDS=4 `
    worker python -m app.worker --log-level INFO

$deadline = (Get-Date).AddSeconds(10)
do {
    $crashLogs = docker logs $crashName 2>&1 | Out-String
    if ($crashLogs -match "processing_started event_id=$($crashEvent.id)") { break }
    Start-Sleep -Milliseconds 200
} while ((Get-Date) -lt $deadline)
if ($crashLogs -notmatch "processing_started event_id=$($crashEvent.id)") {
    throw "Temporary worker did not claim the target event"
}

$owner = (docker inspect --format `
    '{{index .Config.Labels "com.docker.compose.project"}}' $crashName).Trim()
if ($owner -ne $project) { throw "Refusing to kill a container from another project" }
docker kill $crashName
Start-Sleep -Seconds 5
docker rm $crashName
docker compose @compose start worker
```

Inspect the event until it reaches a terminal state. Expected attempts are `lease_expired` followed by `completed`, with exactly one local fulfillment. The normal worker runs with simulation disabled, so the second attempt does not sleep.

## 18. Confirm persistence across restart

Record one or more event IDs, then restart only this project without deleting its volume:

```powershell
docker compose @compose stop worker api db
docker compose @compose start db
docker compose @compose ps db
# Wait for healthy, then:
docker compose @compose start api worker
Invoke-RestMethod http://127.0.0.1:8001/ready
Invoke-RestMethod `
    -Uri "http://127.0.0.1:8001/v1/events/$eventId" `
    -Headers $authHeaders
```

Submit another event after restart to prove new work is also processed. Do not run `down -v`.

## 19. Verify the migration revision

```powershell
$dbUser = (docker compose @compose exec -T db printenv POSTGRES_USER).Trim()
$dbName = (docker compose @compose exec -T db printenv POSTGRES_DB).Trim()
'SELECT version_num FROM alembic_version;' |
    docker compose @compose exec -T db `
        psql -X -U $dbUser -d $dbName -tA -v ON_ERROR_STOP=1
docker compose @compose exec -T api python -m alembic heads
```

Both should identify the single head `d4e5f6a7b8c9`.

## 20. Stop safely

```powershell
docker compose @compose stop
docker compose @compose ps -a
```

`stop` preserves containers and the named PostgreSQL volume. If you intentionally remove containers and the network, `docker compose @compose down` also preserves the named volume when no volume flag is supplied, but `stop` is the least surprising choice. Never add `-v` when the data must be retained, and do not run global Docker prune commands as part of this demonstration.

Clear in-memory secrets when finished:

```powershell
$apiKey = $null
$apiKeyLine = $null
$authHeaders = $null
$ingestionHeaders = $null
$retryHeaders = $null
```

## 21. Troubleshooting

### PowerShell reports missing `Fill` or `ToHexString`

Those APIs are absent in Windows PowerShell 5.1's .NET runtime. Use the `New-HexSecret` helper in this guide.

### JSON requests return `422 json_invalid`

Do not manually escape JSON for `curl.exe` in PowerShell 5.1. Build a hashtable, pipe it to `ConvertTo-Json`, and send it with `Invoke-RestMethod` or `Invoke-WebRequest` as shown above.

### Compose says a required variable is missing

Confirm `.env.delivery` exists, contains every variable from `.env.delivery.example`, and that the command includes `--env-file .env.delivery`. Validate with `config --quiet`; do not print expanded configuration.

### PostgreSQL rejects credentials after editing the environment file

PostgreSQL initialization credentials apply when a volume is first created. Changing the environment file does not rewrite credentials inside an existing database. Do not delete the volume to work around this if it contains needed data. Restore the matching credentials or create a new, separately named Compose project and volume.

### API is live but not ready

Check `docker compose @compose ps -a` and migration logs. `/health` does not test PostgreSQL. `/ready` requires connectivity and the exact Alembic revision.

### Port 8001 is already in use

Inspect the owner before stopping anything:

```powershell
Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue
docker ps --format 'table {{.Names}}\t{{.Ports}}'
```

Do not stop an unrelated container. Use the intended existing delivery project or deliberately configure an isolated alternate port in a separate Compose override.

### A one-off simulation worker processes the wrong pending event

The worker selects the oldest eligible event, not a requested event ID. Keep the normal worker stopped, ensure the delivery queue has no unrelated eligible pending events, and create/process one demonstration event at a time.

### A retry is still pending

Retry scheduling uses database time. Wait beyond the configured retry delay and run another one-off cycle or restore the regular worker. Do not reset attempt counts manually.

## Actual verified results

The following results were observed in the isolated `eventledger-d3demo` project on 2026-11-04:

- Image: `eventledger:day3-delivery`
- Host API: `http://127.0.0.1:8001`
- Services: PostgreSQL 18, one-shot migration, FastAPI API, independent worker
- Migration: single head `d4e5f6a7b8c9`
- `/health` and `/ready`: `200`
- Database: no host-published PostgreSQL port
- API and worker: non-root `eventledger` user
- Normal worker failure simulation: `false`
- Original event `c5597b30-d351-45ed-ba3e-b7927b3593bc`: completed order `DAY3-7e8ff441d642`, exactly one fulfillment
- Manual retry event `2c0c71ba-7952-4eff-89ad-cb5b40a8afa7`: attempt 1 `retryable_failure`, one authorized attempt 2, then `completed`
- Crash recovery event `327c815a-6d31-4317-bbef-3c9ac96cc41a`: attempt 1 `lease_expired`, attempt 2 `completed`, exactly one fulfillment
- Authentication, source isolation, ingestion replay/conflict, filtering, pagination, permanent failure, bounded exhaustion, manual replay/conflict, persistence across PostgreSQL restart, and fresh-volume startup all passed
- No delivery volume was removed during verification

These are local acceptance results. No video has yet been recorded, no AWS or other cloud deployment was performed, and no load or external exactly-once claim follows from them.
