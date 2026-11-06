# Phase 3B plan review (Windows PowerShell)

Use the initialized S3 backend in `shipsafe/infra/main`; do not use
`-lock=false`, `force-unlock`, state migration, or manual lock deletion to
work around a failure. S3 native locking creates a transient `.tflock` object;
that object is a coordination mechanism, not infrastructure provisioning.
The operator requires ListBucket, GetObject/PutObject on the state key, and
GetObject/PutObject/DeleteObject on its `.tflock` key, plus applicable KMS
permissions. The EC2 and GitHub deployment roles must not receive state access.
See [HashiCorp S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3).

Use `AWS_PROFILE=shipsafe-credits`, account `542468167362`, Region `us-east-1`.
The pinned AMI is `ami-0c230065b91270bc6` (available AL2023 x86_64). AMI IDs are
Region-specific; reverify ownership, architecture and availability before
changing Region or the pin. There is no broad DescribeImages lookup.

Prepare ignored `terraform.tfvars` using actual approved identifiers. Production
OIDC stays restricted to `main`, even when Terraform is reviewed on
`feature/shipsafe`. Do not replace the verified immutable subject with a wildcard
or enable classic subject fallback. See [readiness review](PHASE3B1_READINESS.md).

`alarm_email` is a non-secret deployment variable shared by CloudWatch SNS email
and both AWS Budgets notifications. Set it in local tfvars, or omit it there and
set `TF_VAR_alarm_email` in the process. A tfvars assignment takes precedence
over the environment variable. No real notification address has been supplied.
SNS subscriptions require recipient confirmation. An empty value disables email
notifications. Budgets notify; they do not stop resources or cap spending.

The AWS provider version currently cannot read the CLI login session directly.
Export temporary credentials in memory only and clear them in `finally`:

```powershell
Set-Location D:\Personal\Builder\01_EventLedger\shipsafe\infra\main
$env:AWS_PROFILE = 'shipsafe-credits'
$env:AWS_REGION = 'us-east-1'
$env:AWS_DEFAULT_REGION = $env:AWS_REGION
try {
    $credentials = aws configure export-credentials --profile $env:AWS_PROFILE --format process | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or !$credentials.AccessKeyId) { throw 'Credential export failed' }
    $env:AWS_ACCESS_KEY_ID = $credentials.AccessKeyId
    $env:AWS_SECRET_ACCESS_KEY = $credentials.SecretAccessKey
    $env:AWS_SESSION_TOKEN = $credentials.SessionToken
    $identity = aws sts get-caller-identity --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $identity.Account -ne '542468167362') { throw 'Unexpected AWS account' }
    terraform fmt -check
    if ($LASTEXITCODE -ne 0) { throw 'Formatting failed' }
    terraform validate
    if ($LASTEXITCODE -ne 0) { throw 'Validation failed' }
    terraform plan -input=false -lock=true '-lock-timeout=60s' '-var-file=terraform.tfvars' '-out=shipsafe-phase3b1.tfplan'
    if ($LASTEXITCODE -ne 0) { throw 'Plan failed; inspect error without bypassing locking' }
    terraform show -no-color shipsafe-phase3b1.tfplan | Set-Content -Encoding utf8 shipsafe-phase3b1.plan.txt
} finally {
    Remove-Item Env:AWS_ACCESS_KEY_ID,Env:AWS_SECRET_ACCESS_KEY,Env:AWS_SESSION_TOKEN -ErrorAction SilentlyContinue
    $credentials = $null
}
```

Do not print or persist the credential object. Plans, local tfvars, caches and
state stay Git-ignored. Do not apply from this review procedure. Review counts,
destruction/replacement, EBS preservation, IAM scope, notifications, publisher
and runtime prerequisites, and credit eligibility before any later approval.
