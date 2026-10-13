data "archive_file" "api" {
  type        = "zip"
  source_file = "${path.module}/../src/api/handler.py"
  output_path = "${path.module}/.build/api.zip"
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/cryostream-api"
  retention_in_days = 14
}

resource "aws_lambda_function" "api" {
  function_name    = "cryostream-api"
  role             = aws_iam_role.api.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.14" # verified latest Lambda-supported runtime during pre-flight
  timeout          = 10
  memory_size      = 128
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.video_expirations.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.api]
}
