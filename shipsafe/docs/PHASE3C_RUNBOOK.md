# Phase 3C deployment runbook

This is a future operator procedure, not authorization to provision. No main
AWS infrastructure, ECR image, release object, SSM invocation or DNS record was
created during Phase 3C. See [implementation evidence](PHASE3C_REPORT.md).

## Decisions and provisioning gate

- Account `542468167362`, Region `us-east-1`, profile `shipsafe-credits`.
- Approved AL2023 AMI `ami-0c230065b91270bc6`; IDs are Region-specific.
- Credits are confirmed available by the owner. Use a **24-48-hour** live window,
  with an explicit teardown appointment before applying. Credit amount, expiry
  and service exclusions are not independently audited here.
- Notification recipient is configured **only in ignored local tfvars**. Confirm
  the SNS subscription using the email received after provisioning. Do not copy
  the owner's address into GitHub variables, tracked examples or documentation.
- Local monthly budget is $5, actual alert $2 and forecast alert $4. Credits and
  refunds are excluded from the cost basis so alerts reflect underlying usage.
  Budgets can be delayed and are **not a hard cap or automatic shutdown**.
- **Missing decision:** a lowercase public DNS hostname controlled by the owner.
  Set `domain_name` in ignored local tfvars and regenerate/review the locked plan
  before apply. Do not use a made-up domain, IP-only TLS or production `.invalid`.
- Review/merge this feature branch separately so the tested release workflow
  exists on **main** before the live window. No merge/push was performed here.
  Feature-branch production OIDC access remains deliberately denied.

Assuming Linux on-demand t3.medium, public IPv4, 40 GiB gp3 and one KMS key,
base usage is about **$1.26/day or $2.51/48 hours**, plus CloudWatch, S3/ECR,
requests, transfer, snapshots and possible T3 CPU surplus. Plan roughly **$3-5**
for a quiet two-day exercise, not a guaranteed bill. Continuous operation is
about $38.22/month before variable services. Stopping EC2 leaves storage and
other retained resources billable. Check billing manually during the window.
Sources: [compute](https://docs.aws.amazon.com/prescriptive-guidance/latest/optimize-costs-microsoft-workloads/right-size-selection.html),
[EBS](https://aws.amazon.com/ebs/pricing/), [IPv4](https://aws.amazon.com/vpc/pricing/),
[KMS](https://aws.amazon.com/kms/pricing/), [CloudWatch](https://aws.amazon.com/cloudwatch/pricing/).

## First provisioning (requires separate approval)

1. Run the CI workflow on the reviewed source. It runs offline tests, isolated
   PostgreSQL integration, production Compose/Caddy validation and the local
   container bootstrap/promotion/worker/rollback acceptance matrix. A source
   change needs a new CI result; offline simulations do not replace this gate.
2. Supply the actual domain; verify the proposed locked plan, account, Region,
   EBS protections and notification settings. Use
   [the temporary-credential plan procedure](PHASE3B_PLAN_REVIEW.md), changing the
   output names to `shipsafe-phase3c`. No `-lock=false` or automatic force-unlock.
3. Only after explicit operator approval, apply the newly reviewed **main** plan.
   Do not apply an older plan with an empty domain. Do not touch the bootstrap
   workspace or migrate/delete its local state. Record outputs without secrets.
4. Verify cloud-init completed, SSM Agent >= 3.3.2746.0 registered, Docker active,
   `/var/lib/shipsafe-bootstrap.ready` present, and `/data/shipsafe` is mounted
   from the exact provisioned EBS volume with a matching canary. The instance
   depends on policy attachments, log groups, routing and egress. Missing EBS,
   old agent or failed package setup prevents release operations; never format
   an existing volume to repair a failed boot.
5. Point the chosen domain's **A** record to `ec2_public_ip`. Do not publish AAAA
   without working IPv6 (this stack has no IPv6 allocation). Allow TCP 80/443;
   no SSH, database, Docker, API-slot or Caddy-admin port is public. The public
   address is auto-assigned and can change after stop/start; recheck DNS/variables.
6. Confirm the SNS email link and verify Budget/SNS recipients in AWS. Do not
   assume a pending subscription delivers alarm emails.

## Secure runtime configuration after the KMS key exists

Create exactly these five Standard-tier **SecureString** parameters, all using
`kms_parameter_key_arn` from the main stack:

| Path | Supply |
|---|---|
| `/shipsafe/runtime/POSTGRES_DB` | Dedicated test database name |
| `/shipsafe/runtime/POSTGRES_USER` | Dedicated database role |
| `/shipsafe/runtime/POSTGRES_PASSWORD` | Strong randomly generated password |
| `/shipsafe/runtime/EVENTLEDGER_API_KEY` | Strong randomly generated API key |
| `/shipsafe/runtime/EVENTLEDGER_SOURCE_ID` | Dedicated synthetic-test source identity |

Use the local interactive operator tool, after installing the optional SDK:

```powershell
.\.venv\Scripts\python.exe -m pip install '.[operators]'
# Set $keyArn to the actual Terraform output; it is not a secret.
.\.venv\Scripts\python.exe -m shipsafe.provision_secrets --key-arn $keyArn
```

The tool verifies account and enabled customer KMS key, prompts without echo,
passes values directly to the SDK in memory, and writes no credential/value
files or logs. The CLI login session is exported in memory, never permanent
access keys. Do not enable SDK debug logging or record the terminal session.
The operator needs SSM PutParameter and KMS encryption/key-description access;
the deployment workflow must not receive these admin permissions. Writes are
not a transaction: inspect metadata after a partial failure before retrying.
`--overwrite` is for deliberate coordinated rotation; changing a stored password
alone does not change an already initialized PostgreSQL role's password.

The current plain env-file format rejects empty/multiline/edge-whitespace
values, dollar signs, quotes, backticks, backslashes and `#`; compatible random
hex values are suitable. Do not put parameters in Terraform, workflow secrets,
user data, command arguments or tracked `.env` files. Verify names/types/key IDs
without displaying decrypted values.

Among runtime/deployment roles, only the EC2 runtime role reads/decrypts this namespace; authorized account administrators retain their own operator access. An explicit deny
prevents its managed SSM policy from reading outside it. The GitHub deploy role
cannot read runtime secrets or decrypt this key. The host retrieves JSON values
at deployment, checks the exact EBS mount, validates all five parameters and
atomically writes root-owned mode-0600 `/data/shipsafe/runtime.env`. Containers
receive the necessary values at runtime; root/Docker administrators can inspect
container environments, so operator access remains privileged.

## First release and verification

Set these **non-secret repository Actions variables** from real outputs:
`SHIPSAFE_DEPLOY_ROLE_ARN`, `SHIPSAFE_INSTANCE_ID`, `SHIPSAFE_PUBLIC_IP`,
`SHIPSAFE_DOMAIN`. Do not configure a GitHub Environment on the release job:
that changes the subject. Required trust is exactly
`repo:MHS-4ever@112760992/EventLedger@1438492915:ref:refs/heads/main`, audience
`sts.amazonaws.com`. No permanent AWS keys are needed.

Dispatch **ShipSafe release** from main with `operation=bootstrap`:

1. CI must succeed. The publisher verifies main/repository/account and DNS,
   builds linux/amd64 with the full source revision label, pushes immutable
   `sha-<40-char-sha>` to the single ECR repository, or reuses its verified digest.
2. The digest identifies the exact runtime image. Python dependencies are not
   lockfile-resolved across new rebuilds; reproducibility here means reusing the
   retained ECR digest, not a claim of bit-identical future pip rebuilds. Python,
   PostgreSQL and Caddy base image manifests are pinned to observed digests.
3. The deterministic gzip/tar archive is built from an allowlisted **Git commit**,
   not the working directory. It contains repository-root app/migrations,
   controller, local/production Compose and `deployment.json` with source SHA,
   ECR digest, schema contract and file hashes. It excludes env/state/infra/tests.
   Archive and checksum go to `releases/<sha>/` using conditional S3 writes.
   Conflicting reruns fail rather than overwrite. Digest/checksum is integrity,
   not a cryptographic signature; main publishing access is privileged.
4. The constrained `shipsafe-deploy` SSM document checks bootstrap/mount/canary,
   takes a host publisher lock, retrieves secrets, verifies archive and image
   identity, and installs `/opt/shipsafe/releases/<sha>` atomically. No links,
   special files, traversal, duplicate members or checksum mismatch are allowed.
5. Controller production mode starts PostgreSQL on the EBS bind mount, migrates
   only to `d4e5f6a7b8c9`, verifies both API slots and read-only authentication,
   starts Caddy and independently starts/verifies the worker with a synthetic
   event. It records the deployment manifest/state on EBS. Missing dependencies
   fail the command; a failed initial bootstrap stops application services,
   retains the database, records failure, and permits an explicit bootstrap retry.
6. Caddy exposes 80/443 with automatic certificates/HTTP redirect for the real
   domain. Certificate/config data persists on EBS. Host-loopback 8080 provides
   controller checks only; Caddy admin 2019 remains container-local. Every switch
   updates both internal and HTTPS upstreams and validates/reloads Caddy.
7. The publisher waits for SSM terminal success, then verifies certificate-trusted
   public HTTPS `/health` and `/ready`, and HTTP-to-HTTPS redirection. Controller
   checks auth and worker processing independently. Check SSM/CloudWatch status,
   both slots, worker, DB revision and synthetic event history. API health alone
   does not prove worker success or a valid certificate.

The five existing CloudWatch groups receive host/container/controller logs.
Docker awslogs uses the EC2 role, not credentials inside app containers.
SSM output needs read-only DescribeLogGroups on `*` and scoped creation/stream
writes for the five predeclared ShipSafe groups; no arbitrary log groups are
allowed. See [AWS Run Command logging permissions](https://docs.aws.amazon.com/systems-manager/latest/userguide/sysman-rc-setting-up-cwlogs.html). Missing
logging permissions prevent container creation. Caddy access logs remove request
headers, URIs and response headers to avoid logging API keys/query data. Do not
run verbose Compose config, environment dumps or secret-bearing shell tracing.

If certificate/DNS verification fails after SSM success, the release may already
be active. Inspect status and fix DNS/certificate reachability; do not blindly
bootstrap again. A canceled Actions run does not necessarily cancel its SSM
command: inspect that command before another operation. Workflow concurrency
and a host lock prevent overlapping publication/deployment.

## Deploy, rollback and recovery

- Subsequent main releases: dispatch `operation=deploy`. The controller verifies
  inactive-slot readiness/auth, switches Caddy, observes the proxy, switches and
  verifies the worker, then records active/previous identities.
- Dispatch `status` or `rollback` from main. These do not wait for build CI or
  publish new artifacts. Rollback accepts an optional recorded release ID and
  requires a previously API/worker-verified compatible image. No previous
  release exists immediately after first bootstrap: rollback must fail safely.
- Keep prior local Docker images, source directories and ECR digests through the
  test/rollback window. The controller checks the image ID rather than trusting
  a mutable local tag. Database downgrade and destructive migration are forbidden.
- For failed initial bootstrap, inspect status/logs, fix the dependency, then
  explicitly retry bootstrap. It never silently resets a healthy installation.
  Interrupted later deployments can require the controller's documented `recover`
  procedure through a separately authorized operator SSM session; the GitHub
  role is not granted arbitrary AWS-RunShellScript access.

## Backup, teardown and retained cost checks

Before destructive operations, stop the worker/writes, take a verified
`pg_dump -Fc` from the database container without a password argument, and copy
it to approved encrypted operator storage **outside this EBS volume**. Take an
EBS snapshot if retained, record volume/release/schema identities and prove a
restore in a disposable database. A same-volume backup is not disaster recovery.
Do not paste dumps, secret files or raw environment output into logs/artifacts.

At the end of 24-48 hours:

- Stop traffic/writes and export non-sensitive test evidence and verified backup.
- Review a locked **main-only** teardown plan separately. EBS `prevent_destroy`
  deliberately blocks blanket destruction; remove that guard only after an
  explicit data-retention decision and verified backup. Never force-detach EBS.
- Remove EC2 instances and confirm instance-store/root volumes are gone. A stopped
  instance still incurs EBS and other charges; stop is not complete teardown.
- Delete or deliberately retain EBS data volumes and snapshots; record ongoing
  storage charges. Confirm no unattached volumes or orphaned snapshots remain.
- Empty/delete only the approved ECR repository and versioned artifact bucket
  if disposable. S3 versions/delete markers and nonempty ECR can block destroy.
  Do not touch unrelated resources or confuse artifacts with Terraform state.
- Review CloudWatch log retention/groups/alarms/custom metrics, SNS subscriptions,
  Budget, KMS key/alias/deletion schedule, IAM/OIDC and network resources. Confirm
  expected main resources are removed or explicitly retained with cost ownership.
- Check public IPv4 and any Elastic IP allocations. No Elastic IP is planned;
  do not leave an unexpectedly allocated billable address. Inspect snapshots,
  backup exports, ECR images, S3 versions and other retained billable resources.
- **Keep** the bootstrap state bucket, main remote state history and bootstrap
  local `terraform.tfstate`/backup safe for reuse. Back up the bootstrap state
  securely. Never run bootstrap destroy, empty its bucket or delete its local
  state as a cleanup shortcut. Expect small retained S3/KMS request/storage costs.
- Inspect Billing/Cost Explorer and service inventories after teardown and again
  after billing data catches up; record every retained resource and expected cost.
  Credits do not remove this obligation or guarantee all services are covered.
