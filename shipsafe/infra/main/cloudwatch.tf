################################################################################
# CloudWatch — Log groups, metric filters, and basic alarms
#
# Retention limits prevent unbounded cost accumulation.
# Alarms use SNS for notifications; configure var.alarm_email to subscribe.
################################################################################

# ---------------------------------------------------------------------------
# Log groups
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "shipsafe" {
  name              = "/shipsafe/${var.environment}"
  retention_in_days = var.log_retention_days

  tags = { Name = "${var.project_prefix}-logs" }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/shipsafe/${var.environment}/api"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/shipsafe/${var.environment}/worker"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "controller" {
  name              = "/shipsafe/${var.environment}/controller"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "caddy" {
  name              = "/shipsafe/${var.environment}/caddy"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------------------
# SNS topic for alarm notifications
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.project_prefix}-alerts"
}

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowCloudWatchAlarmsToPublish"
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action   = "sns:Publish"
        Resource = aws_sns_topic.alerts.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:*"
          }
        }
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# ---------------------------------------------------------------------------
# CloudWatch alarms
# ---------------------------------------------------------------------------

# CPU utilisation: alert if above 90% for 5 minutes.
resource "aws_cloudwatch_metric_alarm" "cpu_high" {
  alarm_name          = "${var.project_prefix}-cpu-high"
  alarm_description   = "EC2 CPU utilisation above 90% for 5 minutes"
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 1
  threshold           = 90
  comparison_operator = "GreaterThanThreshold"

  dimensions = { InstanceId = aws_instance.shipsafe.id }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# Instance status check: alert on hardware failure.
resource "aws_cloudwatch_metric_alarm" "instance_status" {
  alarm_name          = "${var.project_prefix}-instance-status"
  alarm_description   = "EC2 instance status check failed"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"

  dimensions = { InstanceId = aws_instance.shipsafe.id }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ---------------------------------------------------------------------------
# Metric filter: deployment events (controller log → custom metric)
#
# The controller prints lines like:
#   "bootstrapped release=..."
#   "deploy succeeded release=..."
#   "rollback succeeded release=..."
#   "recovered active release=..."
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "deployment_events" {
  name           = "${var.project_prefix}-deployment-events"
  log_group_name = aws_cloudwatch_log_group.controller.name
  pattern        = "?bootstrapped ?\"deploy succeeded\" ?\"rollback succeeded\" ?\"recovered active\""

  metric_transformation {
    name          = "DeploymentEvents"
    namespace     = "ShipSafe"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

# Metric filter: failed deployments.
resource "aws_cloudwatch_log_metric_filter" "deployment_failures" {
  name           = "${var.project_prefix}-deployment-failures"
  log_group_name = aws_cloudwatch_log_group.controller.name
  pattern        = "?\"ShipSafe error\""

  metric_transformation {
    name          = "DeploymentErrors"
    namespace     = "ShipSafe"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "deployment_errors" {
  alarm_name          = "${var.project_prefix}-deployment-errors"
  alarm_description   = "One or more ShipSafe deployment errors in the last 5 minutes"
  namespace           = "ShipSafe"
  metric_name         = "DeploymentErrors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ---------------------------------------------------------------------------
# AWS Budgets alert (cost guard)
#
# IMPORTANT: AWS Budgets alerts are notifications only — they do NOT stop
# resources or guarantee a spending cap.  Credits may not apply to all services.
# ---------------------------------------------------------------------------

resource "aws_budgets_budget" "shipsafe" {
  name         = "${var.project_prefix}-monthly-budget"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Alert on underlying usage even when promotional credits cover the bill.
  cost_types {
    include_credit = false
    include_refund = false
  }

  dynamic "notification" {
    for_each = var.alarm_email != "" ? [1] : []
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = var.budget_actual_alert_usd
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alarm_email]
    }
  }

  dynamic "notification" {
    for_each = var.alarm_email != "" ? [1] : []
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = var.budget_forecast_alert_usd
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "FORECASTED"
      subscriber_email_addresses = [var.alarm_email]
    }
  }
}
