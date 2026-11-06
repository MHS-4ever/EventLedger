locals {
  ssm_image_pattern    = "^${data.aws_caller_identity.current.account_id}\\.dkr\\.ecr\\.${replace(var.aws_region, ".", "\\.")}\\.amazonaws\\.com/${aws_ecr_repository.shipsafe.name}@sha256:[0-9a-f]{64}$"
  ssm_artifact_pattern = "^(s3://${replace(var.artifact_bucket_name, ".", "\\.")}/${var.artifact_release_prefix}/[0-9a-f]{40}/shipsafe-release\\.tar\\.gz)?$"
  ssm_guard = replace(templatefile("${path.module}/templates/ssm-guard.sh.tpl", {
    ebs_volume_id = aws_ebs_volume.data.id
  }), "\r\n", "\n")
}

################################################################################
# SSM Documents for controlled remote execution
#
# DESIGN RATIONALE
#   AWS-RunShellScript allows arbitrary shell commands — suitable only during
#   initial setup.  Production operations use custom documents with constrained
#   parameter schemas so the deploy role cannot accidentally run arbitrary code.
#
# DOCUMENTS DEFINED HERE
#   shipsafe-deploy   — trigger a release via the ShipSafe controller.
#                       Parameters: image_ref (ECR digest), git_sha (40 hex chars).
#   shipsafe-status   — read and return controller status.
#                       Parameters: none.
#   shipsafe-rollback — trigger a rollback to the previous verified release.
#                       Parameters: release_id (optional; defaults to previous).
#
# PARAMETER RESTRICTIONS
#   image_ref  must match ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[^@]+@sha256:[0-9a-f]{64}$
#   git_sha    must match ^[0-9a-f]{40}$
#   release_id must match ^[0-9a-f]+-[0-9a-f]+$ or be empty.
#
# SECRETS POLICY
#   No secrets appear in document parameters or command text.
#   Runtime credentials are pre-loaded on the host from SSM Parameter Store
#   into /data/shipsafe/runtime.env (chmod 600, owned by root).
################################################################################

resource "aws_ssm_document" "deploy" {
  name            = "${var.project_prefix}-deploy"
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "Deploy or rollback a ShipSafe release on the target EC2 instance."
    parameters = {
      ImageRef = {
        type              = "String"
        interpolationType = "ENV_VAR"
        description       = "ECR digest reference: <registry>/<repo>@sha256:<digest>"
        allowedPattern    = local.ssm_image_pattern
      }
      GitSha = {
        type              = "String"
        interpolationType = "ENV_VAR"
        description       = "40-character lowercase Git commit SHA"
        allowedPattern    = "^[0-9a-f]{40}$"
      }
      ReadyTimeout = {
        type              = "String"
        interpolationType = "ENV_VAR"
        description       = "Readiness timeout in seconds (60–300)"
        allowedPattern    = "^(6[0-9]|[7-9][0-9]|[12][0-9]{2}|300)$"
        default           = "120"
      }
      WorkerTimeout = {
        type              = "String"
        interpolationType = "ENV_VAR"
        description       = "Worker verification timeout in seconds (30–180)"
        allowedPattern    = "^([3-9][0-9]|1[0-7][0-9]|180)$"
        default           = "90"
      }
      ObservationSeconds = {
        type              = "String"
        interpolationType = "ENV_VAR"
        description       = "Post-promotion observation window in seconds (5–60)"
        allowedPattern    = "^([5-9]|[1-5][0-9]|60)$"
        default           = "15"
      }
      ArtifactS3Uri = {
        type              = "String"
        interpolationType = "ENV_VAR"
        description       = "Optional versioned release artifact in the explicitly approved bucket and prefix"
        allowedPattern    = local.ssm_artifact_pattern
        default           = ""
      }
      ArtifactSha256 = {
        type              = "String"
        interpolationType = "ENV_VAR"
        description       = "Optional SHA256 checksum to cryptographically verify artifact integrity"
        allowedPattern    = "^([0-9a-f]{64})?$"
        default           = ""
      }
    }
    mainSteps = [
      {
        precondition = { StringEquals = ["platformType", "Linux"] }
        action       = "aws:runShellScript"
        name         = "RunShipSafeDeploy"
        inputs = {
          runCommand = [replace(templatefile("${path.module}/templates/ssm-deploy.sh.tpl", {
            guard            = local.ssm_guard
            image_pattern    = local.ssm_image_pattern
            artifact_pattern = local.ssm_artifact_pattern
            aws_region       = var.aws_region
            ecr_registry     = local.ecr_registry_url
          }), "\r\n", "\n")]
          timeoutSeconds = 1800
        }
      }
    ]
  })

  tags = { Name = "${var.project_prefix}-deploy-document" }
}

resource "aws_ssm_document" "status" {
  name            = "${var.project_prefix}-status"
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "Return ShipSafe controller status (no credentials in output)."
    parameters    = {}
    mainSteps = [
      {
        precondition = { StringEquals = ["platformType", "Linux"] }
        action       = "aws:runShellScript"
        name         = "RunShipSafeStatus"
        inputs = {
          runCommand = [replace(templatefile("${path.module}/templates/ssm-status.sh.tpl", {
            guard = local.ssm_guard
          }), "\r\n", "\n")]
          timeoutSeconds = 30
        }
      }
    ]
  })

  tags = { Name = "${var.project_prefix}-status-document" }
}

resource "aws_ssm_document" "rollback" {
  name            = "${var.project_prefix}-rollback"
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "Roll back to the previously verified ShipSafe release."
    parameters = {
      ReleaseId = {
        type              = "String"
        interpolationType = "ENV_VAR"
        description       = "Explicit release ID to roll back to, or empty for automatic previous."
        allowedPattern    = "^([0-9a-f]{12}-[0-9a-f]{12})?$"
        default           = ""
      }
    }
    mainSteps = [
      {
        precondition = { StringEquals = ["platformType", "Linux"] }
        action       = "aws:runShellScript"
        name         = "RunShipSafeRollback"
        inputs = {
          runCommand = [replace(templatefile("${path.module}/templates/ssm-rollback.sh.tpl", {
            guard = local.ssm_guard
          }), "\r\n", "\n")]
          timeoutSeconds = 1800
        }
      }
    ]
  })

  tags = { Name = "${var.project_prefix}-rollback-document" }
}
