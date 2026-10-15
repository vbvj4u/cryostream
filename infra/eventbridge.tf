resource "aws_iam_role" "scheduler" {
  name = "cryostream-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "cryostream-scheduler"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.sweeper.arn
    }]
  })
}

resource "aws_scheduler_schedule" "sweeper" {
  name       = "cryostream-sweeper"
  group_name = "default"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = var.sweeper_schedule_rate

  target {
    arn      = aws_lambda_function.sweeper.arn
    role_arn = aws_iam_role.scheduler.arn

    # Default is up to 185 retries over 24h - wrong in front of a handler
    # that can legitimately time out mid-copy (reserved_concurrent_executions
    # = 1 on the sweeper means a retry here would just be throttled anyway).
    # A throttled or failed run is already covered by the next scheduled
    # invocation 15 minutes later, so there's nothing useful for Scheduler
    # itself to retry.
    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 60
    }
  }
}
