output "api_invoke_url" {
  value = "${aws_api_gateway_stage.prod.invoke_url}/videos"
}

output "api_key_id" {
  value = aws_api_gateway_api_key.this.id
}

output "dynamodb_table_name" {
  value = aws_dynamodb_table.video_expirations.name
}
