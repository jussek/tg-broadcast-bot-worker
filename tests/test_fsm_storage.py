"""Tests for persistent aiogram state used by Vercel webhook handlers."""
import unittest
from unittest.mock import patch

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey

from storage.aiogram_supabase_storage import SupabaseFSMStorage


class Response:
    def __init__(self, data):
        self.data = data


class Query:
    def __init__(self, database):
        self.database = database

    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def upsert(self, record, **_kwargs):
        self.database.record = record
        return self

    def execute(self):
        return Response([self.database.record] if self.database.record else [])


class Database:
    def __init__(self):
        self.record = None

    def table(self, _name):
        return Query(self)


class SupabaseFSMStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_and_data_survive_storage_operations(self):
        database = Database()
        key = StorageKey(bot_id=1, chat_id=2, user_id=3)
        storage = SupabaseFSMStorage()
        state = State("form:message")

        with patch("storage.aiogram_supabase_storage.get_supabase_client", return_value=database):
            await storage.set_state(key, state)
            await storage.set_data(key, {"message": "hello"})
            self.assertEqual(await storage.get_state(key), state.state)
            self.assertEqual(await storage.get_data(key), {"message": "hello"})


if __name__ == "__main__":
    unittest.main()
