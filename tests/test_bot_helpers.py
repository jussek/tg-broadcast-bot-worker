"""Tests for bot input parsing and Worker failure handling."""
import unittest
from unittest.mock import AsyncMock, patch

from bot import parse_chat_ids
from worker_client import get_chats


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


if __name__ == "__main__":
    unittest.main()
