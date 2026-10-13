data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "cryostream"
  }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = {
    Name = "cryostream-private-${count.index}"
  }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "cryostream-private"
  }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Free (no hourly/per-GB charge), unlike Interface endpoints. This is what
# lets Lambda reach S3/DynamoDB from private subnets with no NAT Gateway.
#
# Each endpoint carries a policy scoped to this stack's own resources only
# — a Gateway Endpoint with no policy defaults to allowing access to ANY
# S3 bucket or DynamoDB table reachable through it, not just this
# project's. The Lambda IAM policies (infra/iam.tf) already restrict this
# independently; this is a second, defense-in-depth layer so a future
# widening of those IAM policies can't turn this endpoint into an exit
# path to unrelated data.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "RestrictToVideoBucket"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListBucket"]
      Resource = [
        "arn:aws:s3:::${var.video_bucket_name}",
        "arn:aws:s3:::${var.video_bucket_name}/*"
      ]
    }]
  })
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "RestrictToVideoExpirationsTable"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
      Resource = [
        aws_dynamodb_table.video_expirations.arn,
        "${aws_dynamodb_table.video_expirations.arn}/index/*"
      ]
    }]
  })
}

resource "aws_security_group" "lambda" {
  name        = "cryostream-lambda"
  description = "Security group for cryostream Lambda functions"
  vpc_id      = aws_vpc.main.id

  # Egress is wide on paper (0.0.0.0/0:443), but there is no NAT Gateway or
  # Internet Gateway attached to this VPC's route table — the only
  # reachable HTTPS destinations are the S3/DynamoDB Gateway Endpoints
  # above. The route table, not this security group, is what actually
  # confines traffic.
  egress {
    description = "HTTPS to AWS service endpoints reachable via the route table"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "cryostream-lambda"
  }
}
