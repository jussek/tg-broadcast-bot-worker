"""PRIORITY 6-10: Telethon lifecycle, Redis layer semantics, QStash flows."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from api import index


# ---------------------------------------------------------------- Redis fake
class FakeRedis:
    def __init__(self):
        self.store = {}
        self.sets = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and (key in self.store):
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


@pytest.fixture()
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(index, "get_redis", lambda: r)
    import storage.redis_client as rc
    monkeypatch.setattr(rc, "_redis", r)
    return r


# ------------------------------------------------------------- groups cache
def test_cache_missing_vs_empty_are_distinct(fake_redis):
    assert index.load_groups_cache(1) is None          # no cache at all
    fake_redis.set("broadcast:groups:1", json.dumps([]))
    assert index.load_groups_cache(1) == []            # cached empty list
    fake_redis.set("broadcast:groups:1", "{corrupt")
    assert index.load_groups_cache(1) is None          # corrupted -> treated as missing


def test_save_and_load_groups_cache_roundtrip_unicode(fake_redis):
    groups = [{"id": "-1001234567890", "title": "Новости 🚀 / русский"}]
    index.save_groups_cache(7, groups)
    assert index.load_groups_cache(7) == groups


# ------------------------------------------------------------------ state
def test_state_saved_between_callbacks(fake_redis):
    index.set_user_state(5, {"step": "selecting_groups", "selected_groups": ["-1"], "message_text": "x"})
    state = index.get_user_state(5)
    assert state["selected_groups"] == ["-1"]
    index.clear_user_state(5)
    assert index.get_user_state(5) is None


# ---------------------------------------------------------------- Telethon
def test_get_telethon_client_requires_env(monkeypatch):
    monkeypatch.setattr(index, "API_ID", None)
    with pytest.raises(index.MissingEnvError):
        asyncio.run(index.get_telethon_client())


def test_get_telethon_client_unauthorized_disconnects(monkeypatch):
    class FakeClient:
        disconnected = False

        async def connect(self):
            pass

        async def is_user_authorized(self):
            return False

        async def disconnect(self):
            FakeClient.disconnected = True

    monkeypatch.setattr(index, "API_ID", "1")
    monkeypatch.setattr(index, "API_HASH", "hash")
    monkeypatch.setattr(index, "TELEGRAM_SESSION_STRING", "session")
    monkeypatch.setattr(index, "TelegramClient", lambda *a, **k: FakeClient())

    with pytest.raises(index.SessionNotAuthorizedError):
        asyncio.run(index.get_telethon_client())
    assert FakeClient.disconnected is True


def test_fetch_user_dialogs_normalizes_groups_channels(monkeypatch):
    dialogs = [
        SimpleNamespace(id=-1001, is_group=True, is_channel=False, title="Группа", name="Группа"),
        SimpleNamespace(id=-1002, is_group=False, is_channel=True, title="Канал", name="Канал"),
        SimpleNamespace(id=555, is_group=False, is_channel=False, title="Личка", name="Личка"),
        SimpleNamespace(id=-1003, is_group=True, is_channel=False, title=None, name="Без имени"),
    ]

    class FakeClient:
        async def iter_dialogs(self):
            for d in dialogs:
                yield d

    result = asyncio.run(index.fetch_user_dialogs(FakeClient()))
    assert result == [
        {"id": "-1001", "title": "Группа"},
        {"id": "-1002", "title": "Канал"},
        {"id": "-1003", "title": "Без имени"},
    ]


def test_broadcast_message_isolates_failures(monkeypatch):
    calls = {"sent": [], "disconnected": False}

    class FakeClient:
        async def get_entity(self, gid):
            return gid

        async def send_message(self, entity, text, parse_mode=None):
            if entity == "bad":
                raise ValueError("boom")
            calls["sent"].append(entity)

        async def disconnect(self):
            calls["disconnected"] = True

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr(index, "get_telethon_client", fake_client)
    result = asyncio.run(index.broadcast_message(["good", "bad", "good2"], "hello"))

    assert result["success"] == 2
    assert len(result["failures"]) == 1
    assert calls["disconnected"] is True


# ------------------------------------------------------------------- QStash
def test_create_and_schedule_task_persists_and_publishes(fake_redis, monkeypatch):
    published = []
    monkeypatch.setattr(index, "schedule_process",
                        lambda task_id, delay: published.append((task_id, delay)))
    state = {"message_text": "hi", "selected_groups": ["-1001"], "interval_minutes": 5, "total_repeats": 3}
    task_id = asyncio.run(index.create_and_schedule_task(42, state))

    task = json.loads(fake_redis.store[f"broadcast:task:{task_id}"])
    assert task["status"] == "active"
    assert task["total_repeats"] == 3
    assert task_id in fake_redis.sets["broadcast:user:42:tasks"]
    assert published == [(task_id, 5)]


def test_duplicate_qstash_delivery_does_not_resend(fake_redis, client_app=None, monkeypatch=None):
    """A repeated delivery of the same repeat number must be ignored."""
    from fastapi.testclient import TestClient

    task = {"id": "t1", "user_id": 1, "status": "active", "message": "m",
            "groups": ["-1001"], "completed_repeats": 0, "total_repeats": 2,
            "interval_minutes": 5}
    fake_redis.set("broadcast:task:t1", json.dumps(task))

    sent = []

    async def fake_broadcast(groups, text):
        sent.append(list(groups))
        return {"success": len(groups), "failures": []}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(index, "broadcast_message", fake_broadcast)
    monkeypatch.setattr(index, "schedule_process", lambda *a, **k: None)
    monkeypatch.setattr(index, "BOT_TOKEN", None)

    try:
        client = TestClient(index.app)
        first = client.post("/api/process", json={"task_id": "t1"})
        second = client.post("/api/process", json={"task_id": "t1"})
        assert first.status_code == 200
        assert first.json()["ok"] is True
        assert second.json().get("status") == "duplicate_ignored"
        assert len(sent) == 1  # message sent exactly once despite two deliveries
    finally:
        monkeypatch.undo()


def test_completed_task_stops_rescheduling(fake_redis, monkeypatch):
    from fastapi.testclient import TestClient

    task = {"id": "t2", "user_id": 1, "status": "active", "message": "m",
            "groups": ["-1001"], "completed_repeats": 1, "total_repeats": 2,
            "interval_minutes": 5}
    fake_redis.set("broadcast:task:t2", json.dumps(task))

    async def fake_broadcast(groups, text):
        return {"success": 1, "failures": []}

    rescheduled = []
    monkeypatch.setattr(index, "broadcast_message", fake_broadcast)
    monkeypatch.setattr(index, "schedule_process", lambda *a, **k: rescheduled.append(a))
    monkeypatch.setattr(index, "BOT_TOKEN", None)

    client = TestClient(index.app)
    resp = client.post("/api/process", json={"task_id": "t2"})
    assert resp.json()["ok"] is True
    saved = json.loads(fake_redis.store["broadcast:task:t2"])
    assert saved["status"] == "completed"
    assert rescheduled == []


def test_cancelled_task_not_executed(fake_redis, monkeypatch):
    from fastapi.testclient import TestClient

    task = {"id": "t3", "user_id": 1, "status": "cancelled", "message": "m",
            "groups": ["-1001"], "completed_repeats": 0, "total_repeats": 2}
    fake_redis.set("broadcast:task:t3", json.dumps(task))

    executed = []

    async def fake_broadcast(groups, text):
        executed.append(1)
        return {"success": 0, "failures": []}

    monkeypatch.setattr(index, "broadcast_message", fake_broadcast)
    client = TestClient(index.app)
    resp = client.post("/api/process", json={"task_id": "t3"})
    assert resp.json()["status"] == "inactive_or_missing"
    assert executed == []
