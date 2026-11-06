################################################################################
# Outputs — ShipSafe main infrastructure
################################################################################

output "ec2_instance_id" {
  description = "EC2 instance ID of the ShipSafe host."
  value       = aws_instance.shipsafe.id
}

output "ec2_public_ip" {
  description = "Public IP of the ShipSafe host.  Point your DNS A record here before bootstrapping."
  value       = aws_instance.shipsafe.public_ip
}

output "ec2_public_dns" {
  description = "Public DNS name of the EC2 instance (AWS-managed)."
  value       = aws_instance.shipsafe.public_dns
}

output "ecr_repository_url" {
  description = "Full ECR repository URL for Docker push/pull commands."
  value       = local.ecr_repo_url
}

output "ecr_registry_url" {
  description = "ECR registry URL (account + region) for `docker login`."
  value       = local.ecr_registry_url
}

output "release_artifact_bucket" {
  description = "Approved S3 bucket for immutable ShipSafe release archives."
  value       = aws_s3_bucket.release_artifacts.bucket
}

output "release_artifact_prefix" {
  description = "Approved release archive key prefix."
  value       = var.artifact_release_prefix
}

output "github_deploy_role_arn" {
  description = "ARN of the GitHub Actions deploy role.  Set as AWS_ROLE_ARN in your deploy workflow."
  value       = aws_iam_role.github_deploy.arn
}

output "ec2_runtime_role_arn" {
  description = "ARN of the EC2 runtime IAM role."
  value       = aws_iam_role.ec2_runtime.arn
}

output "cloudwatch_log_group" {
  description = "Main CloudWatch log group name."
  value       = aws_cloudwatch_log_group.shipsafe.name
}

output "sns_alerts_arn" {
  description = "ARN of the SNS alerts topic."
  value       = aws_sns_topic.alerts.arn
}

output "ebs_data_volume_id" {
  description = "EBS data volume ID (PostgreSQL).  Reference this for snapshots before destroy."
  value       = aws_ebs_volume.data.id
}

output "domain_name" {
  description = "Configured domain name (empty until set in tfvars)."
  value       = var.domain_name
}

output "ssm_runtime_parameter_path" {
  description = "SSM Parameter Store path prefix for runtime secrets."
  value       = "/shipsafe/runtime/"
}

output "kms_parameter_key_arn" {
  description = "ARN of the KMS key for encrypting Parameter Store secrets."
  value       = aws_kms_key.parameter_store.arn
}

output "kms_parameter_key_alias" {
  description = "Alias of the KMS key for Parameter Store secrets."
  value       = aws_kms_alias.parameter_store.name
}

output "deployment_prerequisites" {
  description = "Human-readable checklist of steps required before first bootstrap."
  value       = <<-EOT
    Before bootstrapping ShipSafe:
    1. Confirm terraform plan shows only expected resources.
    2. Run terraform apply and record outputs.
    3. Verify EC2 instance is SSM-managed: aws ssm describe-instance-information
    4. Create SSM SecureString parameters under /shipsafe/runtime/:
         /shipsafe/runtime/POSTGRES_PASSWORD
         /shipsafe/runtime/POSTGRES_DB
         /shipsafe/runtime/POSTGRES_USER
         /shipsafe/runtime/EVENTLEDGER_API_KEY
         /shipsafe/runtime/EVENTLEDGER_SOURCE_ID
    5. Verify cloud-init completed and the exact EBS volume is mounted at /data/shipsafe; never manually format an existing volume.
    6. Configure Docker Compose to use /data/shipsafe for the postgres volume.
    7. Point ${var.domain_name != "" ? var.domain_name : "<your domain>"} A record to: ${aws_instance.shipsafe.public_ip}
    8. Confirm outbound connectivity to ECR, SSM, and CloudWatch endpoints.
    9. Push the EventLedger image to ECR: ${local.ecr_repo_url}
    10. Verify SSM Agent >= 3.3.2746.0 and /var/lib/shipsafe-bootstrap.ready exists.
    11. Implement and review the publisher and first-release bootstrap workflow before deployment (not supplied by this stack).
  EOT
}
