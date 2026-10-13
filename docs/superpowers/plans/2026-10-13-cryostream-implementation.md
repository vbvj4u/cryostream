# Cryostream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Terraform + Lambda system that archives (Glacier Deep Archive) or deletes S3 video objects when their individually-tracked expiration date/time arrives.

**Architecture:** A DynamoDB table tracks each video's `action` (ARCHIVE/DELETE) and `expiration_at`. An API Gateway + Lambda lets callers register/inspect/cancel that tracking. An EventBridge-Scheduler-triggered sweeper Lambda queries a DynamoDB GSI for due items every 15 minutes and performs the action against S3. Everything runs in a private VPC with no NAT Gateway (Gateway VPC Endpoints only), deployed by Terraform via GitHub Actions with OIDC auth.

**Tech Stack:** Terraform ≥1.10 (S3 backend, native state locking), Python Lambdas (stdlib + boto3 only, no third-party deps), pytest + moto for unit tests, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-13-cryostream-design.md` (local-only, gitignored — read it alongside this plan; it is not in git history).

## Global Constraints

- Terraform ≥1.10 everywhere (required for native S3 backend state locking — no DynamoDB lock table).
- DynamoDB billing mode is **Provisioned**, never On-Demand. Capacity is split **5 RCU/5 WCU on the base table + 5 RCU/5 WCU on the GSI** (10/10 total) rather than the spec's illustrative "25/25" — DynamoDB's Always-Free allowance (25 RCU/25 WCU) is an account-level aggregate across a table *and* its indexes, so provisioning 25/25 on both the table and the GSI separately would total 50/50 and exceed it. 10/10 total is comfortably inside the free allowance and far more than this workload needs.
- No NAT Gateway anywhere. Lambda reaches S3/DynamoDB only via Gateway VPC Endpoints.
- `action` has no default anywhere in the system (API or sweeper) — always explicit `ARCHIVE` or `DELETE`.
- The video S3 bucket is pre-existing: referenced via a Terraform variable, never created/destroyed by this stack.
- Bucket versioning is assumed OFF (confirmed with Vijay); not handled if actually on.
- GitHub Actions authenticates to AWS via OIDC only — no long-lived access keys as secrets.
- `main` requires PRs (already configured). `apply` runs in a GitHub Environment named `production` requiring manual approval. `destroy` is `workflow_dispatch`-only with a typed `DESTROY` confirmation input.
- CloudWatch Logs retention is 14 days on both Lambda log groups.
- Before writing Task 2, verify current latest stable versions rather than trusting any version numbers in this plan: the `hashicorp/aws` provider's latest major version (check `https://registry.terraform.io/providers/hashicorp/aws/latest`), the latest Terraform CLI release, and the latest AWS-Lambda-supported Python runtime. Update the version constraints this plan uses accordingly before proceeding — this plan was written to a best estimate but dependency freshness rules take priority over any specific number written here.

## Review Focus

- **DynamoDB `Decimal` fields breaking JSON serialization in API responses** — every `GET`/`PUT` response must serialize numeric DynamoDB attributes correctly instead of raising on `Decimal`. (Task 8)
- **`expiration_at` supplied as an ISO 8601 string** (the spec explicitly allows this, not just epoch seconds) **being silently rejected** by validation that only accepts numbers. (Task 8)
- **Backfilling existing videos with an expiration already in the past** must be picked up on the very next sweeper run, not require a future date. (Tasks 5, 8)
- **The S3 object already missing when the sweeper tries to archive it** (deleted out-of-band) must resolve to a terminal success state, not retry forever into `FAILED`. (Task 5)
- **Overlapping/duplicate sweeper invocations processing the same item twice** — every status transition must be a conditional update so double-processing never corrupts state or triggers a redundant copy. (Task 5)

---

## File Structure

```
cryostream/
  bootstrap/                  # one-time, local-state Terraform: creates the TF state bucket
    main.tf
    variables.tf
    outputs.tf
  infra/                      # main Terraform stack (S3 backend)
    versions.tf
    providers.tf
    variables.tf
    outputs.tf
    vpc.tf
    dynamodb.tf
    iam.tf
    lambda_sweeper.tf
    lambda_api.tf
    api_gateway.tf
    eventbridge.tf
    observability.tf
    github_oidc.tf
  src/
    sweeper/handler.py        # sweeper Lambda, stdlib + boto3 only
    api/handler.py            # registration API Lambda, stdlib + boto3 only
  tests/unit/
    test_sweeper_handler.py
    test_api_handler.py
  .github/workflows/
    terraform-plan.yml
    terraform-apply.yml
    terraform-destroy.yml
  requirements-dev.txt
  pytest.ini
```

---

### Task 1: Terraform bootstrap stack (state bucket)

**Files:**
- Create: `bootstrap/main.tf`
- Create: `bootstrap/variables.tf`
- Create: `bootstrap/outputs.tf`

**Interfaces:**
- Consumes: nothing.
- Produces: an S3 bucket (name supplied at apply time via `var.state_bucket_name`) that Task 2's backend configuration points at. This bucket is created by `terraform apply` in Task 14, not during this task.

- [ ] **Step 1: Write the bootstrap Terraform config**

`bootstrap/main.tf`:
```hcl
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0" # verify latest major version before Task 14 (see Global Constraints)
    }
  }
}

provider "aws" {
  region = var.aws_region
}

resource "aws_s3_bucket" "terraform_state" {
  bucket = var.state_bucket_name
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket                  = aws_s3_bucket.terraform_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
```

`bootstrap/variables.tf`:
```hcl
variable "aws_region" {
  description = "AWS region for the Terraform state bucket"
  type        = string
  default     = "us-east-1"
}

variable "state_bucket_name" {
  description = "Globally-unique S3 bucket name to hold Terraform state for the main cryostream stack"
  type        = string
}
```

`bootstrap/outputs.tf`:
```hcl
output "state_bucket_name" {
  value = aws_s3_bucket.terraform_state.bucket
}
```

This stack intentionally has no `backend` block — it uses local state, which is fine since it's a one-time, single-operator bootstrap (`*.tfstate` is already gitignored).

- [ ] **Step 2: Validate syntax without AWS credentials**

Run:
```bash
cd bootstrap && terraform fmt -check -recursive && terraform init -backend=false && terraform validate
```
Expected: `Success! The configuration is valid.` (no AWS credentials needed for this — `validate` doesn't call AWS).

- [ ] **Step 3: Commit**

```bash
git add bootstrap/
git commit -m "Add Terraform bootstrap stack for state bucket"
```

---

### Task 2: Main Terraform stack skeleton

**Files:**
- Create: `infra/versions.tf`
- Create: `infra/providers.tf`
- Create: `infra/variables.tf`
- Modify: `.gitignore` (add Terraform build artifact dir)

**Interfaces:**
- Consumes: nothing yet.
- Produces: `var.aws_region`, `var.video_bucket_name`, `var.sweeper_schedule_rate`, `var.alert_email`, `var.github_repository`, `var.terraform_state_bucket` — every later `infra/*.tf` task references these by exact name.

- [ ] **Step 1: Verify current latest stable versions**

Before writing this task's files, check:
- `https://registry.terraform.io/providers/hashicorp/aws/latest` for the current `hashicorp/aws` major version.
- The latest Terraform CLI release (must be ≥1.10 for native S3 locking).
- The latest AWS-Lambda-supported Python runtime (for Tasks 6 and 9).

Use whatever you find in place of the `~> 5.0` / `python3.13` values written in this plan.

- [ ] **Step 2: Write the skeleton**

`infra/versions.tf`:
```hcl
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0" # confirmed in Step 1
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  backend "s3" {
    # bucket/key/region supplied via -backend-config at init time, since the
    # bucket name comes from the bootstrap stack's output (Task 1) and
    # can't be a literal here.
    use_lockfile = true
  }
}
```

`infra/providers.tf`:
```hcl
provider "aws" {
  region = var.aws_region
}
```

`infra/variables.tf`:
```hcl
variable "aws_region" {
  description = "AWS region to deploy cryostream into"
  type        = string
  default     = "us-east-1"
}

variable "video_bucket_name" {
  description = "Name of the pre-existing S3 bucket holding the video files"
  type        = string
}

variable "sweeper_schedule_rate" {
  description = "How often the sweeper Lambda runs, as an EventBridge Scheduler rate expression"
  type        = string
  default     = "rate(15 minutes)"
}

variable "alert_email" {
  description = "Email address to receive sweeper failure alarm notifications"
  type        = string
}

variable "github_repository" {
  description = "GitHub repo allowed to assume the deploy role via OIDC, in owner/repo form"
  type        = string
  default     = "vbvj4u/cryostream"
}

variable "terraform_state_bucket" {
  description = "Name of the S3 bucket holding Terraform state (created by the bootstrap stack)"
  type        = string
}
```

- [ ] **Step 3: Add Terraform build artifacts to .gitignore**

Add to `.gitignore`:
```
# Terraform build artifacts (Lambda zips)
infra/.build/
```

- [ ] **Step 4: Validate syntax without AWS credentials or a real backend**

Run:
```bash
cd infra && terraform fmt -check -recursive && terraform init -backend=false && terraform validate
```
Expected: `Success! The configuration is valid.`

- [ ] **Step 5: Commit**

```bash
git add infra/versions.tf infra/providers.tf infra/variables.tf .gitignore
git commit -m "Add main Terraform stack skeleton"
```

---

### Task 3: Networking — private VPC, Gateway Endpoints, no NAT

**Files:**
- Create: `infra/vpc.tf`

**Interfaces:**
- Consumes: `var.aws_region` (Task 2).
- Produces: `aws_subnet.private[*].id`, `aws_security_group.lambda.id` — both Lambda resources (Tasks 6, 9) reference these exact addresses for their `vpc_config` block.

- [ ] **Step 1: Write the networking config**

`infra/vpc.tf`:
```hcl
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
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
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
```

- [ ] **Step 2: Validate**

Run:
```bash
cd infra && terraform fmt -check -recursive && terraform validate
```
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add infra/vpc.tf
git commit -m "Add private VPC networking with Gateway Endpoints, no NAT"
```

---

### Task 4: DynamoDB table

**Files:**
- Create: `infra/dynamodb.tf`

**Interfaces:**
- Consumes: nothing.
- Produces: `aws_dynamodb_table.video_expirations.arn`, `aws_dynamodb_table.video_expirations.name` — Task 6 (sweeper IAM/env vars), Task 9 (API IAM/env vars), and Task 12 (outputs) all reference these exact addresses. GSI name is the literal string `"status-expiration_at-index"`, used verbatim in `src/sweeper/handler.py` (Task 5).

- [ ] **Step 1: Write the table config**

`infra/dynamodb.tf`:
```hcl
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
```

- [ ] **Step 2: Validate**

Run:
```bash
cd infra && terraform fmt -check -recursive && terraform validate
```
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add infra/dynamodb.tf
git commit -m "Add video_expirations DynamoDB table with status/expiration GSI"
```

---

### Task 5: Sweeper Lambda handler + unit tests

**Files:**
- Create: `src/sweeper/handler.py`
- Test: `tests/unit/test_sweeper_handler.py`
- Create: `requirements-dev.txt`
- Create: `pytest.ini`

**Interfaces:**
- Consumes: DynamoDB table with PK `s3_key`, GSI `status-expiration_at-index` (PK `status`, SK `expiration_at`), items shaped `{s3_key, action, expiration_at, status, attempts, last_error, created_at, updated_at}` (Task 4's schema). S3 bucket named by `BUCKET_NAME` env var.
- Produces: `lambda_handler(event, context)` — the exact entry point Task 6's `aws_lambda_function.sweeper` configures as `handler.lambda_handler`. Reads env vars `TABLE_NAME`, `BUCKET_NAME`, `GSI_NAME`.

- [ ] **Step 1: Write the failing tests**

`requirements-dev.txt`:
```
pytest
moto>=5
boto3
```

`pytest.ini`:
```ini
[pytest]
testpaths = tests
```

`tests/unit/test_sweeper_handler.py`:
```python
import importlib.util
import pathlib
import time

import boto3
import pytest
from moto import mock_aws

_HANDLER_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "sweeper" / "handler.py"
)
_spec = importlib.util.spec_from_file_location("sweeper_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

TABLE_NAME = "video_expirations"
BUCKET_NAME = "test-video-bucket"
GSI_NAME = "status-expiration_at-index"
REGION = "us-east-1"


class FakeContext:
    def __init__(self, remaining_ms=60_000):
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self._remaining_ms


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("BUCKET_NAME", BUCKET_NAME)
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "s3_key", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "s3_key", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "expiration_at", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": GSI_NAME,
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "expiration_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                    "ProvisionedThroughput": {
                        "ReadCapacityUnits": 5,
                        "WriteCapacityUnits": 5,
                    },
                }
            ],
            ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
        )
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET_NAME)
        yield {"table": dynamodb.Table(TABLE_NAME), "s3": s3}


def _put_item(table, s3_key, action, status="PENDING", expiration_at=None, attempts=0):
    table.put_item(
        Item={
            "s3_key": s3_key,
            "action": action,
            "status": status,
            "expiration_at": expiration_at if expiration_at is not None else int(time.time()) - 3600,
            "attempts": attempts,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
    )


def test_handle_delete_removes_object_and_marks_deleted(aws):
    table, s3 = aws["table"], aws["s3"]
    s3.put_object(Bucket=BUCKET_NAME, Key="videos/a.mp4", Body=b"data")
    _put_item(table, "videos/a.mp4", "DELETE")

    handler.handle_delete(s3, table, BUCKET_NAME, "videos/a.mp4")

    with pytest.raises(s3.exceptions.ClientError):
        s3.get_object(Bucket=BUCKET_NAME, Key="videos/a.mp4")
    item = table.get_item(Key={"s3_key": "videos/a.mp4"})["Item"]
    assert item["status"] == "DELETED"


def test_handle_delete_missing_object_is_idempotent(aws):
    table, s3 = aws["table"], aws["s3"]
    _put_item(table, "videos/missing.mp4", "DELETE")

    handler.handle_delete(s3, table, BUCKET_NAME, "videos/missing.mp4")

    item = table.get_item(Key={"s3_key": "videos/missing.mp4"})["Item"]
    assert item["status"] == "DELETED"


def test_handle_archive_copies_to_deep_archive_and_marks_archived(aws):
    table, s3 = aws["table"], aws["s3"]
    s3.put_object(Bucket=BUCKET_NAME, Key="videos/b.mp4", Body=b"data")
    _put_item(table, "videos/b.mp4", "ARCHIVE")

    handler.handle_archive(s3, table, BUCKET_NAME, "videos/b.mp4")

    head = s3.head_object(Bucket=BUCKET_NAME, Key="videos/b.mp4")
    assert head.get("StorageClass") == "DEEP_ARCHIVE"
    item = table.get_item(Key={"s3_key": "videos/b.mp4"})["Item"]
    assert item["status"] == "ARCHIVED"


def test_handle_archive_already_deep_archive_is_noop(aws):
    table, s3 = aws["table"], aws["s3"]
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key="videos/c.mp4",
        Body=b"data",
        StorageClass="DEEP_ARCHIVE",
    )
    _put_item(table, "videos/c.mp4", "ARCHIVE")

    handler.handle_archive(s3, table, BUCKET_NAME, "videos/c.mp4")

    item = table.get_item(Key={"s3_key": "videos/c.mp4"})["Item"]
    assert item["status"] == "ARCHIVED"


def test_handle_archive_missing_object_is_idempotent(aws):
    table, s3 = aws["table"], aws["s3"]
    _put_item(table, "videos/gone.mp4", "ARCHIVE")

    handler.handle_archive(s3, table, BUCKET_NAME, "videos/gone.mp4")

    item = table.get_item(Key={"s3_key": "videos/gone.mp4"})["Item"]
    assert item["status"] == "ARCHIVED"


def test_mark_status_is_idempotent_under_race(aws):
    table = aws["table"]
    _put_item(table, "videos/race.mp4", "ARCHIVE", status="ARCHIVED")

    # Simulates a second, overlapping invocation trying to mark the same
    # item ARCHIVED after a concurrent invocation already did so.
    handler.mark_status(table, "videos/race.mp4", "ARCHIVED")

    item = table.get_item(Key={"s3_key": "videos/race.mp4"})["Item"]
    assert item["status"] == "ARCHIVED"


def test_process_item_records_failure_and_increments_attempts(aws):
    table, s3 = aws["table"], aws["s3"]
    _put_item(table, "videos/bad.mp4", "ARCHIVE_TYPO")  # unknown action -> raises

    handler.process_item(s3, table, BUCKET_NAME, table.get_item(Key={"s3_key": "videos/bad.mp4"})["Item"])

    item = table.get_item(Key={"s3_key": "videos/bad.mp4"})["Item"]
    assert item["attempts"] == 1
    assert item["status"] == "PENDING"
    assert "unknown action" in item["last_error"]


def test_process_item_marks_failed_after_max_attempts(aws):
    table, s3 = aws["table"], aws["s3"]
    _put_item(table, "videos/bad2.mp4", "ARCHIVE_TYPO", attempts=4)

    handler.process_item(s3, table, BUCKET_NAME, table.get_item(Key={"s3_key": "videos/bad2.mp4"})["Item"])

    item = table.get_item(Key={"s3_key": "videos/bad2.mp4"})["Item"]
    assert item["attempts"] == 5
    assert item["status"] == "FAILED"


def test_iter_due_items_returns_only_pending_items_past_expiration(aws):
    table = aws["table"]
    now = int(time.time())
    _put_item(table, "videos/past.mp4", "DELETE", status="PENDING", expiration_at=now - 10)
    _put_item(table, "videos/future.mp4", "DELETE", status="PENDING", expiration_at=now + 10_000)
    _put_item(table, "videos/already_archived.mp4", "ARCHIVE", status="ARCHIVED", expiration_at=now - 10)

    due_keys = {item["s3_key"] for item in handler.iter_due_items(table, GSI_NAME, now)}

    assert due_keys == {"videos/past.mp4"}


def test_lambda_handler_processes_due_items_end_to_end(aws):
    table, s3 = aws["table"], aws["s3"]
    s3.put_object(Bucket=BUCKET_NAME, Key="videos/e2e.mp4", Body=b"data")
    _put_item(table, "videos/e2e.mp4", "DELETE")

    result = handler.lambda_handler({}, FakeContext())

    assert result == {"processed": 1}
    item = table.get_item(Key={"s3_key": "videos/e2e.mp4"})["Item"]
    assert item["status"] == "DELETED"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pip install -r requirements-dev.txt
pytest tests/unit/test_sweeper_handler.py -v
```
Expected: FAIL — `src/sweeper/handler.py` does not exist yet (`FileNotFoundError` from the `importlib` loader).

- [ ] **Step 3: Write the implementation**

`src/sweeper/handler.py`:
```python
import os
import time

import boto3
from botocore.exceptions import ClientError

MAX_ATTEMPTS = 5
TIME_BUDGET_MS = 10_000  # stop picking up new items once this close to timeout


def lambda_handler(event, context):
    table_name = os.environ["TABLE_NAME"]
    bucket_name = os.environ["BUCKET_NAME"]
    gsi_name = os.environ.get("GSI_NAME", "status-expiration_at-index")

    table = boto3.resource("dynamodb").Table(table_name)
    s3 = boto3.client("s3")

    now = int(time.time())
    processed = 0

    for item in iter_due_items(table, gsi_name, now):
        if context.get_remaining_time_in_millis() < TIME_BUDGET_MS:
            break
        process_item(s3, table, bucket_name, item)
        processed += 1

    return {"processed": processed}


def iter_due_items(table, gsi_name, now_epoch):
    kwargs = {
        "IndexName": gsi_name,
        "KeyConditionExpression": "#s = :pending AND expiration_at <= :now",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":pending": "PENDING", ":now": now_epoch},
    }
    while True:
        response = table.query(**kwargs)
        for item in response.get("Items", []):
            yield item
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return
        kwargs["ExclusiveStartKey"] = last_key


def process_item(s3, table, bucket_name, item):
    s3_key = item["s3_key"]
    action = item["action"]
    try:
        if action == "DELETE":
            handle_delete(s3, table, bucket_name, s3_key)
        elif action == "ARCHIVE":
            handle_archive(s3, table, bucket_name, s3_key)
        else:
            raise ValueError(f"unknown action {action!r} for {s3_key}")
    except Exception as exc:  # any failure here must be recorded, not raised
        record_failure(table, s3_key, str(exc))


def handle_delete(s3, table, bucket_name, s3_key):
    try:
        s3.delete_object(Bucket=bucket_name, Key=s3_key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey"):
            raise
    mark_status(table, s3_key, "DELETED")


def handle_archive(s3, table, bucket_name, s3_key):
    try:
        head = s3.head_object(Bucket=bucket_name, Key=s3_key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            mark_status(table, s3_key, "ARCHIVED")
            return
        raise

    if head.get("StorageClass") == "DEEP_ARCHIVE":
        mark_status(table, s3_key, "ARCHIVED")
        return

    # s3.copy (not the low-level copy_object) handles multipart copy
    # automatically for objects over 5GB, which video files routinely are.
    s3.copy(
        CopySource={"Bucket": bucket_name, "Key": s3_key},
        Bucket=bucket_name,
        Key=s3_key,
        ExtraArgs={"StorageClass": "DEEP_ARCHIVE", "MetadataDirective": "COPY"},
    )
    mark_status(table, s3_key, "ARCHIVED")


def record_failure(table, s3_key, error_message):
    try:
        response = table.update_item(
            Key={"s3_key": s3_key},
            UpdateExpression=(
                "SET attempts = if_not_exists(attempts, :zero) + :one, "
                "last_error = :err, updated_at = :now"
            ),
            ExpressionAttributeValues={
                ":zero": 0,
                ":one": 1,
                ":err": error_message,
                ":now": int(time.time()),
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError:
        return

    attempts = response["Attributes"].get("attempts", 0)
    if attempts < MAX_ATTEMPTS:
        return

    try:
        table.update_item(
            Key={"s3_key": s3_key},
            UpdateExpression="SET #s = :failed",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":failed": "FAILED", ":pending": "PENDING"},
            ConditionExpression="#s = :pending",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def mark_status(table, s3_key, new_status):
    try:
        table.update_item(
            Key={"s3_key": s3_key},
            UpdateExpression="SET #s = :new_status, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":new_status": new_status,
                ":now": int(time.time()),
                ":pending": "PENDING",
            },
            ConditionExpression="#s = :pending",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/unit/test_sweeper_handler.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sweeper/handler.py tests/unit/test_sweeper_handler.py requirements-dev.txt pytest.ini
git commit -m "Add sweeper Lambda handler with unit tests"
```

---

### Task 6: IAM roles + sweeper Lambda Terraform resource

**Files:**
- Create: `infra/iam.tf`
- Create: `infra/lambda_sweeper.tf`

**Interfaces:**
- Consumes: `aws_dynamodb_table.video_expirations` (Task 4), `aws_subnet.private[*].id` / `aws_security_group.lambda.id` (Task 3), `var.video_bucket_name` / `var.aws_region` (Task 2), `src/sweeper/handler.py` (Task 5).
- Produces: `aws_iam_role.sweeper`, `aws_iam_role.api` (the latter consumed by Task 9), `aws_lambda_function.sweeper.arn`/`.function_name` (consumed by Task 7).

- [ ] **Step 1: Write IAM roles for both Lambdas**

`infra/iam.tf`:
```hcl
data "aws_caller_identity" "current" {}

resource "aws_iam_role" "sweeper" {
  name = "cryostream-sweeper"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "sweeper" {
  name = "cryostream-sweeper"
  role = aws_iam_role.sweeper.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDBAccess"
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:UpdateItem"]
        Resource = [
          aws_dynamodb_table.video_expirations.arn,
          "${aws_dynamodb_table.video_expirations.arn}/index/status-expiration_at-index"
        ]
      },
      {
        Sid      = "S3Access"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "arn:aws:s3:::${var.video_bucket_name}/*"
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        # AWS requires Resource "*" for these three actions when a Lambda
        # manages VPC-attached ENIs - there is no resource-level
        # restriction available (matches AWSLambdaVPCAccessExecutionRole).
        Sid      = "VpcNetworking"
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role" "api" {
  name = "cryostream-api"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "api" {
  name = "cryostream-api"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDBAccess"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.video_expirations.arn
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        Sid      = "VpcNetworking"
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface"]
        Resource = "*"
      }
    ]
  })
}
```

- [ ] **Step 2: Write the sweeper Lambda resource**

`infra/lambda_sweeper.tf`:
```hcl
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
  runtime          = "python3.13" # confirmed in Task 2 Step 1
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
```

- [ ] **Step 3: Validate**

Run:
```bash
cd infra && terraform fmt -check -recursive && terraform validate
```
Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add infra/iam.tf infra/lambda_sweeper.tf
git commit -m "Add Lambda IAM roles and sweeper Lambda resource"
```

---

### Task 7: EventBridge Scheduler

**Files:**
- Create: `infra/eventbridge.tf`

**Interfaces:**
- Consumes: `aws_lambda_function.sweeper.arn`/`.function_name` (Task 6), `var.sweeper_schedule_rate` (Task 2).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the schedule**

`infra/eventbridge.tf`:
```hcl
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
  }
}
```

- [ ] **Step 2: Validate**

Run:
```bash
cd infra && terraform fmt -check -recursive && terraform validate
```
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add infra/eventbridge.tf
git commit -m "Add EventBridge Scheduler to trigger sweeper Lambda every 15 minutes"
```

---

### Task 8: Registration API Lambda handler + unit tests

**Files:**
- Create: `src/api/handler.py`
- Test: `tests/unit/test_api_handler.py`

**Interfaces:**
- Consumes: DynamoDB table with PK `s3_key` (Task 4's schema).
- Produces: `lambda_handler(event, context)` — the entry point Task 9's `aws_lambda_function.api` configures as `handler.lambda_handler`. Expects an API Gateway REST proxy-integration `event` with `event["httpMethod"]`, `event["pathParameters"]["key"]`, `event["body"]`. Reads env var `TABLE_NAME`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_api_handler.py`:
```python
import importlib.util
import json
import pathlib
import time

import boto3
import pytest
from moto import mock_aws

_HANDLER_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "api" / "handler.py"
)
_spec = importlib.util.spec_from_file_location("api_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

TABLE_NAME = "video_expirations"
REGION = "us-east-1"


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("TABLE_NAME", TABLE_NAME)
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "s3_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "s3_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield dynamodb.Table(TABLE_NAME)


def _event(method, key, body=None):
    return {
        "httpMethod": method,
        "pathParameters": {"key": key},
        "body": json.dumps(body) if body is not None else None,
    }


def test_put_creates_new_pending_item(table):
    response = handler.lambda_handler(
        _event("PUT", "videos/a.mp4", {"action": "ARCHIVE", "expiration_at": 9999999999}),
        None,
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "PENDING"
    assert body["action"] == "ARCHIVE"
    assert body["expiration_at"] == 9999999999


def test_put_accepts_iso8601_expiration_at(table):
    response = handler.lambda_handler(
        _event("PUT", "videos/iso.mp4", {"action": "DELETE", "expiration_at": "2030-01-01T00:00:00Z"}),
        None,
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["expiration_at"] == 1893456000


def test_put_rejects_unparseable_expiration_at(table):
    response = handler.lambda_handler(
        _event("PUT", "videos/bad.mp4", {"action": "DELETE", "expiration_at": "not-a-date"}),
        None,
    )
    assert response["statusCode"] == 400


def test_put_rejects_invalid_action(table):
    response = handler.lambda_handler(
        _event("PUT", "videos/b.mp4", {"action": "WIPE", "expiration_at": 9999999999}),
        None,
    )
    assert response["statusCode"] == 400


def test_put_rejects_missing_expiration_at(table):
    response = handler.lambda_handler(
        _event("PUT", "videos/c.mp4", {"action": "ARCHIVE"}),
        None,
    )
    assert response["statusCode"] == 400


def test_put_accepts_past_expiration_at_for_backfill(table):
    past = int(time.time()) - 86400
    response = handler.lambda_handler(
        _event("PUT", "videos/old.mp4", {"action": "ARCHIVE", "expiration_at": past}),
        None,
    )
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["status"] == "PENDING"


def test_put_resets_archived_item_to_pending(table):
    table.put_item(
        Item={
            "s3_key": "videos/d.mp4",
            "action": "ARCHIVE",
            "status": "ARCHIVED",
            "expiration_at": 1000,
            "created_at": 1000,
            "updated_at": 1000,
            "attempts": 0,
        }
    )
    response = handler.lambda_handler(
        _event("PUT", "videos/d.mp4", {"action": "DELETE", "expiration_at": 9999999999}),
        None,
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "PENDING"
    assert body["action"] == "DELETE"
    assert body["created_at"] == 1000  # preserved from the original record


def test_put_rejects_update_to_deleted_item(table):
    table.put_item(
        Item={
            "s3_key": "videos/e.mp4",
            "action": "DELETE",
            "status": "DELETED",
            "expiration_at": 1000,
            "created_at": 1000,
            "updated_at": 1000,
            "attempts": 0,
        }
    )
    response = handler.lambda_handler(
        _event("PUT", "videos/e.mp4", {"action": "ARCHIVE", "expiration_at": 9999999999}),
        None,
    )
    assert response["statusCode"] == 409


def test_get_returns_item_and_serializes_decimal_fields(table):
    table.put_item(
        Item={
            "s3_key": "videos/f.mp4",
            "action": "ARCHIVE",
            "status": "PENDING",
            "expiration_at": 9999999999,
            "created_at": 1000,
            "updated_at": 1000,
            "attempts": 0,
        }
    )
    response = handler.lambda_handler(_event("GET", "videos/f.mp4"), None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])  # raises if Decimal leaked through
    assert body["expiration_at"] == 9999999999


def test_get_returns_404_when_missing(table):
    response = handler.lambda_handler(_event("GET", "videos/missing.mp4"), None)
    assert response["statusCode"] == 404


def test_delete_removes_tracking_row_only(table):
    table.put_item(
        Item={
            "s3_key": "videos/g.mp4",
            "action": "DELETE",
            "status": "PENDING",
            "expiration_at": 1000,
            "created_at": 1000,
            "updated_at": 1000,
            "attempts": 0,
        }
    )
    response = handler.lambda_handler(_event("DELETE", "videos/g.mp4"), None)
    assert response["statusCode"] == 204
    assert "Item" not in table.get_item(Key={"s3_key": "videos/g.mp4"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/unit/test_api_handler.py -v
```
Expected: FAIL — `src/api/handler.py` does not exist yet.

- [ ] **Step 3: Write the implementation**

`src/api/handler.py`:
```python
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3

VALID_ACTIONS = {"ARCHIVE", "DELETE"}


def lambda_handler(event, context):
    method = event["httpMethod"]
    key = event["pathParameters"]["key"]
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

    if method == "PUT":
        return handle_put(table, key, event.get("body") or "{}")
    if method == "GET":
        return handle_get(table, key)
    if method == "DELETE":
        return handle_delete(table, key)
    return _response(405, {"message": f"unsupported method {method}"})


def handle_put(table, key, raw_body):
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return _response(400, {"message": "body must be valid JSON"})

    action = body.get("action")
    if action not in VALID_ACTIONS:
        return _response(400, {"message": "action must be ARCHIVE or DELETE"})

    expiration_at = _parse_expiration(body.get("expiration_at"))
    if expiration_at is None:
        return _response(
            400, {"message": "expiration_at must be epoch seconds or an ISO 8601 timestamp"}
        )

    existing = table.get_item(Key={"s3_key": key}).get("Item")
    if existing and existing.get("status") == "DELETED":
        return _response(409, {"message": "object already deleted, cannot reschedule"})

    now = int(time.time())
    item = {
        "s3_key": key,
        "action": action,
        "expiration_at": expiration_at,
        "status": "PENDING",
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        "attempts": 0,
    }
    table.put_item(Item=item)
    return _response(200, item)


def handle_get(table, key):
    item = table.get_item(Key={"s3_key": key}).get("Item")
    if not item:
        return _response(404, {"message": "not found"})
    return _response(200, item)


def handle_delete(table, key):
    table.delete_item(Key={"s3_key": key})
    return _response(204, None)


def _parse_expiration(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return None


def _to_jsonable(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(_to_jsonable(body)) if body is not None else "",
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/unit/test_api_handler.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/handler.py tests/unit/test_api_handler.py
git commit -m "Add registration API Lambda handler with unit tests"
```

---

### Task 9: API Lambda Terraform resource + API Gateway + API key

**Files:**
- Create: `infra/lambda_api.tf`
- Create: `infra/api_gateway.tf`

**Interfaces:**
- Consumes: `aws_iam_role.api` (Task 6), `aws_subnet.private[*].id`/`aws_security_group.lambda.id` (Task 3), `aws_dynamodb_table.video_expirations.name` (Task 4), `src/api/handler.py` (Task 8).
- Produces: `aws_api_gateway_stage.prod.invoke_url`, `aws_api_gateway_api_key.this.id` — both referenced by Task 12's outputs.

- [ ] **Step 1: Write the API Lambda resource**

`infra/lambda_api.tf`:
```hcl
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
  runtime          = "python3.13" # confirmed in Task 2 Step 1
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
```

- [ ] **Step 2: Write API Gateway, API key, and usage plan**

`infra/api_gateway.tf`:
```hcl
resource "aws_api_gateway_rest_api" "this" {
  name = "cryostream"
}

resource "aws_api_gateway_resource" "videos" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = "videos"
}

resource "aws_api_gateway_resource" "video_key" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_resource.videos.id
  path_part   = "{key+}"
}

resource "aws_api_gateway_method" "video_key_any" {
  rest_api_id      = aws_api_gateway_rest_api.this.id
  resource_id      = aws_api_gateway_resource.video_key.id
  http_method      = "ANY"
  authorization    = "NONE"
  api_key_required = true

  request_parameters = {
    "method.request.path.key" = true
  }
}

resource "aws_api_gateway_integration" "video_key_any" {
  rest_api_id             = aws_api_gateway_rest_api.this.id
  resource_id             = aws_api_gateway_resource.video_key.id
  http_method             = aws_api_gateway_method.video_key_any.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.this.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.videos.id,
      aws_api_gateway_resource.video_key.id,
      aws_api_gateway_method.video_key_any.id,
      aws_api_gateway_integration.video_key_any.id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.this.id
  deployment_id = aws_api_gateway_deployment.this.id
  stage_name    = "prod"
}

resource "aws_api_gateway_api_key" "this" {
  name    = "cryostream"
  enabled = true
}

resource "aws_api_gateway_usage_plan" "this" {
  name = "cryostream"

  api_stages {
    api_id = aws_api_gateway_rest_api.this.id
    stage  = aws_api_gateway_stage.prod.stage_name
  }
}

resource "aws_api_gateway_usage_plan_key" "this" {
  key_id        = aws_api_gateway_api_key.this.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.this.id
}
```

- [ ] **Step 3: Validate**

Run:
```bash
cd infra && terraform fmt -check -recursive && terraform validate
```
Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add infra/lambda_api.tf infra/api_gateway.tf
git commit -m "Add API Lambda resource, API Gateway, and API key/usage plan"
```

---

### Task 10: GitHub OIDC + deploy role

**Files:**
- Create: `infra/github_oidc.tf`

**Interfaces:**
- Consumes: `var.github_repository` / `var.terraform_state_bucket` (Task 2).
- Produces: `aws_iam_role.github_actions.arn` — the value set as the `AWS_GITHUB_ACTIONS_ROLE_ARN` repository variable in Task 14, and referenced by all three Task 13 workflows.

- [ ] **Step 1: Write the OIDC provider and deploy role**

`infra/github_oidc.tf`:
```hcl
data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]
}

resource "aws_iam_role" "github_actions" {
  name = "cryostream-github-actions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions" {
  name = "cryostream-github-actions"
  role = aws_iam_role.github_actions.id

  # Broad by necessity: Terraform must manage its own IAM roles/policies as
  # part of this stack, which is an inherent trade-off of Terraform-driven
  # IAM management (see spec). Scoped to resource types this stack owns,
  # never AdministratorAccess.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TerraformStateAccess"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${var.terraform_state_bucket}",
          "arn:aws:s3:::${var.terraform_state_bucket}/*"
        ]
      },
      {
        Sid    = "ManageCryostreamResources"
        Effect = "Allow"
        Action = [
          "dynamodb:*", "lambda:*", "apigateway:*", "scheduler:*",
          "logs:*", "sns:*", "cloudwatch:*",
          "ec2:DescribeVpcs", "ec2:CreateVpc", "ec2:DeleteVpc", "ec2:ModifyVpcAttribute",
          "ec2:DescribeSubnets", "ec2:CreateSubnet", "ec2:DeleteSubnet",
          "ec2:DescribeRouteTables", "ec2:CreateRouteTable", "ec2:DeleteRouteTable",
          "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable",
          "ec2:DescribeSecurityGroups", "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
          "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress",
          "ec2:DescribeVpcEndpoints", "ec2:CreateVpcEndpoint", "ec2:DeleteVpcEndpoints",
          "ec2:ModifyVpcEndpoint", "ec2:DescribeAvailabilityZones",
          "ec2:CreateTags", "ec2:DeleteTags",
          "iam:GetRole", "iam:CreateRole", "iam:DeleteRole", "iam:PutRolePolicy",
          "iam:DeleteRolePolicy", "iam:GetRolePolicy", "iam:PassRole",
          "iam:TagRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
          "iam:GetOpenIDConnectProvider"
        ]
        Resource = "*"
      }
    ]
  })
}
```

- [ ] **Step 2: Validate**

Run:
```bash
cd infra && terraform fmt -check -recursive && terraform validate
```
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add infra/github_oidc.tf
git commit -m "Add GitHub OIDC provider and scoped deploy role"
```

---

### Task 11: Observability — CloudWatch alarm + SNS

**Files:**
- Create: `infra/observability.tf`

**Interfaces:**
- Consumes: `aws_lambda_function.sweeper.function_name` (Task 6), `var.alert_email` (Task 2).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the alarm and SNS topic**

`infra/observability.tf`:
```hcl
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
```

- [ ] **Step 2: Validate**

Run:
```bash
cd infra && terraform fmt -check -recursive && terraform validate
```
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add infra/observability.tf
git commit -m "Add CloudWatch alarm and SNS email alert for sweeper errors"
```

---

### Task 12: Stack outputs + full-stack validation pass

**Files:**
- Create: `infra/outputs.tf`

**Interfaces:**
- Consumes: `aws_api_gateway_stage.prod` (Task 9), `aws_api_gateway_api_key.this` (Task 9), `aws_iam_role.github_actions` (Task 10), `aws_dynamodb_table.video_expirations` (Task 4).
- Produces: `terraform output` values consumed manually in Task 14 (to configure GitHub repo variables).

- [ ] **Step 1: Write outputs**

`infra/outputs.tf`:
```hcl
output "api_invoke_url" {
  value = "${aws_api_gateway_stage.prod.invoke_url}/videos"
}

output "api_key_id" {
  value = aws_api_gateway_api_key.this.id
}

output "github_actions_role_arn" {
  value = aws_iam_role.github_actions.arn
}

output "dynamodb_table_name" {
  value = aws_dynamodb_table.video_expirations.name
}
```

- [ ] **Step 2: Validate the whole stack together**

Run:
```bash
cd infra && terraform fmt -check -recursive && terraform init -backend=false && terraform validate
```
Expected: `Success! The configuration is valid.` This is the first validation covering every `infra/*.tf` file written across Tasks 2–12 at once — fix any cross-file reference errors it surfaces.

- [ ] **Step 3: Commit**

```bash
git add infra/outputs.tf
git commit -m "Add stack outputs for API URL, API key, and deploy role ARN"
```

---

### Task 13: GitHub Actions workflows

**Files:**
- Create: `.github/workflows/terraform-plan.yml`
- Create: `.github/workflows/terraform-apply.yml`
- Create: `.github/workflows/terraform-destroy.yml`

**Interfaces:**
- Consumes: repository variables `AWS_GITHUB_ACTIONS_ROLE_ARN`, `AWS_REGION`, `TF_STATE_BUCKET`, `VIDEO_BUCKET_NAME`, `ALERT_EMAIL` — all set in Task 14, not by these files.
- Produces: nothing consumed by later tasks (Task 14 is the last task).

- [ ] **Step 1: Write the plan workflow**

`.github/workflows/terraform-plan.yml`:
```yaml
name: Terraform Plan

on:
  pull_request:
    paths:
      - "infra/**"
      - ".github/workflows/terraform-plan.yml"

permissions:
  id-token: write
  contents: read
  pull-requests: write

jobs:
  plan:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: infra
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_GITHUB_ACTIONS_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "1.10.0" # confirmed in Task 2 Step 1

      - name: Terraform Init
        run: |
          terraform init \
            -backend-config="bucket=${{ vars.TF_STATE_BUCKET }}" \
            -backend-config="key=cryostream/terraform.tfstate" \
            -backend-config="region=${{ vars.AWS_REGION }}"

      - name: Terraform Plan
        run: terraform plan -no-color -input=false
        env:
          TF_VAR_video_bucket_name: ${{ vars.VIDEO_BUCKET_NAME }}
          TF_VAR_alert_email: ${{ vars.ALERT_EMAIL }}
          TF_VAR_terraform_state_bucket: ${{ vars.TF_STATE_BUCKET }}
          TF_VAR_aws_region: ${{ vars.AWS_REGION }}
```

- [ ] **Step 2: Write the apply workflow**

`.github/workflows/terraform-apply.yml`:
```yaml
name: Terraform Apply

on:
  push:
    branches: [main]
    paths:
      - "infra/**"
      - ".github/workflows/terraform-apply.yml"

permissions:
  id-token: write
  contents: read

jobs:
  apply:
    runs-on: ubuntu-latest
    environment: production
    defaults:
      run:
        working-directory: infra
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_GITHUB_ACTIONS_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "1.10.0"

      - name: Terraform Init
        run: |
          terraform init \
            -backend-config="bucket=${{ vars.TF_STATE_BUCKET }}" \
            -backend-config="key=cryostream/terraform.tfstate" \
            -backend-config="region=${{ vars.AWS_REGION }}"

      - name: Terraform Apply
        run: terraform apply -auto-approve -input=false
        env:
          TF_VAR_video_bucket_name: ${{ vars.VIDEO_BUCKET_NAME }}
          TF_VAR_alert_email: ${{ vars.ALERT_EMAIL }}
          TF_VAR_terraform_state_bucket: ${{ vars.TF_STATE_BUCKET }}
          TF_VAR_aws_region: ${{ vars.AWS_REGION }}
```

- [ ] **Step 3: Write the destroy workflow**

`.github/workflows/terraform-destroy.yml`:
```yaml
name: Terraform Destroy

on:
  workflow_dispatch:
    inputs:
      confirm:
        description: "Type DESTROY to confirm you want to tear down all cryostream infrastructure, including the DynamoDB table and all expiration-tracking data"
        required: true

permissions:
  id-token: write
  contents: read

jobs:
  destroy:
    runs-on: ubuntu-latest
    environment: production
    defaults:
      run:
        working-directory: infra
    steps:
      - name: Verify confirmation input
        run: |
          if [ "${{ github.event.inputs.confirm }}" != "DESTROY" ]; then
            echo "Confirmation input did not match DESTROY, aborting."
            exit 1
          fi

      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_GITHUB_ACTIONS_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "1.10.0"

      - name: Terraform Init
        run: |
          terraform init \
            -backend-config="bucket=${{ vars.TF_STATE_BUCKET }}" \
            -backend-config="key=cryostream/terraform.tfstate" \
            -backend-config="region=${{ vars.AWS_REGION }}"

      - name: Terraform Destroy
        run: terraform destroy -auto-approve -input=false
        env:
          TF_VAR_video_bucket_name: ${{ vars.VIDEO_BUCKET_NAME }}
          TF_VAR_alert_email: ${{ vars.ALERT_EMAIL }}
          TF_VAR_terraform_state_bucket: ${{ vars.TF_STATE_BUCKET }}
          TF_VAR_aws_region: ${{ vars.AWS_REGION }}
```

- [ ] **Step 4: Validate YAML syntax**

Run:
```bash
python3 -c "import yaml, glob; [yaml.safe_load(open(f)) for f in glob.glob('.github/workflows/*.yml')]"
```
Expected: no output, no exception (each file parses as valid YAML). `actionlint`, if installed, is a stronger check — use it if available.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/
git commit -m "Add Terraform plan/apply/destroy GitHub Actions workflows"
```

---

### Task 14: Bootstrap and first deploy (manual, one-time — requires Vijay's own AWS credentials)

**This task is not for an agent to run unattended.** It creates and modifies real resources in Vijay's AWS account and GitHub repo. Everything up to here (Tasks 1–13) is code that only needed `terraform validate`/`fmt` and `pytest` — no AWS credentials, no real infrastructure. This task is the one place that changes that, and it should be run by Vijay directly, or by an agent only after Vijay explicitly confirms each AWS-touching command at the time it runs.

**Files:** none — this task runs commands, it doesn't add code.

- [ ] **Step 1: Apply the bootstrap stack**

```bash
cd bootstrap
terraform init
terraform apply -var="state_bucket_name=<choose a globally-unique name>" -var="aws_region=<your region>"
```
Note the `state_bucket_name` output.

- [ ] **Step 2: Apply the main stack locally, once**

This is the one time the main stack is applied outside GitHub Actions — it has to create the GitHub OIDC role before GitHub Actions can use it.

```bash
cd infra
terraform init \
  -backend-config="bucket=<state_bucket_name from Step 1>" \
  -backend-config="key=cryostream/terraform.tfstate" \
  -backend-config="region=<your region>"
terraform apply \
  -var="video_bucket_name=<your existing video bucket>" \
  -var="alert_email=<your email>" \
  -var="terraform_state_bucket=<state_bucket_name from Step 1>" \
  -var="aws_region=<your region>"
```
Confirm the SNS email subscription (check your inbox) and note the `github_actions_role_arn`, `api_invoke_url`, and `api_key_id` outputs.

- [ ] **Step 3: Set GitHub repository variables**

```bash
gh variable set AWS_GITHUB_ACTIONS_ROLE_ARN --body "<github_actions_role_arn output>"
gh variable set AWS_REGION --body "<your region>"
gh variable set TF_STATE_BUCKET --body "<state_bucket_name from Step 1>"
gh variable set VIDEO_BUCKET_NAME --body "<your existing video bucket>"
gh variable set ALERT_EMAIL --body "<your email>"
```

- [ ] **Step 4: Create the `production` GitHub Environment with required reviewers**

```bash
gh api -X PUT repos/vbvj4u/cryostream/environments/production --input - <<'EOF'
{
  "reviewers": [{"type": "User", "id": <your GitHub user ID>}],
  "deployment_branch_policy": { "protected_branches": true, "custom_branch_policies": false }
}
EOF
```
Get your user ID with `gh api user --jq .id`. This is what makes the `apply` and `destroy` workflows pause for manual approval (Global Constraints).

- [ ] **Step 5: Verify the registration API**

```bash
curl -X PUT "<api_invoke_url output>/<a real object key in your bucket>" \
  -H "x-api-key: <retrieve with: aws apigateway get-api-key --api-key <api_key_id output> --include-value --query value --output text>" \
  -H "Content-Type: application/json" \
  -d '{"action": "DELETE", "expiration_at": '"$(date -d '+5 minutes' +%s)"'}'
```
Expected: `200` with the created item. Wait for the next sweeper run (up to 15 minutes) and confirm the object is gone and the DynamoDB item's `status` is `DELETED`.

From here on, all further infrastructure changes go through a PR → `plan` workflow → merge to `main` → manually-approved `apply` workflow, per the CI/CD design in Global Constraints.
