"""Regression tests for webhook handler helpers that do not need Telegram."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import index


class FakeMessage:
    def __init__(self, text=None):
        self.from_user = SimpleNamespace(id=42)
        self.text = text
        self.caption = None
        self.answers = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=42)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


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


def test_main_menu_does_not_include_settings():
    keyboard = index.main_menu_keyboard()
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "settings" not in callbacks


def test_parse_positive_integer_rejects_invalid_values():
    assert index.parse_positive_integer("15") == 15
    assert index.parse_positive_integer(" 2 ") == 2
    assert index.parse_positive_integer("0") is None
    assert index.parse_positive_integer("-1") is None
    assert index.parse_positive_integer("one") is None


def test_timer_setup_accepts_custom_interval_and_repeat_count(monkeypatch):
    state = {
        "step": "waiting_for_interval",
        "message_text": "Hello",
        "selected_groups": ["-1001"],
    }
    saved_states = []
    scheduled_states = []

    monkeypatch.setattr(index, "get_user_state", lambda _user_id: state)
    monkeypatch.setattr(index, "set_user_state", lambda _user_id, value: saved_states.append(value.copy()))
    monkeypatch.setattr(index, "clear_user_state", lambda _user_id: None)

    async def fake_create_and_schedule(user_id, task_state):
        scheduled_states.append((user_id, task_state.copy()))
        return "12345678-task"

    monkeypatch.setattr(index, "create_and_schedule_task", fake_create_and_schedule)

    interval_message = FakeMessage("7")
    asyncio.run(index.handle_message(interval_message))

    assert saved_states[-1]["interval_minutes"] == 7
    assert saved_states[-1]["step"] == "waiting_for_repeats"
    assert "количество отправок" in interval_message.answers[-1][0]

    repeat_message = FakeMessage("3")
    asyncio.run(index.handle_message(repeat_message))

    assert scheduled_states == [(42, {
        "step": "waiting_for_repeats",
        "message_text": "Hello",
        "selected_groups": ["-1001"],
        "interval_minutes": 7,
        "total_repeats": 3,
    })]
    assert "через 7 мин." in repeat_message.answers[-1][0]
    assert "3 раз" in repeat_message.answers[-1][0]


def test_group_list_name_is_saved_from_waiting_state(monkeypatch):
    state = {
        "step": "waiting_for_group_list_name",
        "selected_groups": ["-1001", "-1002"],
    }
    saved_lists = []
    cleared_users = []

    monkeypatch.setattr(index, "get_user_state", lambda _user_id: state)
    monkeypatch.setattr(index, "get_group_lists", lambda _user_id: [])
    monkeypatch.setattr(index, "save_group_lists", lambda user_id, lists: saved_lists.append((user_id, lists)))
    monkeypatch.setattr(index, "clear_user_state", lambda user_id: cleared_users.append(user_id))

    message = FakeMessage("Основные каналы")
    asyncio.run(index.handle_message(message))

    assert saved_lists[0][0] == 42
    assert saved_lists[0][1][0]["name"] == "Основные каналы"
    assert saved_lists[0][1][0]["groups"] == ["-1001", "-1002"]
    assert cleared_users == [42]
    assert message.answers[-1][0] == "✅ Список «Основные каналы» сохранён."


def test_active_timer_can_be_cancelled(monkeypatch):
    task = {"task_id": "task-1", "user_id": 42, "status": "active"}
    saved_tasks = []
    callback = FakeCallback("cancel_task:task-1")

    monkeypatch.setattr(index, "get_task", lambda _task_id: task)
    monkeypatch.setattr(index, "save_task", lambda task_id, data: saved_tasks.append((task_id, data.copy())))

    async def fake_tasks_handler(_callback):
        return None

    monkeypatch.setattr(index, "cb_tasks", fake_tasks_handler)
    asyncio.run(index.cb_cancel_task(callback))

    assert len(saved_tasks) == 1
    task_id, saved = saved_tasks[0]
    assert task_id == "task-1"
    assert saved["status"] == "cancelled"
    assert saved["user_id"] == 42
    assert callback.answers[0][0] == "✅ Таймер отменён, рассылка остановлена."


def test_scheduled_task_records_next_run(monkeypatch):
    state = {
        "message_text": "Hello",
        "selected_groups": ["-1001"],
        "interval_minutes": 5,
        "total_repeats": 2,
    }
    saved_tasks = []

    monkeypatch.setattr(index, "save_task", lambda task_id, task: saved_tasks.append(task.copy()))
    monkeypatch.setattr(index, "ensure_redis", lambda: None)
    monkeypatch.setattr(index, "redis", SimpleNamespace(sadd=lambda *_args: None))
    monkeypatch.setattr(index, "ensure_qstash", lambda: None)
    monkeypatch.setattr(index, "qstash", SimpleNamespace(message=SimpleNamespace(publish_json=lambda **_kwargs: None)))
    monkeypatch.setattr(index.uuid, "uuid4", lambda: "task-id")
    monkeypatch.setattr(index.time, "time", lambda: 1_000.0)

    task_id = asyncio.run(index.create_and_schedule_task(42, state))

    assert task_id == "task-id"
    assert saved_tasks[0]["next_run"] == 1_300.0
