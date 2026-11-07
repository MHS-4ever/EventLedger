# AWS teardown and redeployment checklist

This is a future operator checklist, not an executed cleanup record. Confirm
ownership and approval before destructive work. Target only ShipSafe resources
in account `542468167362`, Region `us-east-1`; tags aid identification but do not
replace exact IDs, state inventory and dependency review. Do not delete unrelated
AWS resources, shared OIDC providers or state infrastructure as a shortcut.

## Preserve evidence and decide data retention

- Record the source SHA, active/previous/worker releases, active slot, pending
  state, SSM command IDs, workflow links and timestamped external readiness
  observations. Export sanitized controller/CloudWatch evidence before retention
  expires. See [recorded results](AWS_RESULTS.md).
- Confirm no workflow, SSM command or controller operation remains running.
  Stop new traffic/writes and coordinate worker shutdown for a consistent backup.
  Capture Terraform outputs/resource inventory without publishing state or secrets.
- If data must survive, take PostgreSQL `pg_dump -Fc` from the running database
  using the existing protected container environment, never a password argument
  or logged decrypted value. Use a Linux operator shell with safe binary
  redirection; avoid legacy Windows PowerShell binary-pipeline corruption.
  Copy the dump to approved encrypted storage outside this host/data volume.
- Verify backup checksum/readability (`pg_restore --list`), then actually restore
  into an isolated compatible PostgreSQL instance and check schema, representative
  rows and application readiness. A successful dump/list alone is not a restore
  test. Record the restore result before approving destruction.
- Review/create an EBS snapshot under a separately approved retention decision;
  quiesce PostgreSQL or stop it cleanly for a consistent physical snapshot. Record
  snapshot ID, completion, encryption/KMS dependency, volume ID/AZ and retention
  owner. A snapshot is not automatically an application-consistent backup.
- Preserve required keys and secrets securely. A backup encrypted by a deleted
  KMS key may be unusable. A dump on `/data/shipsafe/backups` alone is not disaster recovery.

## Review the exact destroy scope

The following commands are **examples for an authorized operator** with the
correct main remote backend already initialized. They are not run by this
documentation task and do not authorize an apply:

```powershell
aws sts get-caller-identity --profile shipsafe-credits --region us-east-1
terraform -chdir=shipsafe/infra/main state list
terraform -chdir=shipsafe/infra/main plan -destroy -out=shipsafe-teardown.tfplan
terraform -chdir=shipsafe/infra/main show -no-color shipsafe-teardown.tfplan
```

Ensure Terraform uses the intended authorized credentials as well as the CLI
profile; `--profile` on the first command does not configure Terraform. Use
normal state locking. Inspect all deletions/replacements and halt for unknown
resources or drift. Saved plan files may contain sensitive values.

The data EBS resource has `prevent_destroy=true`, so a blanket destroy plan is
expected to fail until a deliberate retention decision is implemented. Do not
remove the guard just to make the command pass. The configured requirement is a
verified dump backup plus EBS snapshot before removing that protection through
a separately reviewed change. Retaining the volume instead requires an explicit
state/resource ownership plan; `terraform state rm` is not cleanup and can orphan
billable storage. Keep the bootstrap state bucket's own destroy protection.

## DNS and resource checklist

1. Review the external DNS A/AAAA records, TTL and consumers. Plan removal or a
   controlled replacement before deleting EC2; stale DNS to a released public
   IP is unsafe. DNS is not managed by this Terraform stack. Preserve the domain
   registration unless separately intended for deletion.
2. Review the main-managed EC2/volume attachment, VPC/subnet/routes/internet
   gateway/security group, IAM roles/policies/instance profile/OIDC provider,
   SSM documents, ECR/lifecycle, artifact S3/configuration, KMS/alias,
   CloudWatch groups/filters/alarms, SNS/subscription and Budget. Apply a reviewed
   destroy plan only under separate authorization; never force-unlock, force-detach
   or replace an existing volume to bypass a blocked teardown.
3. Verify EC2 termination and root-volume disposition. Stopping EC2 leaves EBS
   and other services chargeable. The stack uses an auto-assigned public IPv4,
   not a planned Elastic IP; inspect unexpected allocated addresses separately.
4. Nonempty ECR/S3 can block destruction. Decide whether required images and
   archives are retained or deleted. Review ECR image digests and versioned S3
   objects, noncurrent versions, delete markers and incomplete multipart uploads.
   Emptying only current objects is insufficient. Operate on the exact approved
   artifact bucket/prefix, never the Terraform state bucket or unrelated repositories.
5. Inventory unattached EBS volumes, all retained snapshots and backup exports.
   Assign retention, cost owner and deletion date. Preserve images/source needed
   for rollback until that window explicitly closes.
6. Review CloudWatch log exports/retention, custom metrics, alarms, SNS and Budget.
   Retaining logs can retain costs. Confirm any intentionally retained resources
   rather than assuming a successful Terraform command removes everything.
7. The five runtime SecureStrings were provisioned outside Terraform; decide
   their deletion/retention separately without displaying their values. Review
   KMS dependencies before scheduling key deletion; key deletion affects secrets
   and any dependent retained encrypted data. Do not delete shared keys/providers.
8. Preserve the bootstrap local state/backup and remote state bucket/history
   securely for reuse/audit. Do not run bootstrap destroy or empty its bucket
   while main resources or recovery needs remain. Record retained state costs.

## Confirm cleanup and reproduce later

Reconcile Terraform state with AWS inventories after cleanup, using exact IDs
and project ownership. Check EC2, volumes/snapshots, ECR, both S3 purposes,
CloudWatch, KMS and address allocations. Check Billing/Cost Explorer after data
settles and again later; budgets/credits are neither immediate nor a hard cap.
Keep a list of every retained billable resource and its owner. No fixed final
bill is promised by this guide.

For redeployment, retain the reviewed Git commit, provider lockfiles, backend
configuration, secret-recovery plan, image/artifact identities and verified
backup. Use [AWS handover prerequisites](AWS_HANDOVER.md#infrastructure-and-first-deployment)
and generate a fresh plan against the actual retained state. Do not reuse a
saved teardown/provisioning plan. Check AMI availability, Region/AZ placement,
domain/IP and OIDC settings for the new environment. A retained volume must not
be formatted; restoring a snapshot/new volume or database backup needs explicit
attachment/mount/ownership verification. Update repository instance/IP variables
and DNS, ensure the SSM default document version and Python 3.12 are correct,
then choose bootstrap versus recovery based on existing controller/database
state. Never bootstrap blindly over retained data. Verify HTTPS, API and worker
independently and capture new evidence.
