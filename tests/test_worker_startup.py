"""Regression tests for safe persistent-worker startup behavior."""
import asyncio
import base64
import os
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _unauthorized_string_session() -> str:
    """Create a syntactically valid, deliberately unauthorized session."""
    payload = struct.pack(
        ">B4sH256s", 2, bytes((149, 154, 167, 50)), 443, bytes(256)
    )
    return "1" + base64.urlsafe_b64encode(payload).decode("ascii")


os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-api-hash")
os.environ.setdefault("TELEGRAM_SESSION_STRING", _unauthorized_string_session())
os.environ.setdefault("WORKER_SECRET", "test-worker-secret")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

import worker  # noqa: E402


class UnauthorizedClient:
    def __init__(self):
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return False

    async def disconnect(self):
        self.disconnected = True


class WorkerStartupTests(unittest.TestCase):
    def test_dialog_uses_telethon_entity_attribute(self):
        entity = SimpleNamespace(title="Broadcast group", username=None, megagroup=True, broadcast=False)
        dialog = SimpleNamespace(entity=entity, is_group=True, is_channel=True)

        with patch.object(worker.utils, "get_peer_id", return_value=-100123):
            chat = worker.serialize_broadcast_dialog(dialog)

        self.assertEqual(
            chat,
            {"id": "-100123", "title": "Broadcast group", "type": "group", "username": None},
        )

    def test_private_dialog_is_not_a_broadcast_destination(self):
        dialog = SimpleNamespace(entity=SimpleNamespace(), is_group=False, is_channel=False)
        self.assertIsNone(worker.serialize_broadcast_dialog(dialog))

    def test_dialog_without_entity_is_skipped_without_reading_chat(self):
        dialog = SimpleNamespace(is_group=True, is_channel=True)
        self.assertIsNone(worker.serialize_broadcast_dialog(dialog))

    def test_redis_is_optional_for_a_single_worker(self):
        with patch.object(worker, "redis", None):
            self.assertTrue(worker.acquire_task_lock("task-id"))
            worker.release_task_lock("task-id")

    def test_invalid_session_fails_without_interactive_login(self):
        client = UnauthorizedClient()
        with patch.object(worker, "client", client):
            with self.assertRaisesRegex(RuntimeError, "TELEGRAM_SESSION_STRING"):
                asyncio.run(worker.on_startup({}))

        self.assertTrue(client.connected)
        self.assertTrue(client.disconnected)


if __name__ == "__main__":
    unittest.main()
