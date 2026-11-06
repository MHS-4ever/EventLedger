################################################################################
# ShipSafe — Main Infrastructure
#
# PURPOSE
#   Defines the complete AWS infrastructure for one ShipSafe deployment:
#   VPC, subnet, routing, security group, EC2 instance, ECR repository,
#   CloudWatch log groups / alarms, and GitHub OIDC IAM configuration.
#
# ARCHITECTURE PRESERVED
#   Single EC2 host — Amazon Linux 2023
#   Docker Compose + Caddy on the host
#   Blue/green FastAPI containers
#   PostgreSQL in a Docker named volume on encrypted EBS
#   Worker separately controlled
#   ECR for image storage
#   SSM Run Command for remote execution (no inbound SSH)
#   CloudWatch for logs and alarms
#   GitHub Actions with AWS OIDC (no long-lived keys)
#
# WHAT THIS FILE DOES NOT DO
#   It does not create ECS, EKS, ALB, RDS, NAT Gateway, or CodeDeploy resources.
#   It does not apply.  Run `terraform plan` for review before `terraform apply`.
#
# DATA-LOSS WARNINGS
#   - Replacing or destroying the EC2 instance detaches the EBS data volume.
#     All PostgreSQL data stored on that volume is at risk unless a snapshot
#     exists.  Take a manual EBS snapshot and pg_dump backup before any
#     operation that could replace or destroy the instance.
#   - `terraform destroy` will attempt to delete the EBS data volume unless
#     it has been protected with `prevent_destroy = true` or manually detached
#     first.  Review the plan carefully and take a snapshot before destroy.
################################################################################

terraform {
  required_version = ">= 1.10, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 6.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0, < 5.0"
    }
  }

  # Remote state: fill in the bucket name from the bootstrap output.
  # `use_lockfile = true` uses S3 native object locking instead of DynamoDB.
  backend "s3" {
    # Set via -backend-config or backend.tfvars — never hardcode here.
    # Required keys: bucket, key, region, use_lockfile
    # Example:
    #   bucket      = "shipsafe-tf-state-123456789012-us-east-1"
    #   key         = "shipsafe/main/terraform.tfstate"
    #   region      = "us-east-1"
    #   use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "ShipSafe"
      Environment = var.environment
      Owner       = var.owner_tag
      ManagedBy   = "Terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
