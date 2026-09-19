"""Regression tests for webhook handler helpers that do not need Telegram."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import index


class FakeMessage:
    def __init__(self):
        self.from_user = SimpleNamespace(id=42)
        self.answers = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


def test_start_sends_menu_when_redis_is_unavailable(monkeypatch):
    """The start command must still respond instead of failing before answer()."""
    monkeypatch.setattr(index, "clear_user_state", lambda _user_id: (_ for _ in ()).throw(RuntimeError("Redis unavailable")))
    message = FakeMessage()

    asyncio.run(index.cmd_start(message))

    assert message.answers[0][0] == "🤖 Панель рассылки\n\nВыбери действие:"
    assert message.answers[0][1] is not None


def test_task_lock_accepts_upstash_success_response(monkeypatch):
    class FakeRedis:
        @staticmethod
        def set(*_args, **_kwargs):
            return "OK"

    monkeypatch.setattr(index, "redis", FakeRedis())
    assert index.acquire_task_lock("task-id") is True


def test_group_keyboard_uses_telethon_dialog_name():
    keyboard = index.groups_selection_keyboard([], [SimpleNamespace(id=-100, name="Новости")])
    assert keyboard.inline_keyboard[0][0].text == "☐ Новости"
