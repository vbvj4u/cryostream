import importlib.util
import logging
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
    def __init__(self, remaining_ms=300_000):
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


class _HeadObject403S3(object):
    """Wraps a real boto3 S3 client so head_object raises 403 Forbidden for
    one key (what S3 actually returns for a missing key when the caller
    lacks s3:ListBucket) while every other call passes through untouched.
    """

    def __init__(self, real_client, key_that_403s):
        self._real = real_client
        self._key_that_403s = key_that_403s

    def head_object(self, Bucket, Key):
        if Key == self._key_that_403s:
            raise self._real.exceptions.ClientError(
                {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
            )
        return self._real.head_object(Bucket=Bucket, Key=Key)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_handle_archive_403_is_not_confused_with_missing_object(aws):
    # A 403 from head_object (e.g. a caller missing s3:ListBucket, which
    # makes S3 return 403 instead of 404 for a nonexistent key) must never
    # be silently treated the same as "object is gone" - that would paper
    # over a real permissions gap by marking unarchived videos ARCHIVED.
    table, s3 = aws["table"], aws["s3"]
    _put_item(table, "videos/forbidden.mp4", "ARCHIVE")
    wrapped_s3 = _HeadObject403S3(s3, "videos/forbidden.mp4")
    item_before = table.get_item(Key={"s3_key": "videos/forbidden.mp4"})["Item"]

    # Routed through process_item, not handle_archive directly: handle_archive
    # deliberately re-raises anything that isn't 404/NoSuchKey, and
    # process_item is what turns that into a recorded failure in production.
    handler.process_item(wrapped_s3, table, BUCKET_NAME, item_before)

    item = table.get_item(Key={"s3_key": "videos/forbidden.mp4"})["Item"]
    assert item["status"] == "PENDING"
    assert item["attempts"] == 1
    assert "403" in item["last_error"]


def test_mark_status_is_idempotent_under_race(aws):
    table = aws["table"]
    _put_item(table, "videos/race.mp4", "ARCHIVE", status="ARCHIVED")

    # Simulates a second, overlapping invocation trying to mark the same
    # item ARCHIVED after a concurrent invocation already did so.
    handler.mark_status(table, "videos/race.mp4", "ARCHIVED")

    item = table.get_item(Key={"s3_key": "videos/race.mp4"})["Item"]
    assert item["status"] == "ARCHIVED"


def test_process_item_handles_missing_action_key_without_crashing(aws):
    table, s3 = aws["table"], aws["s3"]
    now = int(time.time())
    # A malformed row (e.g. from a bad bulk-backfill write) with no
    # "action" key at all - must not raise out of process_item, or every
    # later item in the same (and every future) sweep gets wedged behind it.
    table.put_item(
        Item={
            "s3_key": "videos/malformed.mp4",
            "status": "PENDING",
            "expiration_at": now - 10,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    item = table.get_item(Key={"s3_key": "videos/malformed.mp4"})["Item"]

    handler.process_item(s3, table, BUCKET_NAME, item)

    updated = table.get_item(Key={"s3_key": "videos/malformed.mp4"})["Item"]
    assert updated["attempts"] == 1
    assert updated["status"] == "PENDING"
    assert updated["last_error"]


def test_lambda_handler_continues_past_a_malformed_item_to_later_ones(aws):
    table, s3 = aws["table"], aws["s3"]
    now = int(time.time())
    s3.put_object(Bucket=BUCKET_NAME, Key="videos/after-malformed.mp4", Body=b"data")
    # GSI sort key is expiration_at ascending, so this malformed row sorts
    # before the valid one below in the same query.
    table.put_item(
        Item={
            "s3_key": "videos/malformed2.mp4",
            "status": "PENDING",
            "expiration_at": now - 20,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    _put_item(table, "videos/after-malformed.mp4", "DELETE", expiration_at=now - 10)

    result = handler.lambda_handler({}, FakeContext())

    assert result == {"processed": 2}
    later_item = table.get_item(Key={"s3_key": "videos/after-malformed.mp4"})["Item"]
    assert later_item["status"] == "DELETED"


def test_record_failure_logs_terminal_failure_with_greppable_marker(aws, caplog):
    # The sweeper's only CloudWatch alarm watches AWS/Lambda Errors, which
    # never fires (every exception is caught). A log metric filter is what
    # actually detects a permanently-failed item, so the terminal
    # transition must be logged with a marker a metric filter can match.
    table = aws["table"]
    _put_item(table, "videos/logme.mp4", "ARCHIVE", attempts=4)

    with caplog.at_level(logging.ERROR):
        handler.record_failure(table, "videos/logme.mp4", "boom")

    assert any("SWEEPER_ITEM_FAILED_TERMINAL" in r.message for r in caplog.records)
    item = table.get_item(Key={"s3_key": "videos/logme.mp4"})["Item"]
    assert item["status"] == "FAILED"


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
