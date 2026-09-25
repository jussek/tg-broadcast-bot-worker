"""PRIORITY 5: every callback_data routes to a handler and answers exactly once."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'api'))

import pytest

import index as index


class FakeCallback:
    """Records answer()/edit_text() calls like the real CallbackQuery."""

    def __init__(self, data, user_id=42):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.edits = []
        self.message = SimpleNamespace(
            edit_text=self._edit_text,
            edit_reply_markup=lambda reply_markup=None: self.edits.append(("markup", reply_markup)),
        )

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


def matching_handlers(callback_data):
    """Resolve handlers exactly like the Dispatcher does: filters are checked
    against the CallbackQuery object (Update.callback_query), not the Update."""
    update = make_update(callback_data)
    callback_query = update.callback_query

    async def _match():
        matched = []
        for h in index.dp.callback_query.handlers:
            try:
                passing, _kwargs = await h.check(callback_query)
            except Exception:
                passing = False
            if passing:
                matched.append(h)
        return matched

    return asyncio.run(_match())


CALLBACKS_WITHOUT_ARGS = [
    "new_broadcast", "write_message", "use_last_message", "last_message",
    "select_template", "select_live_groups", "select_group_list",
    "continue_to_send", "send_now", "schedule_task", "cancel", "templates",
    "create_template", "tasks", "groups", "create_group_list",
    "back_broadcast", "back_menu", "ignore",
]

PREFIXED_CALLBACKS = [
    "use_template:t1", "use_group_list:l1", "toggle_group:-100123",
    "groups_page:1", "manage_template:t1", "delete_template:t1",
    "task_details:abc", "cancel_task:abc", "manage_group_list:l1",
    "delete_group_list:l1",
]


@pytest.mark.parametrize("data", CALLBACKS_WITHOUT_ARGS + PREFIXED_CALLBACKS)
def test_every_callback_has_exactly_one_handler(data):
    handlers = matching_handlers(data)
    assert len(handlers) == 1, f"{data}: expected 1 handler, got {len(handlers)}"


@pytest.mark.parametrize("data", ["new_broadcast", "write_message", "back_menu", "cancel",
                                  "templates", "tasks", "groups", "back_broadcast",
                                  "create_template", "schedule_task", "ignore"])
def test_handler_answers_callback(monkeypatch, data):
    callback = FakeCallback(data)
    monkeypatch.setattr(index, "get_user_state", lambda uid: {"message_text": "m", "selected_groups": ["-1"]})
    monkeypatch.setattr(index, "set_user_state", lambda uid, s: None)
    monkeypatch.setattr(index, "clear_user_state", lambda uid: None)
    monkeypatch.setattr(index, "get_user_templates", lambda uid: [])
    monkeypatch.setattr(index, "get_group_lists", lambda uid: [])
    monkeypatch.setattr(index, "get_user_tasks", lambda uid: [])
    monkeypatch.setattr(index, "get_last_message", lambda uid: None)
    monkeypatch.setattr(index, "load_groups_cache", lambda uid: [{"id": "-1001", "title": "G"}])

    handler = matching_handlers(data)[0]
    asyncio.run(handler.callback(callback))

    assert len(callback.answers) == 1, f"{data}: callback.answer must be called exactly once (got {len(callback.answers)})"


def test_toggle_group_updates_state_and_answers_once(monkeypatch):
    state = {"step": "selecting_groups", "message_text": "hi", "selected_groups": []}
    saved = []
    monkeypatch.setattr(index, "get_user_state", lambda uid: dict(state))
    monkeypatch.setattr(index, "set_user_state", lambda uid, s: saved.append(s))
    monkeypatch.setattr(index, "load_groups_cache", lambda uid: [{"id": "-1001", "title": "Первая"}, {"id": "-1002", "title": "Вторая"}])

    callback = FakeCallback("toggle_group:-1001")
    handler = matching_handlers("toggle_group:-1001")[0]
    asyncio.run(handler.callback(callback))

    assert saved[0]["selected_groups"] == ["-1001"]
    assert len(callback.answers) == 1
    text, markup = callback.edits[-1]
    button_texts = [b.text for row in markup.inline_keyboard for b in row]
    assert any(t.startswith("☑️ Первая") for t in button_texts)


def test_select_all_groups_selects_every_cached_group(monkeypatch):
    saved = []
    monkeypatch.setattr(index, "get_user_state", lambda uid: {"selected_groups": []})
    monkeypatch.setattr(index, "set_user_state", lambda uid, s: saved.append(s))
    monkeypatch.setattr(index, "load_groups_cache", lambda uid: [{"id": "-1", "title": "A"}, {"id": "-2", "title": "B"}])

    callback = FakeCallback("select_all_groups")
    handler = matching_handlers("select_all_groups")[0]
    asyncio.run(handler.callback(callback))

    assert sorted(saved[0]["selected_groups"]) == ["-1", "-2"]
    assert len(callback.answers) == 1


def test_groups_page_clamps_out_of_range(monkeypatch):
    saved = {}

    def fake_set(uid, s):
        saved.update(s)

    monkeypatch.setattr(index, "get_user_state", lambda uid: {"selected_groups": ["-1"], "group_page": 0})
    monkeypatch.setattr(index, "set_user_state", fake_set)
    monkeypatch.setattr(index, "load_groups_cache", lambda uid: [{"id": f"-{i}", "title": f"G{i}"} for i in range(5)])

    callback = FakeCallback("groups_page:99")
    handler = matching_handlers("groups_page:99")[0]
    asyncio.run(handler.callback(callback))

    assert saved["group_page"] == 0  # 5 groups -> single page
    assert len(callback.answers) == 1


def test_corrupted_cache_does_not_crash_toggle(monkeypatch):
    monkeypatch.setattr(index, "get_user_state", lambda uid: {"selected_groups": []})
    monkeypatch.setattr(index, "set_user_state", lambda uid, s: None)
    monkeypatch.setattr(index, "load_groups_cache", lambda uid: None)  # missing/corrupted

    callback = FakeCallback("toggle_group:-1001")
    handler = matching_handlers("toggle_group:-1001")[0]
    asyncio.run(handler.callback(callback))

    assert len(callback.answers) == 1
    assert callback.answers[0][1].get("show_alert") is True


def test_handler_error_still_answers_with_alert(monkeypatch):
    monkeypatch.setattr(index, "get_user_state", lambda uid: (_ for _ in ()).throw(RuntimeError("boom")))
    callback = FakeCallback("continue_to_send")
    handler = matching_handlers("continue_to_send")[0]
    asyncio.run(handler.callback(callback))
    assert len(callback.answers) == 1
    assert "ошибка" in callback.answers[0][0].lower()


# ---------------------------------------------------------------------------
# Regression: "Мои таймеры" must show ONLY active timers; cancelling a timer
# from the list must not throw (previously showed "Произошла ошибка").
# ---------------------------------------------------------------------------
def _patch_task_store(monkeypatch, tasks):
    store = {t["id"]: dict(t) for t in tasks}

    def fake_get_user_tasks(uid):
        return [dict(t) for t in store.values() if int(t.get("user_id", -1)) == int(uid)]

    def fake_get_task(tid):
        return dict(store[tid]) if tid in store else None

    def fake_save_task(task):
        store[task["id"]] = dict(task)

    monkeypatch.setattr(index, "get_user_tasks", fake_get_user_tasks)
    monkeypatch.setattr(index, "get_task", fake_get_task)
    monkeypatch.setattr(index, "save_task", fake_save_task)
    return store


TASKS_MIXED = [
    {"id": "act1", "user_id": 42, "status": "active", "interval_minutes": 5,
     "completed_repeats": 1, "total_repeats": 3, "groups": ["-1"], "message": "hi",
     "next_run": 1e12, "created_at": 3},
    {"id": "done1", "user_id": 42, "status": "completed", "interval_minutes": 5,
     "completed_repeats": 3, "total_repeats": 3, "groups": ["-1"], "message": "hi",
     "created_at": 2},
    {"id": "canc1", "user_id": 42, "status": "cancelled", "interval_minutes": 5,
     "completed_repeats": 0, "total_repeats": 3, "groups": ["-1"], "message": "hi",
     "created_at": 1},
]


def test_tasks_screen_shows_only_active_timers(monkeypatch):
    _patch_task_store(monkeypatch, TASKS_MIXED)
    callback = FakeCallback("tasks")
    handler = matching_handlers("tasks")[0]
    asyncio.run(handler.callback(callback))

    assert len(callback.answers) == 1
    text, markup = callback.edits[-1]
    button_data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "task_details:act1" in button_data
    assert "cancel_task:act1" in button_data
    assert not any("done1" in d or "canc1" in d for d in button_data if ":" in d)
    assert "done1" not in text and "canc1" not in text
    assert "Активных таймеров: 1" in text


def test_cancel_task_from_list_does_not_error_and_hides_timer(monkeypatch):
    _patch_task_store(monkeypatch, TASKS_MIXED)
    callback = FakeCallback("cancel_task:act1")
    handler = matching_handlers("cancel_task:act1")[0]
    asyncio.run(handler.callback(callback))

    assert len(callback.answers) == 1
    toast = callback.answers[0][0] or ""
    assert "ошибка" not in toast.lower()
    assert "Отменён" in toast or "отменён" in toast
    # The re-rendered list must no longer contain the cancelled timer.
    _, markup = callback.edits[-1]
    button_data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "task_details:act1" not in button_data


def test_groups_screen_renders_without_telethon(monkeypatch):
    monkeypatch.setattr(index, "get_group_lists",
                        lambda uid: [{"id": "l1", "name": "Список 1", "groups": ["-1", "-2"]}])
    callback = FakeCallback("groups")
    handler = matching_handlers("groups")[0]
    asyncio.run(handler.callback(callback))

    assert len(callback.answers) == 1
    text, markup = callback.edits[-1]
    button_data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "manage_group_list:l1" in button_data
    assert "create_group_list" in button_data
    assert "ошибка" not in (callback.answers[0][0] or "").lower()
