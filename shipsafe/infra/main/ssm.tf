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
        allowedPattern    = "^${data.aws_caller_identity.current.account_id}\\.dkr\\.ecr\\.${var.aws_region}\\.amazonaws\\.com/${aws_ecr_repository.shipsafe.name}@sha256:[0-9a-f]{64}$"
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
        allowedPattern    = "^[1-9][0-9]{1,2}$"
        default           = "120"
      }
      WorkerTimeout = {
        type              = "String"
        interpolationType = "ENV_VAR"
        description       = "Worker verification timeout in seconds (30–180)"
        allowedPattern    = "^[1-9][0-9]{1,2}$"
        default           = "90"
      }
      ObservationSeconds = {
        type              = "String"
        interpolationType = "ENV_VAR"
        description       = "Post-promotion observation window in seconds (5–60)"
        allowedPattern    = "^[1-9][0-9]?$"
        default           = "15"
      }
      ArtifactS3Uri = {
        type              = "String"
        interpolationType = "ENV_VAR"
        description       = "Optional versioned release artifact in the explicitly approved bucket and prefix"
        allowedPattern    = "^(s3://${replace(var.artifact_bucket_name, ".", "\\.")}/${var.artifact_release_prefix}/[0-9a-f]{40}/shipsafe-release\\.tar\\.gz)?$"
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
        action = "aws:runShellScript"
        name   = "RunShipSafeDeploy"
        inputs = {
          runCommand = [
            "set -euo pipefail",
            "if [ ! -f /data/shipsafe/.shipsafe_ebs_mounted ]; then echo 'FATAL: /data/shipsafe/.shipsafe_ebs_mounted missing! Refusing deployment to prevent data loss on root disk.' >&2; exit 1; fi",
            "TARGET_IMAGE_REF=\"$${SSM_ImageRef}\"",
            "TARGET_GIT_SHA=\"$${SSM_GitSha}\"",
            "TARGET_READY_TIMEOUT=\"$${SSM_ReadyTimeout}\"",
            "TARGET_WORKER_TIMEOUT=\"$${SSM_WorkerTimeout}\"",
            "TARGET_OBS_SECONDS=\"$${SSM_ObservationSeconds}\"",
            "TARGET_ARTIFACT_URI=\"$${SSM_ArtifactS3Uri}\"",
            "TARGET_ARTIFACT_SHA=\"$${SSM_ArtifactSha256}\"",
            "RUNTIME_ENV=/data/shipsafe/runtime.env",
            "if [ ! -f \"$RUNTIME_ENV\" ]; then echo 'runtime.env not found; synchronizing from SSM Parameter Store...' >&2; /usr/local/bin/shipsafe-fetch-secrets; fi",
            "if [ -n \"$TARGET_ARTIFACT_URI\" ]; then echo \"Fetching release artifact: $TARGET_ARTIFACT_URI\"; aws s3 cp \"$TARGET_ARTIFACT_URI\" /tmp/shipsafe-artifact.tar.gz; if [ -n \"$TARGET_ARTIFACT_SHA\" ]; then printf '%s  /tmp/shipsafe-artifact.tar.gz\\n' \"$TARGET_ARTIFACT_SHA\" | sha256sum -c -; fi; mkdir -p /opt/shipsafe; tar -xzf /tmp/shipsafe-artifact.tar.gz -C /opt/shipsafe; rm -f /tmp/shipsafe-artifact.tar.gz; fi",
            "aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${local.ecr_registry_url}",
            "docker pull \"$TARGET_IMAGE_REF\"",
            "cd /opt/shipsafe && python3 controller.py --runtime-env \"$RUNTIME_ENV\" --runtime-dir /data/shipsafe/runtime deploy --image \"$TARGET_IMAGE_REF\" --git-sha \"$TARGET_GIT_SHA\" --ready-timeout \"$TARGET_READY_TIMEOUT\" --worker-timeout \"$TARGET_WORKER_TIMEOUT\" --observation-seconds \"$TARGET_OBS_SECONDS\"",
          ]
          timeoutSeconds = 600
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
        action = "aws:runShellScript"
        name   = "RunShipSafeStatus"
        inputs = {
          runCommand = [
            "set -euo pipefail",
            "if [ ! -f /data/shipsafe/.shipsafe_ebs_mounted ]; then echo 'FATAL: /data/shipsafe/.shipsafe_ebs_mounted missing!' >&2; exit 1; fi",
            "cd /opt/shipsafe && python3 controller.py --runtime-env /data/shipsafe/runtime.env --runtime-dir /data/shipsafe/runtime status",
          ]
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
        allowedPattern    = "^([0-9a-f]+-[0-9a-f]+)?$"
        default           = ""
      }
    }
    mainSteps = [
      {
        action = "aws:runShellScript"
        name   = "RunShipSafeRollback"
        inputs = {
          runCommand = [
            "set -euo pipefail",
            "if [ ! -f /data/shipsafe/.shipsafe_ebs_mounted ]; then echo 'FATAL: /data/shipsafe/.shipsafe_ebs_mounted missing!' >&2; exit 1; fi",
            "TARGET_RELEASE_ID=\"$${SSM_ReleaseId}\"",
            "RELEASE_ARG=\"\"",
            "if [ -n \"$TARGET_RELEASE_ID\" ]; then RELEASE_ARG=\"--release-id $TARGET_RELEASE_ID\"; fi",
            "cd /opt/shipsafe && python3 controller.py --runtime-env /data/shipsafe/runtime.env --runtime-dir /data/shipsafe/runtime rollback $RELEASE_ARG",
          ]
          timeoutSeconds = 300
        }
      }
    ]
  })

  tags = { Name = "${var.project_prefix}-rollback-document" }
}
