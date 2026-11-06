# Phase 3C implementation and readiness report

## Decision

**Local Phase 3C implementation is complete; NO-GO for provisioning today.**
Supply the actual production DNS hostname, review/merge the workflow to main
under a separate approval, and regenerate/review the plan with that hostname
before applying. The code deliberately refuses an empty or invalid production
domain. The confirmed notification address is configured only in ignored local
tfvars. Credits are owner-confirmed available; plan a 24-48-hour live window.
No main AWS resources, ECR images, S3 artifacts, SSM commands, DNS records,
pushes or merges were created by this work.

## Delivered

- Separate read-only CI and manually dispatched main-only release workflows,
  pinned official Action commit SHAs, branch/repository/account guards, OIDC
  audience `sts.amazonaws.com`, concurrency without canceling a live deployment,
  and only short-lived AWS credentials. Status/rollback skip the build gate.
- Publisher with an explicit no-network dry-run default. The real workflow
  verifies DNS/account, builds linux/amd64 or verifies an existing immutable
  revision tag, resolves its ECR digest, conditionally publishes a deterministic
  allowlisted source archive/checksum, invokes only approved SSM documents,
  waits for success, and verifies public HTTPS plus HTTP redirect.
- Versioned repository-root artifact layout and per-file manifest hashes. Host
  installation checks source/image/schema identity, hashes, exact membership,
  normalized paths and no links/special files. SHA directories are immutable;
  a host publisher lock includes extraction and all deployment operations.
- Explicit first-release bootstrap versus later deployment in the existing SSM
  document. Failed bootstrap stops application services, preserves PostgreSQL,
  records failure and permits an explicit retry; a healthy bootstrap cannot be
  silently reset. Both API slots are verified before the independent worker.
- Separate production Compose with fail-closed EBS bind mounts, persistent Caddy
  certificates/configuration, CloudWatch awslogs and no public database/API/admin
  ports. Existing local Compose and established controller behavior are retained.
- Production Caddy configuration automatically enables HTTPS and redirects HTTP;
  internal loopback health and public TLS routes switch to the same slot.
  Access logs remove request headers, URIs and response headers. The custom API
  key must never appear in request logs. No production domain was invented.
- Verified Docker Hub manifest pins for existing Python/PostgreSQL/Caddy tags.
  The reproducible runtime identity is the retained ECR digest; new pip rebuilds
  are not claimed bit-identical because Python dependencies remain range-bound.
- Interactive operator-only SecureString provisioning with hidden input, SDK
  values in memory, account/KMS checks, no permanent credentials and no values
  in Terraform/Git/logs. Runtime namespace is limited to the five required keys.
- Existing IAM security controls retained. Publisher GetObject was added only
  for its approved release prefix to verify immutable reruns; ECR layer reads
  target only its one repository. SSM/agent logging now has unscoped read-only
  log-group discovery plus idempotent creation/write only for five ShipSafe
  groups, consistent with AWS's Run Command logging requirements; no runtime secret,
  infrastructure admin, state access or arbitrary-branch trust was added.
- $5 local monthly budget; $2 actual/$4 forecast alerts on pre-credit usage,
  plus existing CPU/status/deployment alarms. The configured SNS subscription
  must be confirmed by email after provisioning. Budgets are not a hard cap.

## Validation evidence

| Check | Result | Scope |
|---|---|---|
| Non-database pytest suite | **229 passed** | Unit/offline, mocked AWS/TLS Terraform, Bash, deterministic archives, simulated publisher/SSM, production bootstrap and Compose parsing |
| PostgreSQL integration suite | **106 passed** | Real isolated local Docker PostgreSQL, migrations and application contracts; test project/volume removed afterward |
| Local blue/green Docker acceptance | **13 checks passed**, 8 image identities | Actual local containers: initial release, promotion, rollback, startup/readiness/auth failures, reversal, worker failure, locking, interruption recovery, no-op redeploy, persistence/schema |
| Production Compose | Passed | Actual `docker compose config --quiet`; no production awslogs containers started |
| Production Caddy, both slots | Passed | Checksum-verified Caddy 2.10.2 binary adapt/validate; no public DNS/certificate issuance or server started |
| Workflow lint | Passed | Checksum-verified actionlint 1.7.12; workflows not run on GitHub |
| Python syntax | Passed | New modules and controller compiled; test imports also compile modules |
| Terraform formatting/validation | Passed | Main configuration |
| Main authenticated plan | **60 add, 0 change, 0 destroy** | Temporary profile credentials, approved account/Region, normal S3 locking enabled and released; no apply |

The actual local acceptance matrix used the modified working tree, with the
then-current HEAD revision label. This is test evidence, not a published release
identity. Real published artifacts use a clean committed main source SHA.
AWS boot, IAM execution, CloudWatch ingestion, ECR/S3 publication, remote SSM,
public DNS, ACME certificates and real HTTPS have **not** been tested live.
No claim of a production deployment or cloud recovery test is made.

## Plan review

The previous plan had 59 additions. The new total is **60**, solely because the
confirmed local recipient enables one `aws_sns_topic_subscription.email`.
Existing planned resources also incorporate the bootstrap operation, updated
artifact handling/user data, scoped artifact reread, and short-window budget
notifications. No new compute, database, load balancer, NAT or orchestration
service was introduced. Main-only immutable OIDC trust, encrypted persistent
EBS, IMDSv2, S3 backend and native locking remain intact.

Ignored review files: `shipsafe/infra/main/shipsafe-phase3c.tfplan` and
`shipsafe-phase3c.plan.txt`. They contain account configuration and the local
notification recipient and must not be committed. The bootstrap state bucket
and its local state were not changed and must remain safe for reuse.

## Remaining gates

1. Owner chooses a real domain and configures it locally before reviewing the
   final apply plan. DNS must later point to the actual instance public IPv4.
2. Separate review/merge approval makes these workflows available on main;
   production OIDC stays main-only. Set repository variables from real outputs
   after provisioning, then run CI before publishing the first release.
3. Separately approve a freshly reviewed apply and schedule teardown within
   24-48 hours. Do not apply automatically from this report or workflow.
4. After provisioning, confirm SNS subscription and supply the five SecureStrings
   with the actual KMS output. Verify first-boot dependencies and SSM registration.
5. Execute live publication/bootstrap/HTTPS/worker/rollback/backup/restore checks
   and distinguish those results from this local evidence.

The [deployment runbook](PHASE3C_RUNBOOK.md) covers first provisioning, secure
parameters, first release, verification, rollback, backup and resource-by-resource
teardown. Keep bootstrap state, inspect retained resources and delayed billing,
and do not treat credits or Budget alerts as a spending limit.

## Full planned resource summary

| Terraform resource type | Additions |
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
| `aws_sns_topic_subscription` | 1 |
| `aws_ssm_document` | 3 |
| `aws_subnet` | 1 |
| `aws_volume_attachment` | 1 |
| `aws_vpc` | 1 |
| `aws_vpc_security_group_egress_rule` | 2 |
| `aws_vpc_security_group_ingress_rule` | 4 |
| **Total** | **60** |

No Terraform warnings or errors. Normal S3 lock acquisition/release succeeded.
