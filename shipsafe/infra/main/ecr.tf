################################################################################
# ECR — Private container registry
#
# Immutable image tags prevent overwriting a published image.
# Digest-based references are enforced at deploy time by the controller.
################################################################################

resource "aws_ecr_repository" "shipsafe" {
  name                 = "${var.project_prefix}/eventledger"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }

  tags = { Name = "${var.project_prefix}-ecr" }
}

# Lifecycle: keep tagged release images for rollback; expire untagged after safe buffer
resource "aws_ecr_lifecycle_policy" "shipsafe" {
  repository = aws_ecr_repository.shipsafe.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 14 days (safety buffer for in-flight digests)"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 14
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Retain the last ${var.ecr_image_retention_count} release images for active and rollback verification"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["sha-", "shipsafe-", "v"]
          countType     = "imageCountMoreThan"
          countNumber   = var.ecr_image_retention_count
        }
        action = { type = "expire" }
      }
    ]
  })
}

locals {
  ecr_registry_url = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
  ecr_repo_url     = "${local.ecr_registry_url}/${aws_ecr_repository.shipsafe.name}"
}
