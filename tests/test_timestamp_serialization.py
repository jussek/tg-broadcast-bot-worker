"""Regression tests for PostgreSQL timestamp values sent to Supabase."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from storage import supabase_storage as storage


class Response:
    data = []


class RecordingQuery:
    def __init__(self, records):
        self.records = records

    def __getattr__(self, name):
        def method(*args, **kwargs):
            if name in {"insert", "update", "upsert"}:
                self.records.append(args[0])
            if name == "lte":
                self.records.append({"next_run_filter": args[1]})
            return self

        return method

    def execute(self):
        return Response()


class RecordingDatabase:
    def __init__(self):
        self.records = []

    def table(self, _name):
        return RecordingQuery(self.records)


class TimestampSerializationTests(unittest.TestCase):
    def assert_timestamp(self, value):
        parsed = datetime.fromisoformat(value)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_storage_uses_iso_utc_timestamps_for_writes_and_due_query(self):
        database = RecordingDatabase()
        with patch.object(storage, "get_supabase_client", return_value=database):
            storage.get_or_create_user(1)
            storage.create_template(1, "Name", "Message")
            storage.create_group_set(1, "Groups", [123])
            task = storage.create_broadcast_task(1, "Message", [123], 10, 1)
            storage.update_broadcast_task(task["id"], status="completed")
            storage.log_broadcast(None, 1, "Message", [123], 1, 0)
            storage.sync_user_chats(1, [{"id": "123", "title": "Chat", "type": "group"}])
            storage.get_due_broadcast_tasks()

        for record in database.records:
            for field in ("created_at", "updated_at", "last_seen", "next_run", "next_run_filter"):
                if field in record:
                    self.assertIsInstance(record[field], str)
                    self.assert_timestamp(record[field])


if __name__ == "__main__":
    unittest.main()
