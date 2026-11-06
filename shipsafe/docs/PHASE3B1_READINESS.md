# Phase 3B.1 pre-deployment readiness

Historical checkpoint. The current implementation and remaining decisions are
in [Phase 3C](PHASE3C_REPORT.md) and its [runbook](PHASE3C_RUNBOOK.md).

Reviewed on 2026-11-06 on `feature/shipsafe`. Target: account `542468167362`,
Region `us-east-1`, AMI `ami-0c230065b91270bc6` (AL2023 x86_64). No infrastructure
apply, push, merge or resource creation was performed by this review.

## Decision

**Not ready for apply.** The final authenticated plan succeeded with normal S3
native locking: **59 additions, zero changes, zero destroys**, with no Terraform
warnings or errors. Locking is no longer a blocker. Notification recipient,
credit eligibility, production Compose delivery and first-release
publication/bootstrap must still be settled. No apply was run.

## Changes and offline evidence

- The broad AMI search is removed. `ami_id` is required, format-validated, and
  pinned locally and in the tracked example. Reverify AMI availability,
  architecture and Amazon ownership when changing Region or pin.
- EC2 `user_data` now receives plain rendered text, with CRLF normalized to LF.
  The provider performs the single API encoding. Regression tests render the
  real Terraform expressions with mocked providers, verify the initial
  `#!/usr/bin/env bash` header, check the decoded EC2 16 KiB limit, and run Bash
  syntax validation. `user_data_base64` is not configured.
- All three schema-2.2 SSM documents have a Linux precondition and explicitly
  invoke `/bin/bash` through a quoted heredoc, independent of the plugin's
  default shell. Parameters use `SSM_...` environment variables, never inline
  substitution or `eval`. Missing variables fail closed.
- SSM Agent >= **3.3.2746.0** is required for ENV_VAR interpolation. Bootstrap
  updates/installs the agent, checks its version and enables it. No unsafe
  legacy interpolation fallback exists. Actual registration/version still
  needs verification after a separately approved apply. See
  [AWS parameter handling](https://docs.aws.amazon.com/systems-manager/latest/userguide/documents-schemas-features.html).
- Schema and runtime checks restrict image registry/account/Region/repository
  and digest, Git SHA, artifact bucket/prefix/SHA, and 12-hex/12-hex release IDs.
  Timeouts enforce 60-300s readiness, 30-180s worker, and 5-60s observation.
  Deploy/rollback SSM command limits are 1800s to leave time for pulls and checks.
- Artifact downloads use a private temporary directory, require a SHA256 when
  a URI is supplied, require the archive path's SHA to match GitSha, and reject
  archive traversal, links and special files. Rollback uses a quoted Bash array.
  Publisher code is privileged trusted deployment code; checksum validation
  is integrity verification, not a signature or a sandbox.
- All SSM operations verify the mounted filesystem, exact expected EBS canary,
  secret helper, and a bootstrap-complete marker. Bootstrap clears that marker
  before work and creates it only on success.
- Secret retrieval uses AWS CLI JSON/pagination, checks required SecureStrings,
  writes mode-0600 temporary files and atomically replaces runtime.env. Errors
  never print returned values. The current controller/Compose env-file format
  accepts only nonempty single-line values without leading/trailing whitespace,
  dollar signs, quotes, backticks, backslashes or `#`. Incompatible values fail
  before replacing an existing env file; use compatible generated secrets.
  Values are fetched only at deployment, not embedded in user data or Terraform.

Run offline checks from the repository root:

```powershell
terraform fmt -check shipsafe/infra/main
terraform -chdir=shipsafe/infra/main validate
terraform -chdir=shipsafe/infra/main test
.\.venv\Scripts\python.exe -m pytest tests/test_shipsafe_infra_contract.py tests/test_shipsafe_readiness.py tests/test_shipsafe_controller.py -q -p no:cacheprovider
```

Terraform tests use mocked AWS/TLS providers and plan commands only. Python
checks consume the actual mocked rendered documents and exercise Bash input
boundaries, injection rejection, old-agent behavior, and atomic secret writes.
Bash, Terraform, pytest and PyYAML are required; PyYAML is a dev dependency.
On Windows use Git Bash, and a writable pytest temporary directory if needed.

## Verified GitHub trust

The connected repository API and read-only GitHub REST calls returned:

- Repository: `MHS-4ever/EventLedger`, ID `1438492915`.
- Owner ID: `112760992`; default branch `main`.
- Repository created `2026-10-29T13:55:44Z`.
- OIDC settings: `use_default=true`, `use_immutable_subject=true`.
- Subject prefix: `repo:MHS-4ever@112760992/EventLedger@1438492915`.
- Only accepted production subject:
  `repo:MHS-4ever@112760992/EventLedger@1438492915:ref:refs/heads/main`.
- Required audience: `sts.amazonaws.com`; issuer
  `https://token.actions.githubusercontent.com`.

Classic-subject fallback remains false. No branch wildcard or feature-branch
trust is added. A future workflow must request `id-token: write`, explicitly use
AWS's audience, and run from main without a GitHub Environment that changes the
subject. No live Actions token was minted in this review. See
[GitHub OIDC reference](https://docs.github.com/en/actions/reference/security/oidc)
and [subject customization API](https://docs.github.com/en/rest/actions/oidc).

## IAM and first-boot prerequisites

| Area | Review and required behavior |
|---|---|
| EC2 profile | EC2-only trust; SSM core, ECR pull, CloudWatch, runtime parameter read/decrypt, approved artifact-prefix read. All five policy attachments and outbound route/egress are dependencies of EC2 creation. IAM propagation can still require retry/repair. |
| SSM | SendCommand document ARNs and tagged EC2 instance permission are separate statements. Only three custom documents are allowed; arbitrary AWS-RunShellScript is excluded from the deploy role. Result listing requires `Resource="*"`. |
| Parameter Store | Runtime prefix/base is explicitly allowed. An explicit deny outside that namespace counteracts the broader GetParameter(s) permissions in AmazonSSMManagedInstanceCore. This also disallows unrelated AWS public parameters/associations: assess any future agent features before expanding access. |
| KMS | Runtime decrypt/DescribeKey target the one customer-managed key, through SSM only. Removed the ineffective alias-ARN fallback. Parameters must use the planned key, not an arbitrary key or assumed alias fallback. Default key policy delegates to account IAM. |
| ECR | Pull/push scoped to the one repository. GetAuthorizationToken necessarily uses `*`. Immutable tags; lifecycle expiry can remove a rollback image, so verify digests before deployment. |
| Artifacts | Host read and publisher write limited to the approved bucket/prefix. Encryption, TLS-only policy, public blocking and versioning preserved. Versioning does not prevent a permitted publisher overwriting a key. |
| CloudWatch | Write to exactly five configured log groups; namespace-limited PutMetricData uses `*`. Removed unnecessary CreateLogGroup and agent retention updates; Terraform owns retention. Per-container log routing still needs production Compose delivery. |
| GitHub | Exact immutable main subject/audience. Deployment role has no infrastructure-admin or state access. `/shipsafe/deploy/*` read is retained for deployment configuration; no runtime-secret decrypt is granted. A separate Terraform-admin role is described but not created. |
| Network | Public single subnet; ingress only HTTP/HTTPS (80/443), no SSH/PostgreSQL/Docker/admin ports. Outbound unrestricted for SSM/ECR/packages/logs. IPv6 rules exist but the VPC/subnet have no IPv6 allocation. No NAT, ALB, ECS, EKS or RDS introduced. |
| EBS | Separate encrypted gp3 data volume, prevent_destroy, clean stop/detach and no forced detach. Exact-volume discovery, blank-only format, UUID mount and Docker mount/canary guard preserved. `prevent_destroy` is a Terraform configuration guard, not an AWS deletion lock or backup. |

Before **EC2 first boot**, provisioned dependencies must include the profile and
attached policies, log groups, public address/routing/egress, and attachable EBS
volume in the same AZ. The AMI must include working cloud-init, Bash, Python,
AWS CLI and package repositories; bootstrap updates/install packages and needs
outbound access to AWS endpoints and GitHub Compose downloads. IAM eventual
consistency and the 60-second EBS attachment wait can fail boot safely. Wait for
`cloud-init status --wait`, verify `/var/lib/shipsafe-bootstrap.ready`, exact
mount/canary, Docker guard and SSM registration before release operations.
Do not manually format an existing volume to repair a failed boot.

Runtime SecureStrings, image and archive are **not** first-boot inputs; they must
exist before the first application bootstrap. Configure the five required
parameters under `/shipsafe/runtime/` using the planned KMS key without putting
values into Terraform, user data, Git or command logs. Do not run an application
until the verified EBS-backed production Compose setup is installed.

## Remaining deployment gates

1. Locked plan gate completed. After user approval, the exact orphaned lock
   `d1dc241a-bc4a-c2a2-206f-846b3cb908b2` was cleared with Terraform force-unlock
   after rechecking that no local Terraform process existed. The final plan
   ran with `-lock=true -lock-timeout=60s` and succeeded. Never routinely bypass
   locking or remove a lock whose owner may still be active.
2. Supply a real non-secret `alarm_email` (tfvars or TF_VAR_alarm_email) and
   confirm SNS subscription. Local blank override was removed so the environment
   variable works. This review invents no recipient and creates no subscription.
3. Confirm credits, expiry, eligible services and paid/free account plan, plus
   acceptable monthly budget. The current $30 threshold is below the baseline.
4. Implement/review GitHub publisher packaging the repository layout at archive
   root (`shipsafe/controller.py`, `shipsafe/compose.yaml`, migrations, etc.),
   checksumming/publishing to the approved prefix and pushing digest-pinned ECR
   images. Use workflow concurrency; controller locking does not serialize
   archive extraction that happens before controller invocation.
5. Deliver a production Compose configuration: the tracked Compose file is
   still for local use (Docker-managed PostgreSQL named volume and loopback
   Caddy port 8080). It does not yet prove an EBS-backed PostgreSQL bind mount,
   public HTTPS/Caddy certificate persistence or production log routing.
6. Implement/review first-release controller `bootstrap`. The three SSM docs
   provide deploy/status/rollback; deploy is not an initial bootstrap operation.
   Do not grant the deploy role arbitrary shell to work around this gap.
7. Verify DNS/domain, mount/secret/SSM prerequisites, backups and restore, memory
   capacity, runtime health and recovery after a separately approved apply.
   `ignore_changes=[ami,user_data]` means future AMI/bootstrap updates do not
   automatically update an existing host; replacement/repair requires review.

## State locking evidence

The initialized backend is S3 with `use_lockfile=true`, key
`shipsafe/main/terraform.tfstate` in
`shipsafe-tf-state-542468167362-us-east-1`. The first normal authenticated plan
returned **412 PreconditionFailed** because of an orphaned plan lock, demonstrating
that S3 conditional writes enforce exclusion. The user explicitly approved
clearing that exact lock after a local process check showed no Terraform process.

Terraform force-unlock removed the approved lock at `2026-11-06T11:11:28Z`.
The subsequent plan used `-lock=true -lock-timeout=60s` and acquired a fresh lock
at `11:11:34Z`, then released it at `11:11:45Z` (16:11 Pakistan time).
S3 object-version metadata showed the new lock version followed by a latest
delete marker. Both acquisition and release are confirmed. Main state HEAD
remained 404, consistent with no main apply; state contents were not edited.
Temporary AWS credentials were kept in memory and cleared in finally.

The reviewed plan is saved locally as
`shipsafe/infra/main/shipsafe-phase3b1.tfplan`; its readable output is
`shipsafe/infra/main/shipsafe-phase3b1.plan.txt`. Both remain Git-ignored.
See [normal plan procedure](PHASE3B_PLAN_REVIEW.md).

## Cost estimate (before credits, taxes and usage)

Assume Linux on-demand us-east-1, 730 hours/month, one public IPv4, and 40 GiB gp3:

| Item | Approximate monthly USD |
|---|---:|
| t3.medium at $0.0416/hour | 30.37 |
| Public IPv4 at $0.005/hour | 3.65 |
| 40 GiB gp3 at $0.08/GiB-month | 3.20 |
| One customer-managed symmetric KMS key | 1.00 |
| Baseline before telemetry/storage/requests | **38.22** |

CloudWatch custom metrics/alarms/log ingestion, ECR storage/scanning features,
S3 state/artifact versions and requests, KMS requests, snapshots/backups, SNS,
network egress and T3 surplus CPU credits add usage-based costs. A small workload
may be around $40-45/month, but that is not a cap or verified account quote.
Credits and free-tier eligibility remain unverified. AWS Budgets notifications
are delayed alerts, **not a hard spending cap**, and do not shut down resources.

Sources: [AWS instance-size example](https://docs.aws.amazon.com/prescriptive-guidance/latest/optimize-costs-microsoft-workloads/right-size-selection.html),
[gp3 pricing](https://aws.amazon.com/ebs/pricing/),
[IPv4 pricing](https://aws.amazon.com/vpc/pricing/),
[KMS pricing](https://aws.amazon.com/kms/pricing/),
[CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/),
[SSM managed policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonSSMManagedInstanceCore.html).

## Resource summary

The fresh authenticated, normally locked main plan of the reviewed configuration
has the following **59 additions, zero changes and zero destroys**. Account,
Region, approved AMI, main-only OIDC inputs and disabled classic-subject fallback
were verified in the saved plan. No notification email was supplied.

| Terraform resource type | Count |
|---|---:|
| `aws_budgets_budget` | 1 |
| `aws_cloudwatch_log_group` | 5 |
| `aws_cloudwatch_log_metric_filter` | 2 |
| `aws_cloudwatch_metric_alarm` | 3 |
| `aws_ebs_volume` | 1 |
| `aws_ecr_lifecycle_policy` | 1 |
| `aws_ecr_repository` | 1 |
| `aws_iam_instance_profile` | 1 |
| `aws_iam_openid_connect_provider` | 1 |
| `aws_iam_policy` | 7 |
| `aws_iam_role` | 2 |
| `aws_iam_role_policy_attachment` | 8 |
| `aws_instance` | 1 |
| `aws_internet_gateway` | 1 |
| `aws_kms_alias` | 1 |
| `aws_kms_key` | 1 |
| `aws_route_table` | 1 |
| `aws_route_table_association` | 1 |
| `aws_s3_bucket` | 1 |
| `aws_s3_bucket_policy` | 1 |
| `aws_s3_bucket_public_access_block` | 1 |
| `aws_s3_bucket_server_side_encryption_configuration` | 1 |
| `aws_s3_bucket_versioning` | 1 |
| `aws_security_group` | 1 |
| `aws_sns_topic` | 1 |
| `aws_sns_topic_policy` | 1 |
| `aws_ssm_document` | 3 |
| `aws_subnet` | 1 |
| `aws_volume_attachment` | 1 |
| `aws_vpc` | 1 |
| `aws_vpc_security_group_egress_rule` | 2 |
| `aws_vpc_security_group_ingress_rule` | 4 |
| **Total** | **59** |
