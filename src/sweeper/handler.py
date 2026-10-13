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
