variable "aws_region" {
  description = "AWS region for the state bucket.  Must match the region used by the main workspace."
  type        = string
}

variable "state_bucket_name" {
  description = <<-EOT
    Globally unique name for the S3 state bucket.
    Recommended pattern: shipsafe-tf-state-<account-id>-<region>
    Example: shipsafe-tf-state-123456789012-us-east-1
  EOT
  type        = string
}

variable "environment" {
  description = "Environment label applied to all resource tags."
  type        = string
  default     = "production"
}
