# Phase 3B authentication and plan review (Windows PowerShell)

These commands authenticate only for the current PowerShell process. They do
not apply Terraform. Obtain the temporary access key, secret, and session token
through the approved operator process; never save them in the repository.

```powershell
Set-Location D:\Personal\Builder\01_EventLedger

$env:AWS_ACCESS_KEY_ID = Read-Host 'Temporary AWS access key ID'
$secret = Read-Host 'Temporary AWS secret access key' -AsSecureString
$env:AWS_SECRET_ACCESS_KEY = [Net.NetworkCredential]::new('', $secret).Password
$token = Read-Host 'Temporary AWS session token' -AsSecureString
$env:AWS_SESSION_TOKEN = [Net.NetworkCredential]::new('', $token).Password
$env:AWS_REGION = Read-Host 'Approved AWS Region'
$env:AWS_DEFAULT_REGION = $env:AWS_REGION

aws sts get-caller-identity
aws configure get region
```

Confirm the returned account and Region are the approved ShipSafe target before
continuing. Prepare `shipsafe/infra/main/terraform.tfvars` locally from the
example, including the approved artifact bucket, prefix, repository identity,
and `deploy_branch = "feature/shipsafe"` for feature-branch review.

After the separately reviewed bootstrap stack has created the state bucket:

```powershell
Set-Location D:\Personal\Builder\01_EventLedger\shipsafe\infra\main

$stateBucket = Read-Host 'Approved Terraform state bucket'
terraform init -reconfigure `
  -backend-config="bucket=$stateBucket" `
  -backend-config="key=shipsafe/main/terraform.tfstate" `
  -backend-config="region=$env:AWS_REGION" `
  -backend-config="use_lockfile=true"

terraform fmt -check -recursive
terraform validate
terraform plan -input=false -lock-timeout=5m `
  -var-file=terraform.tfvars `
  -out=shipsafe-phase3b.tfplan
terraform show -no-color shipsafe-phase3b.tfplan | Tee-Object shipsafe-phase3b.plan.txt
```

Review the plan for the exact account, Region, ECR repository, artifact bucket
and prefix, OIDC subject, instance replacement, EBS replacement/detachment, IAM
wildcards, and unexpected destroy actions. Do not run `terraform apply` from
this review procedure.

Clear credentials when review is finished:

```powershell
Remove-Item Env:AWS_ACCESS_KEY_ID,Env:AWS_SECRET_ACCESS_KEY,Env:AWS_SESSION_TOKEN `
  -ErrorAction SilentlyContinue
$secret = $null
$token = $null
```
