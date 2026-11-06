################################################################################
# IAM — EC2 runtime role, GitHub OIDC deploy role, Terraform admin role
#
# THREE SEPARATE ROLES — principle of least privilege:
#
#   ec2_runtime  — assumed by the EC2 instance profile.  Can pull from ECR,
#                  read SSM parameters under /shipsafe/, and write to
#                  CloudWatch Logs.  Cannot push images, modify IAM, or
#                  touch Terraform state.
#
#   github_deploy — assumed by GitHub Actions deploy job via OIDC.  Can push
#                  to ECR, trigger an SSM Run Command against the tagged EC2
#                  instance, and read deploy-time SSM parameters.  Cannot
#                  modify IAM, VPC, or Terraform state.
#
#   github_tf_admin — separate role for infrastructure changes.  Should NOT
#                  be used by the deploy job.  Configured separately and
#                  granted only when operator explicitly runs terraform apply.
#
# GITHUB OIDC SUBJECT FORMAT (verified November 2026)
#   The correct subject format for GitHub Actions is:
#     repo:<owner>/<repo>:ref:refs/heads/<branch>
#   or for environments:
#     repo:<owner>/<repo>:environment:<environment-name>
#
#   IMPORTANT: The old "repo:<owner>/<repo>" format without ref/environment is
#   no longer the recommended trust condition for production use.  Always scope
#   to a specific protected branch OR a named GitHub Environment with required
#   reviewers.
#
# OIDC TRUST SCOPE
#   Trust is narrowed to var.deploy_branch OR var.deploy_environment.
#   Both cannot be empty simultaneously.  Set one per your repository's
#   branch-protection and environment-protection configuration.
################################################################################

# ---------------------------------------------------------------------------
# GitHub OIDC provider
# ---------------------------------------------------------------------------

data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
}

resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  # GitHub's OIDC thumbprint is fetched dynamically; there is no offline fallback.
  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]

  # sts.amazonaws.com is the correct audience for AWS STS AssumeRoleWithWebIdentity.
  client_id_list = ["sts.amazonaws.com"]
}

# ---------------------------------------------------------------------------
# GitHub deploy role (ECR push + SSM Run Command)
# ---------------------------------------------------------------------------

locals {
  # Parse owner and repo name from github_repository (format "owner/repo")
  github_repo_parts = split("/", var.github_repository)
  github_owner      = local.github_repo_parts[0]
  github_repo       = length(local.github_repo_parts) > 1 ? local.github_repo_parts[1] : var.github_repository

  # Target qualifier: environment or branch ref
  github_target_qualifier = (
    var.deploy_environment != ""
    ? "environment:${var.deploy_environment}"
    : "ref:refs/heads/${var.deploy_branch}"
  )

  # Immutable subject format (enforced on GitHub for repositories created/renamed on/after July 15, 2026):
  # repo:<owner>@<owner_id>/<repo>@<repo_id>:<qualifier>
  # For EventLedger: owner_id = "112760992", repo_id = "1438492915"
  github_oidc_sub_immutable = (
    var.github_owner_id != "" && var.github_repository_id != ""
    ? "repo:${local.github_owner}@${var.github_owner_id}/${local.github_repo}@${var.github_repository_id}:${local.github_target_qualifier}"
    : ""
  )

  # Classic name-based subject format (repositories created before July 15, 2026 without opt-in):
  github_oidc_sub_classic = "repo:${var.github_repository}:${local.github_target_qualifier}"

  # Set of permitted subjects: strictly immutable by default, with optional classic fallback
  github_allowed_subjects = compact([
    local.github_oidc_sub_immutable,
    var.allow_classic_oidc_subject ? local.github_oidc_sub_classic : ""
  ])
}

resource "aws_iam_role" "github_deploy" {
  name        = "${var.project_prefix}-github-deploy"
  description = "GitHub Actions deploy role: ECR push and SSM Run Command only."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "GitHubOIDCDeploy"
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
            "token.actions.githubusercontent.com:sub" = local.github_allowed_subjects
          }
        }
      }
    ]
  })

  tags = { Name = "${var.project_prefix}-github-deploy" }
}

# ECR: authenticate and push to the ShipSafe repository only.
resource "aws_iam_policy" "ecr_push" {
  name        = "${var.project_prefix}-ecr-push"
  description = "Allow GitHub Actions to push images to the ShipSafe ECR repository."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuthenticate"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "ECRPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecr:DescribeImages",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = aws_ecr_repository.shipsafe.arn
      }
    ]
  })
}

# SSM: send Run Command to the specifically tagged EC2 instance only.
# The command document is restricted to shipsafe-deploy to prevent arbitrary shell.
resource "aws_iam_policy" "ssm_deploy" {
  name        = "${var.project_prefix}-ssm-deploy"
  description = "Allow GitHub Actions to trigger ShipSafe SSM deployment document."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SSMSendDocument"
        Effect = "Allow"
        Action = "ssm:SendCommand"
        Resource = [
          aws_ssm_document.deploy.arn,
          aws_ssm_document.status.arn,
          aws_ssm_document.rollback.arn,
        ]
      },
      {
        Sid       = "SSMSendTaggedInstance"
        Effect    = "Allow"
        Action    = "ssm:SendCommand"
        Resource  = "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = { StringEquals = { "ssm:resourceTag/Project" = "ShipSafe" } }
      },
      {
        Sid    = "SSMGetCommandResult"
        Effect = "Allow"
        Action = [
          "ssm:GetCommandInvocation",
          "ssm:ListCommandInvocations",
        ]
        Resource = "*"
      },
      {
        Sid    = "SSMReadDeployParams"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/shipsafe/deploy/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "github_deploy_ecr" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = aws_iam_policy.ecr_push.arn
}

resource "aws_iam_role_policy_attachment" "github_deploy_ssm" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = aws_iam_policy.ssm_deploy.arn
}

# ---------------------------------------------------------------------------
# EC2 runtime instance role
# ---------------------------------------------------------------------------

resource "aws_iam_role" "ec2_runtime" {
  name        = "${var.project_prefix}-ec2-runtime"
  description = "EC2 instance role: ECR pull, SSM managed, CloudWatch logs, secrets read."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EC2AssumeRole"
        Effect    = "Allow"
        Principal = { Service = "ec2.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = { Name = "${var.project_prefix}-ec2-runtime" }
}

# AWS managed policy: allows SSM Run Command agent to register and receive commands.
resource "aws_iam_role_policy_attachment" "ec2_ssm_managed" {
  role       = aws_iam_role.ec2_runtime.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# ECR pull from the ShipSafe repository only.
resource "aws_iam_policy" "ecr_pull" {
  name        = "${var.project_prefix}-ecr-pull"
  description = "Allow EC2 to pull images from the ShipSafe ECR repository."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuthenticate"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "ECRPull"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:DescribeImages",
        ]
        Resource = aws_ecr_repository.shipsafe.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ec2_ecr_pull" {
  role       = aws_iam_role.ec2_runtime.name
  policy_arn = aws_iam_policy.ecr_pull.arn
}

# CloudWatch: write logs and metrics.
resource "aws_iam_policy" "cloudwatch_write" {
  name        = "${var.project_prefix}-cloudwatch-write"
  description = "Allow EC2 to write logs and metrics to CloudWatch."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams",
        ]
        Resource = [
          for group in [aws_cloudwatch_log_group.shipsafe, aws_cloudwatch_log_group.api,
            aws_cloudwatch_log_group.worker, aws_cloudwatch_log_group.controller,
          aws_cloudwatch_log_group.caddy] : "${group.arn}:*"
        ]
      },
      {
        # Required by SSM Run Command output discovery; this read API is unscoped.
        Sid      = "CloudWatchLogGroupDiscovery"
        Effect   = "Allow"
        Action   = "logs:DescribeLogGroups"
        Resource = "*"
      },
      {
        Sid      = "CloudWatchMetrics"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "ShipSafe"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ec2_cloudwatch" {
  role       = aws_iam_role.ec2_runtime.name
  policy_arn = aws_iam_policy.cloudwatch_write.arn
}

# ---------------------------------------------------------------------------
# KMS Customer Managed Key for Parameter Store encryption
# ---------------------------------------------------------------------------

resource "aws_kms_key" "parameter_store" {
  description             = "KMS key for ShipSafe runtime Parameter Store secrets encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = { Name = "${var.project_prefix}-kms-parameters" }
}

resource "aws_kms_alias" "parameter_store" {
  name          = "alias/${var.project_prefix}-parameters"
  target_key_id = aws_kms_key.parameter_store.key_id
}

# SSM Parameter Store: read runtime secrets under /shipsafe/runtime/.
# SECRETS DESIGN:
#   All runtime credentials (POSTGRES_PASSWORD, EVENTLEDGER_API_KEY, etc.)
#   are stored as SecureString parameters under /shipsafe/runtime/.
#   The EC2 instance reads them at bootstrap and deploy time.
#   They must NEVER appear in:
#     - Terraform state values (use SSM paths, not actual values)
#     - EC2 user data (readable via IMDS)
#     - GitHub Actions logs (mask with ${{ secrets.* }} or SSM)
#     - Docker Compose env files committed to git
#     - CloudWatch log output
resource "aws_iam_policy" "ssm_read_secrets" {
  name        = "${var.project_prefix}-ssm-read-secrets"
  description = "Allow EC2 to read runtime secrets from SSM Parameter Store."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SSMReadRuntime"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
        ]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/shipsafe/runtime",
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/shipsafe/runtime/*",
        ]
      },
      {
        # AmazonSSMManagedInstanceCore also allows GetParameter(s) on '*'.
        # Explicit deny keeps that managed policy from expanding secret access.
        Sid    = "DenyParameterReadsOutsideRuntime"
        Effect = "Deny"
        Action = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
        NotResource = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/shipsafe/runtime",
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/shipsafe/runtime/*",
        ]
      },
      {
        Sid    = "KMSDecryptSSM"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
        ]
        Resource = [
          aws_kms_key.parameter_store.arn,
        ]
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.aws_region}.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ec2_ssm_secrets" {
  role       = aws_iam_role.ec2_runtime.name
  policy_arn = aws_iam_policy.ssm_read_secrets.arn
}

# S3: read release artifacts only from the explicitly approved bucket/prefix
resource "aws_iam_policy" "s3_read_artifacts" {
  name        = "${var.project_prefix}-s3-read-artifacts"
  description = "Allow EC2 to fetch version-pinned deployment archives from the approved ShipSafe prefix."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadArtifacts"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
        ]
        Resource = "${aws_s3_bucket.release_artifacts.arn}/${var.artifact_release_prefix}/*"
      }
    ]
  })
}

resource "aws_iam_policy" "s3_publish_artifacts" {
  name        = "${var.project_prefix}-s3-publish-artifacts"
  description = "Allow the branch-scoped GitHub role to publish only ShipSafe release archives."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "PublishReleaseArtifacts"
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
      Resource = "${aws_s3_bucket.release_artifacts.arn}/${var.artifact_release_prefix}/*"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "github_deploy_s3_publish" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = aws_iam_policy.s3_publish_artifacts.arn
}

resource "aws_iam_role_policy_attachment" "ec2_s3_artifacts" {
  role       = aws_iam_role.ec2_runtime.name
  policy_arn = aws_iam_policy.s3_read_artifacts.arn
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${var.project_prefix}-ec2-profile"
  role = aws_iam_role.ec2_runtime.name
}
