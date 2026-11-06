################################################################################
# Input variables — ShipSafe main infrastructure
#
# Real values go in terraform.tfvars (never committed to git).
# See terraform.tfvars.example for the full list of required values.
################################################################################

# ---------------------------------------------------------------------------
# Provider / region
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region for all resources. Example: us-east-1"
  type        = string
}

# ---------------------------------------------------------------------------
# Project identity / tagging
# ---------------------------------------------------------------------------

variable "project_prefix" {
  description = "Short prefix used in all resource names and tags."
  type        = string
  default     = "shipsafe"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.project_prefix))
    error_message = "project_prefix must contain only lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "environment" {
  description = "Environment label (e.g. production, staging)."
  type        = string
  default     = "production"
}

variable "owner_tag" {
  description = "Owner tag value applied to all resources."
  type        = string
  default     = "Builder"
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR block for the public subnet."
  type        = string
  default     = "10.0.1.0/24"
}

variable "availability_zone_suffix" {
  description = "Single-letter AZ suffix appended to aws_region (e.g. 'a' → us-east-1a)."
  type        = string
  default     = "a"
}

# ---------------------------------------------------------------------------
# EC2 compute
# ---------------------------------------------------------------------------

variable "ami_id" {
  description = "Explicitly approved Amazon Linux 2023 x86_64 AMI ID. AMI IDs are Region-specific; verify availability and ownership in aws_region before planning."
  type        = string

  validation {
    condition     = can(regex("^ami-([0-9a-f]{8}|[0-9a-f]{17})$", var.ami_id))
    error_message = "ami_id must be an AMI ID: ami- followed by 8 or 17 lowercase hexadecimal characters."
  }
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type.  t3.medium (2 vCPU / 4 GiB) is the recommended minimum.
    Measure RAM under load before downsizing; two API containers + PostgreSQL +
    worker can exceed 2 GiB.
    Regional on-demand pricing varies; check https://aws.amazon.com/ec2/pricing/
    before provisioning.
  EOT
  type        = string
  default     = "t3.medium"
}

variable "root_volume_size_gb" {
  description = "Root EBS volume size in GiB (OS and Docker images)."
  type        = number
  default     = 20
}

variable "data_volume_size_gb" {
  description = <<-EOT
    Separate EBS data volume size in GiB (PostgreSQL data).
    DATA-LOSS WARNING: resizing or replacing this volume requires a snapshot
    and verified restore.  Choose a size you do not expect to need to reduce.
  EOT
  type        = number
  default     = 20
}

# ---------------------------------------------------------------------------
# ECR
# ---------------------------------------------------------------------------

variable "ecr_image_retention_count" {
  description = "Number of tagged images to retain in ECR before lifecycle expiry (preserves active and rollback history)."
  type        = number
  default     = 30
}

variable "artifact_bucket_name" {
  description = "Explicitly approved, account-owned S3 bucket for immutable ShipSafe release archives."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.artifact_bucket_name))
    error_message = "artifact_bucket_name must be a valid lowercase S3 bucket name."
  }
}

variable "artifact_release_prefix" {
  description = "Approved key prefix beneath artifact_bucket_name."
  type        = string
  default     = "releases"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9/_-]*[a-z0-9]$", var.artifact_release_prefix)) && !strcontains(var.artifact_release_prefix, "//") && !strcontains(var.artifact_release_prefix, "..")
    error_message = "artifact_release_prefix must be a normalized lowercase prefix without // or ..."
  }
}

# ---------------------------------------------------------------------------
# GitHub OIDC / deployment identity
# ---------------------------------------------------------------------------

variable "github_repository" {
  description = <<-EOT
    GitHub repository in owner/repo format.
    Example: myorg/eventledger
    Used to construct the OIDC subject trust condition.
  EOT
  type        = string
}

variable "github_owner_id" {
  description = <<-EOT
    Immutable numeric GitHub owner ID (user or organisation).
    Enforced for repositories created or renamed on/after July 15, 2026.
    EventLedger owner ID: 112760992
  EOT
  type        = string
  default     = "112760992"
}

variable "github_repository_id" {
  description = <<-EOT
    Immutable numeric GitHub repository ID.
    Enforced for repositories created or renamed on/after July 15, 2026.
    EventLedger repository ID: 1438492915
  EOT
  type        = string
  default     = "1438492915"
}

variable "allow_classic_oidc_subject" {
  description = <<-EOT
    Set to true to accept both classic (repo:owner/repo:...) and immutable
    (repo:owner@owner_id/repo@repo_id:...) OIDC subjects during transition.
    Defaults to false to strictly enforce immutable IDs.
  EOT
  type        = bool
  default     = false
}

variable "deploy_branch" {
  description = <<-EOT
    Branch that is allowed to assume the GitHub deploy role.
    Used when deploy_environment is empty.
    Example: main
  EOT
  type        = string
  default     = "main"
}

variable "deploy_environment" {
  description = <<-EOT
    GitHub Environment name that is allowed to assume the deploy role.
    When set, overrides deploy_branch for OIDC trust.
    Recommended for production — configure required reviewers in GitHub.
    Example: production
    Leave empty to use deploy_branch instead.
  EOT
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# DNS / domain
# ---------------------------------------------------------------------------

variable "domain_name" {
  description = <<-EOT
    Public domain name for Caddy HTTPS termination.
    Example: api.shipsafe.example.com
    DNS must be pointed to the EC2 public IP before Caddy can obtain a
    certificate.  Leave empty during initial infrastructure provisioning;
    configure DNS and set this variable before the first bootstrap.
    This variable is used in outputs only — Caddy configuration is delivered
    at runtime through SSM, not baked into user data.
  EOT
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Observability / cost
# ---------------------------------------------------------------------------

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 30
}

variable "alarm_email" {
  description = "Email address for CloudWatch alarm notifications and AWS Budget alerts. Leave empty to skip."
  type        = string
  default     = ""

  validation {
    condition     = var.alarm_email == "" || can(regex("^[^@[:space:]]+@[^@[:space:]]+[.][^@[:space:]]+$", var.alarm_email))
    error_message = "alarm_email must be empty or a valid notification email address."
  }
}

variable "monthly_budget_usd" {
  description = <<-EOT
    Monthly cost budget threshold in USD.
    An alert notification is sent at 80% actual and 100% forecasted.
    This is a notification only — it does not stop resources.
  EOT
  type        = string
  default     = "30"
}
