# Offline only: mocked providers, plan commands, no remote state or AWS calls.
mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = { account_id = "542468167362" }
  }
  mock_resource "aws_ssm_document" {
    defaults = { arn = "arn:aws:ssm:us-east-1:542468167362:document/offline-mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:542468167362:log-group:offline-mock" }
  }
  mock_resource "aws_ebs_volume" {
    defaults = { id = "vol-0123456789abcdef0" }
  }
}
mock_provider "tls" {
  override_during = plan
  mock_data "tls_certificate" {
    defaults = { certificates = [{ sha1_fingerprint = "0123456789012345678901234567890123456789" }] }
  }
}
variables {
  aws_region           = "us-east-1"
  ami_id               = "ami-0c230065b91270bc6"
  github_repository    = "MHS-4ever/EventLedger"
  artifact_bucket_name = "shipsafe-artifacts-542468167362-us-east-1"
  deploy_branch        = "main"
  alarm_email          = ""
}
run "readiness" {
  command = plan
  assert {
    condition     = startswith(aws_instance.shipsafe.user_data, "#!/usr/bin/env bash\n") && !strcontains(aws_instance.shipsafe.user_data, "\r")
    error_message = "User data must be an unencoded LF shell script for cloud-init."
  }
  assert {
    condition     = length(aws_instance.shipsafe.user_data) < 16384
    error_message = "Do not encode user_data twice or exceed EC2's 16 KiB decoded limit."
  }
  assert {
    condition = alltrue([for document in [aws_ssm_document.deploy, aws_ssm_document.status, aws_ssm_document.rollback] :
      yamldecode(document.content).schemaVersion == "2.2" &&
      yamldecode(document.content).mainSteps[0].precondition.StringEquals == ["platformType", "Linux"] &&
      strcontains(yamldecode(document.content).mainSteps[0].inputs.runCommand[0], "exec /bin/bash <<'SHIPSAFE_SSM_BASH'")
    ])
    error_message = "All SSM commands must use Linux schema 2.2 and explicitly invoke Bash."
  }
  assert {
    condition     = local.github_allowed_subjects == tolist(["repo:MHS-4ever@112760992/EventLedger@1438492915:ref:refs/heads/main"])
    error_message = "Production OIDC trust must remain exactly the verified immutable main subject."
  }
}
run "reject_invalid_ami" {
  command = plan
  variables { ami_id = "ami-not-hex" }
  expect_failures = [var.ami_id]
}
run "reject_invalid_email" {
  command = plan
  variables { alarm_email = "not-an-email" }
  expect_failures = [var.alarm_email]
}
run "notification_configuration" {
  command = plan
  # Synthetic offline test input only; never used in real plans or subscriptions.
  variables { alarm_email = "offline-test@example.invalid" }
  assert {
    condition     = length(aws_sns_topic_subscription.email) == 1 && length(aws_budgets_budget.shipsafe.notification) == 2
    error_message = "One configured email must enable SNS subscription and both budget notifications."
  }
}

run "reject_whitespace_email" {
  command = plan
  variables { alarm_email = "bad address@example.invalid" }
  expect_failures = [var.alarm_email]
}
