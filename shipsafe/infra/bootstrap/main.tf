################################################################################
# ShipSafe — S3 Terraform State Bootstrap
#
# PURPOSE
#   Creates the S3 bucket that holds Terraform state for the main ShipSafe
#   infrastructure.  This configuration is intentionally minimal and uses a
#   local backend so it can be applied before the remote state bucket exists.
#
# USAGE
#   1. Set the required variables in terraform.tfvars (see terraform.tfvars.example).
#   2. terraform init
#   3. terraform plan -out=tfplan
#   4. Review the plan.  It creates exactly one S3 bucket and nothing else.
#   5. terraform apply tfplan
#   6. Record the bucket name in shipsafe/infra/main/backend.tf.
#
# SAFETY
#   Do NOT run terraform destroy on this configuration after main infrastructure
#   exists: destroying the state bucket while remote state is stored inside it
#   will orphan every resource managed by the main workspace.
#
# DATA-LOSS WARNING
#   Deleting this bucket permanently deletes all Terraform state for the main
#   ShipSafe infrastructure.  Take a manual S3 object backup (or enable
#   versioned object recovery) before any bucket-level operation.
################################################################################

terraform {
  required_version = ">= 1.10, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 6.0"
    }
  }

  # Local backend: state for this tiny bootstrap lives in the repository
  # working directory (not inside the bucket it creates).
  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "ShipSafe"
      Environment = var.environment
      ManagedBy   = "Terraform"
      Component   = "state-bootstrap"
    }
  }
}

# ---------------------------------------------------------------------------
# S3 state bucket
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "tf_state" {
  bucket = var.state_bucket_name

  # Prevent accidental deletion of a bucket that contains live Terraform state.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      # Keep 90 days of old state versions for recovery; adjust to taste.
      noncurrent_days = 90
    }
  }
}

# Enforce in-transit encryption (TLS) for all operations on the state bucket
resource "aws_s3_bucket_policy" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnforceTLSRequestsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.tf_state.arn,
          "${aws_s3_bucket.tf_state.arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Outputs consumed by the main workspace backend configuration
# ---------------------------------------------------------------------------

output "state_bucket_name" {
  description = "Name of the S3 bucket that stores main Terraform state."
  value       = aws_s3_bucket.tf_state.bucket
}

output "state_bucket_arn" {
  description = "ARN of the S3 state bucket."
  value       = aws_s3_bucket.tf_state.arn
}

output "state_bucket_region" {
  description = "Region of the S3 state bucket."
  value       = aws_s3_bucket.tf_state.region
}
