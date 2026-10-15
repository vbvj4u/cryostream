resource "aws_sns_topic" "alerts" {
  name = "cryostream-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "sweeper_errors" {
  alarm_name          = "cryostream-sweeper-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Triggers when the cryostream sweeper Lambda raises any error"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.sweeper.function_name
  }
}

# The Errors alarm above can't detect a per-item FAILED transition:
# process_item catches every exception and the handler still returns
# normally, so a sweep where every item fails emits zero Lambda Errors.
# This metric filter matches the greppable marker the sweeper logs
# (src/sweeper/handler.py's record_failure) exactly when an item exhausts
# its retries and goes terminally FAILED.
resource "aws_cloudwatch_log_metric_filter" "sweeper_item_failed" {
  name           = "cryostream-sweeper-item-failed"
  log_group_name = aws_cloudwatch_log_group.sweeper.name
  pattern        = "\"SWEEPER_ITEM_FAILED_TERMINAL\""

  metric_transformation {
    name          = "ItemsFailedTerminally"
    namespace     = "Cryostream"
    value         = "1"
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "sweeper_item_failed" {
  alarm_name          = "cryostream-sweeper-item-failed-terminally"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ItemsFailedTerminally"
  namespace           = "Cryostream"
  period              = 900
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Triggers when any video permanently fails archive/delete processing after exhausting retries"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"
}
