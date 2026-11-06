resource "aws_s3_bucket" "release_artifacts" {
  bucket = var.artifact_bucket_name

  tags = { Name = "${var.project_prefix}-release-artifacts" }
}

resource "aws_s3_bucket_versioning" "release_artifacts" {
  bucket = aws_s3_bucket.release_artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "release_artifacts" {
  bucket = aws_s3_bucket.release_artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "release_artifacts" {
  bucket                  = aws_s3_bucket.release_artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "release_artifacts_tls" {
  bucket = aws_s3_bucket.release_artifacts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.release_artifacts.arn,
        "${aws_s3_bucket.release_artifacts.arn}/*",
      ]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}
