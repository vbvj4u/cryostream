data "archive_file" "sweeper" {
  type        = "zip"
  source_file = "${path.module}/../src/sweeper/handler.py"
  output_path = "${path.module}/.build/sweeper.zip"
}

resource "aws_cloudwatch_log_group" "sweeper" {
  name              = "/aws/lambda/cryostream-sweeper"
  retention_in_days = 14
}

resource "aws_lambda_function" "sweeper" {
  function_name    = "cryostream-sweeper"
  role             = aws_iam_role.sweeper.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.14" # verified latest Lambda-supported runtime during pre-flight
  timeout          = 300
  memory_size      = 256
  filename         = data.archive_file.sweeper.output_path
  source_code_hash = data.archive_file.sweeper.output_base64sha256

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      TABLE_NAME  = aws_dynamodb_table.video_expirations.name
      BUCKET_NAME = var.video_bucket_name
      GSI_NAME    = "status-expiration_at-index"
    }
  }

  depends_on = [aws_cloudwatch_log_group.sweeper]
}
