"""Business-flow tests: menus, group selection, timers, task details."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from api import index


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.sets = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return "OK"

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    def sadd(self, key, *values):
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.update(str(v) for v in values)
        return len(s) - before

    def smembers(self, key):
        return list(self.sets.get(key, set()))

    def srem(self, key, *values):
        s = self.sets.get(key, set())
        for v in values:
            s.discard(str(v))

    def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]


@pytest.fixture()
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(index, "get_redis", lambda: r)
    import storage.redis_client as rc
    monkeypatch.setattr(rc, "_redis", r)
    return r


class FakeCallback:
    def __init__(self, data, user_id=42):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.edits = []
        self.message = SimpleNamespace(edit_text=self._edit_text)

    async def _edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


def make_update(callback_data, user_id=42):
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    message = Message(
        message_id=1, date=0,
        chat=Chat(id=user_id, type="private"),
        text="menu",
    )
    callback_query = CallbackQuery(
        id="cb", from_user=User(id=user_id, is_bot=False, first_name="U"),
        chat_instance="ci", data=callback_data, message=message,
    )
    return Update(update_id=1, callback_query=callback_query)


def dispatch(callback_data):
    # aiogram registers callback_query handlers with event=CallbackQuery,
    # so filters must be checked against CallbackQuery, not Update.
    cbq = make_update(callback_data).callback_query

    async def _match():
        for h in index.dp.callback_query.handlers:
            try:
                # aiogram's CheckResult is a tuple subclass (passing, kwargs);
                # plain bools are also tolerated.
                result = await h.check(cbq)
                passing = getattr(result, "passing", result)
            except Exception:
                passing = False
            if passing:
                return h
        raise AssertionError(f"No handler matches {callback_data!r}")

    return asyncio.run(_match())


def run_callback(callback_data, user_id=42):
    callback = FakeCallback(callback_data, user_id)
    asyncio.run(dispatch(callback_data).callback(callback))
    return callback


# --------------------------------------------------------------------- locks
def test_task_lock_accepts_upstash_success_response(fake_redis):
    from storage.models import acquire_task_lock, release_task_lock
    assert acquire_task_lock("t9") is True
    assert acquire_task_lock("t9") is False  # second caller must not proceed
    release_task_lock("t9")
    assert acquire_task_lock("t9") is True


# ------------------------------------------------------------------ menus
def test_new_broadcast_shows_action_menu(fake_redis):
    cb = run_callback("new_broadcast")
    assert len(cb.answers) == 1
    texts = [b.text for _, markup in cb.edits for row in markup.inline_keyboard for b in row]
    assert any("Написать сообщение" in t for t in texts)


def test_write_message_sets_waiting_state(fake_redis):
    cb = run_callback("write_message")
    state = index.get_user_state(42)
    assert state["step"] == "waiting_for_message"
    assert len(cb.answers) == 1


def test_back_menu_clears_state(fake_redis):
    index.set_user_state(42, {"step": "selecting_groups", "message_text": "x"})
    cb = run_callback("back_menu")
    assert index.get_user_state(42) is None
    assert len(cb.answers) == 1


# ------------------------------------------------------------- group picker
def test_group_keyboard_uses_telethon_dialog_name(fake_redis):
    index.save_groups_cache(42, [{"id": "-100333", "title": "Мой канал 🚀"}])
    index.set_user_state(42, {"step": "selecting_groups", "selected_groups": [], "message_text": "m"})
    cb = run_callback("select_live_groups")
    button_texts = [b.text for _, markup in cb.edits for row in markup.inline_keyboard for b in row]
    assert any("Мой канал 🚀" in t for t in button_texts)
    assert len(cb.answers) == 1


def test_pagination_preserves_selected_groups(fake_redis):
    groups = [{"id": f"-{i}", "title": f"G{i}"} for i in range(1, 46)]  # 3 pages
    index.save_groups_cache(42, groups)
    index.set_user_state(42, {"step": "selecting_groups", "selected_groups": ["-5"], "group_page": 0})

    cb = run_callback("groups_page:1")
    state = index.get_user_state(42)
    assert state["group_page"] == 1
    assert state["selected_groups"] == [5]  # preserved across page switch

    cb = run_callback("toggle_group:-25")
    state = index.get_user_state(42)
    assert sorted(state["selected_groups"]) == [5, 25]

    cb = run_callback("groups_page:99")  # clamped to last page
    assert index.get_user_state(42)["group_page"] == 2


def test_toggle_then_continue_reaches_send_menu(fake_redis):
    index.save_groups_cache(42, [{"id": "-1001", "title": "A"}, {"id": "-1002", "title": "B"}])
    index.set_user_state(42, {"step": "selecting_groups", "selected_groups": [], "message_text": "hello"})
    run_callback("toggle_group:-1001")
    run_callback("toggle_group:-1002")
    cb = run_callback("continue_to_send")
    texts = [b.text for _, markup in cb.edits for row in markup.inline_keyboard for b in row]
    assert any("Отправить сейчас" in t for t in texts)


# ------------------------------------------------------------------- tasks
def test_scheduled_task_records_next_run(fake_redis, monkeypatch):
    monkeypatch.setattr(index, "schedule_process", lambda *a, **k: None)
    state = {"message_text": "hi", "selected_groups": ["-1001"], "interval_minutes": 10, "total_repeats": 4}
    task_id = asyncio.run(index.create_and_schedule_task(42, state))
    task = index.get_task(task_id)
    assert task["next_run"] > task["created_at"]
    assert task["interval_minutes"] == 10
    assert task["total_repeats"] == 4


def test_active_timer_can_be_cancelled(fake_redis):
    task = {"id": "aaa11111-2222", "user_id": 42, "status": "active", "message": "m",
            "groups": ["-1"], "interval_minutes": 5, "completed_repeats": 0, "total_repeats": 3}
    index.save_task(task)
    get_redis = index.get_redis()
    get_redis.sadd("broadcast:user:42:tasks", task["id"])

    cb = run_callback(f"cancel_task:{task['id']}")
    saved = json.loads(get_redis.store[f"broadcast:task:{task['id']}"])
    assert saved["status"] == "cancelled"
    assert len(cb.answers) >= 1


def test_task_details_shows_all_fields(fake_redis):
    task = {"id": "bbbb2222-3333", "user_id": 42, "status": "active", "message": "тест текст",
            "groups": ["-1", "-2", "-3"], "interval_minutes": 15,
            "completed_repeats": 1, "total_repeats": 5, "next_run": 1770000000.0}
    index.save_task(task)
    cb = run_callback(f"task_details:{task['id']}")
    body = cb.edits[-1][0]
    for fragment in ("Таймер", "Статус", "Интервал", "Повторы: 1/5", "Каналов: 3", "Следующий запуск"):
        assert fragment in body, f"missing {fragment!r} in task details"
    markup = cb.edits[-1][1]
    cancel_buttons = [b for row in markup.inline_keyboard for b in row if "Отменить" in b.text]
    assert cancel_buttons, "task details must offer a working cancel button"
    assert len(cb.answers) == 1


def test_task_details_rejects_foreign_task(fake_redis):
    task = {"id": "cccc3333", "user_id": 99, "status": "active", "message": "m",
            "groups": [], "interval_minutes": 5, "completed_repeats": 0, "total_repeats": 1}
    index.save_task(task)
    cb = run_callback(f"task_details:{task['id']}", user_id=42)
    assert len(cb.edits) == 0
    assert cb.answers[0][1].get("show_alert") is True


# ---------------------------------------------------------------- templates
def test_template_crud_roundtrip(fake_redis):
    index.save_user_templates(42, [])
    index.save_user_templates(42, [{"id": "tpl1", "name": "Шаблон", "message": "текст"}])
    assert index.get_user_templates(42)[0]["name"] == "Шаблон"
    cb = run_callback("manage_template:tpl1")
    assert "Шаблон" in cb.edits[-1][0]
    cb = run_callback("delete_template:tpl1")
    assert index.get_user_templates(42) == []
