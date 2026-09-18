"""Tests for bot input parsing and Worker failure handling."""
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from bot import parse_chat_ids
from worker_client import get_chats
from api.index import get_chats_endpoint


class BotHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_chat_ids_deduplicates_numeric_ids(self):
        self.assertEqual(parse_chat_ids("-1001, 42 -1001"), [-1001, 42])

    def test_parse_chat_ids_rejects_empty_or_non_numeric_values(self):
        for value in ("", "first, second"):
            with self.assertRaises(ValueError):
                parse_chat_ids(value)

    async def test_worker_chat_request_failure_is_not_reported_as_empty_list(self):
        with patch("worker_client._request", new=AsyncMock(side_effect=RuntimeError("offline"))):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                await get_chats()

    async def test_chats_endpoint_preserves_worker_unavailable_status(self):
        unavailable = HTTPException(status_code=503, detail="Telegram Worker is unavailable")
        with patch("api.index.worker_get_chats", new=AsyncMock(side_effect=unavailable)):
            with self.assertRaises(HTTPException) as raised:
                await get_chats_endpoint(x_worker_secret=os.getenv("WORKER_SECRET"))

        self.assertEqual(raised.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
