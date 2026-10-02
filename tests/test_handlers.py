"""Business-flow tests: menus, group selection, timers, task details."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'api'))

import pytest

import index as index
from services import broadcast_runner


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
                # aiogram 3.x HandlerObject.check returns a plain tuple
                # (passing, data); older CheckResult exposed a .passing
                # attribute — read the tuple element first, fall back to the
                # attribute, then to the raw value (plain bool).
                result = await h.check(cbq)
                if isinstance(result, tuple):
                    passing = bool(result[0])
                else:
                    passing = bool(getattr(result, "passing", result))
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
    """acquire_task_lock intentionally returns an OWNER TOKEN (not bool) so
    that release is compare-and-delete — a worker must never delete a lock
    that expired and was re-acquired by someone else."""
    from storage.models import acquire_task_lock, release_task_lock

    token = acquire_task_lock("t9")
    assert isinstance(token, str)
    assert token

    # second caller must not proceed
    assert acquire_task_lock("t9") is None

    # a foreign token must NOT release the lock
    release_task_lock("t9", "deadbeef" * 4)
    assert acquire_task_lock("t9") is None  # still locked

    # the real owner can release it
    release_task_lock("t9", token)
    token2 = acquire_task_lock("t9")
    assert isinstance(token2, str) and token2


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
    # chat ids are kept as strings end-to-end (see storage.models._decode_state)
    assert state["selected_groups"] == ["-5"]  # preserved across page switch

    cb = run_callback("toggle_group:-25")
    state = index.get_user_state(42)
    assert sorted(state["selected_groups"]) == ["-25", "-5"]

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
    # legacy index.schedule_process was intentionally removed; the real
    # planning point is services.broadcast_runner.publish_delivery (called
    # via schedule_first_batch inside create_timer_task).
    published = []
    monkeypatch.setattr(broadcast_runner, "publish_delivery",
                        lambda body, **kw: published.append((body, kw)))
    state = {"message_text": "hi", "selected_groups": ["-1001"], "interval_minutes": 10, "total_repeats": 4}
    task_id = asyncio.run(index.create_and_schedule_task(42, state))
    task = index.get_task(task_id)
    assert task["next_run"] > task["created_at"]
    assert task["next_run"] == pytest.approx(task["first_run_at"])
    assert task["first_run_at"] == pytest.approx(task["created_at"] + 600)
    assert task["interval_minutes"] == 10
    assert task["total_repeats"] == 4
    # exactly ONE initial delivery: repeat_no=0, batch_no=0
    assert len(published) == 1
    assert published[0][0] == {"task_id": task_id, "repeat_no": 0, "batch_no": 0}


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


def test_infinite_timer_can_be_created_tracked_and_cancelled(fake_redis, monkeypatch):
    # legacy index.schedule_process was intentionally removed; patch the real
    # planning point in services.broadcast_runner instead.
    published = []
    monkeypatch.setattr(broadcast_runner, "publish_delivery",
                        lambda body, **kw: published.append((body, kw)))
    index.set_user_state(42, {
        "step": "waiting_for_repeats",
        "message_text": "вечная рассылка",
        "selected_groups": ["-1"],
        "interval_minutes": 5,
    })

    created = run_callback("repeats_infinite")
    tasks = index.get_user_tasks(42)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["total_repeats"] is None
    assert task["status"] == "active"
    assert len(published) == 1  # exactly one initial delivery
    assert "Остановить" in created.edits[-1][0] or "Мои таймеры" in created.edits[-1][0]

    details = run_callback(f"task_details:{task['id']}")
    body = details.edits[-1][0]
    assert "Выполнено циклов рассылки: 0 циклов" in body
    assert "Повторений: ∞" in body
    assert any("Отменить" in button.text
               for row in details.edits[-1][1].inline_keyboard for button in row)

    run_callback(f"cancel_task:{task['id']}")
    assert index.get_task(task["id"])["status"] == "cancelled"

    # a later (already-planned) QStash delivery for the cancelled task must
    # not send anything: handle_delivery returns inactive_or_missing.
    sent = []

    async def recording_sender(client, gid, text):
        sent.append(gid)
        return "sent"

    old_sender = broadcast_runner._default_sender
    broadcast_runner._default_sender = recording_sender
    try:
        result = asyncio.run(broadcast_runner.handle_delivery(
            {"task_id": task["id"], "repeat_no": 0, "batch_no": 0},
            headers={}, client_factory=object()))
    finally:
        broadcast_runner._default_sender = old_sender
    assert result.get("status") == "inactive_or_missing"
    assert sent == []


def test_task_details_shows_all_fields(fake_redis):
    task = {"id": "bbbb2222-3333", "user_id": 42, "status": "active", "message": "тест текст",
            "groups": ["-1", "-2", "-3"], "interval_minutes": 15,
            "completed_repeats": 1, "total_repeats": 5, "next_run": 1770000000.0}
    index.save_task(task)
    cb = run_callback(f"task_details:{task['id']}")
    body = cb.edits[-1][0]
    for fragment in ("Таймер", "Статус", "Интервал", "Выполнено циклов рассылки: 1 из 5",
                     "Повторений: 5", "Групп выбрано: 3", "До запуска", "Следующий запуск"):
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
