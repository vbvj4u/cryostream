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


class _ConcurrentDeleteTable(object):
    """Wraps a real DynamoDB Table resource. Simulates the sweeper deleting
    the tracked item (marking it DELETED) in the gap between handle_put's
    read and its write - proving the write is conditioned on current state
    at write time, not the state read earlier, not just that a pre-read
    check happens to catch an already-DELETED item.
    """

    def __init__(self, real_table, key_to_race):
        self._real = real_table
        self._key_to_race = key_to_race

    def put_item(self, **kwargs):
        if kwargs.get("Item", {}).get("s3_key") == self._key_to_race:
            self._real.update_item(
                Key={"s3_key": self._key_to_race},
                UpdateExpression="SET #s = :deleted",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":deleted": "DELETED"},
            )
        return self._real.put_item(**kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_put_rejects_when_item_deleted_concurrently_between_read_and_write(table):
    table.put_item(
        Item={
            "s3_key": "videos/race.mp4",
            "action": "ARCHIVE",
            "status": "PENDING",
            "expiration_at": 1000,
            "created_at": 1000,
            "updated_at": 1000,
            "attempts": 0,
        }
    )
    racy_table = _ConcurrentDeleteTable(table, "videos/race.mp4")

    response = handler.handle_put(
        racy_table,
        "videos/race.mp4",
        json.dumps({"action": "DELETE", "expiration_at": 9999999999}),
    )

    assert response["statusCode"] == 409
    item = table.get_item(Key={"s3_key": "videos/race.mp4"})["Item"]
    assert item["status"] == "DELETED"
    assert item["action"] == "ARCHIVE"  # untouched by the rejected write


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
