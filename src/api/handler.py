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
