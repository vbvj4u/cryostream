resource "aws_dynamodb_table" "video_expirations" {
  name           = "video_expirations"
  billing_mode   = "PROVISIONED"
  read_capacity  = 5
  write_capacity = 5
  hash_key       = "s3_key"

  attribute {
    name = "s3_key"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "expiration_at"
    type = "N"
  }

  # 5/5 base + 5/5 GSI = 10/10 total, well inside DynamoDB's 25/25
  # Always-Free aggregate (see Global Constraints for why 25/25 on each
  # would have exceeded it).
  global_secondary_index {
    name            = "status-expiration_at-index"
    hash_key        = "status"
    range_key       = "expiration_at"
    projection_type = "ALL"
    read_capacity   = 5
    write_capacity  = 5
  }

  tags = {
    Name = "cryostream"
  }
}
