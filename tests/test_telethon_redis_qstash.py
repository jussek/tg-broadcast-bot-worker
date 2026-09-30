"""Redis layer semantics, Telethon lifecycle and /api/process HTTP adapter.

Updated for the unified delivery engine (services/broadcast_runner):
  * payload schema is {task_id, repeat_no, batch_no} (legacy "expected_repeat"
    is still accepted by handle_delivery);
  * sending goes through broadcast_runner.execute_batch — the removed
    index.broadcast_message no longer exists;
  * scheduling goes through broadcast_runner.publish_delivery (spied) — the
    removed index.schedule_process no longer exists.
"""
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


# ------------------------------------------------------------------ helpers
def make_task(**overrides):
    task = {"id": "t1", "user_id": 1, "status": "active", "message": "m",
            "groups": ["-1001"], "completed_repeats": 0, "total_repeats": 2,
            "interval_minutes": 5, "interval_seconds": 300,
            "created_at": 0.0, "first_run_at": 0.0, "next_run": 0.0}
    task.update(overrides)
    return task


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, entity, text, parse_mode=None):
        self.sent.append(str(entity))

    async def disconnect(self):
        pass


async def ok_factory():
    return FakeClient()


def install_fake_telethon(monkeypatch, factory=None):
    """Route the engine's default client factory to a fake Telethon client."""
    monkeypatch.setattr(broadcast_runner, "get_telethon_client",
                        factory or ok_factory, raising=False)


def sender_recorder(sink):
    async def _sender(client, gid, text):
        sink.append(gid)
        await client.send_message(gid, text)
    return _sender


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
    monkeypatch.delenv("API_ID", raising=False)
    from services import telegram_delivery
    with pytest.raises(telegram_delivery.MissingEnvError):
        asyncio.run(telegram_delivery.get_telethon_client())


def test_get_telethon_client_unauthorized_disconnects(monkeypatch):
    class FakeTelethon:
        disconnected = False

        async def connect(self):
            pass

        async def is_user_authorized(self):
            return False

        async def disconnect(self):
            FakeTelethon.disconnected = True

    monkeypatch.setattr(index, "API_ID", None)
    monkeypatch.setattr(index, "API_HASH", "hash")
    monkeypatch.setattr(index, "TELEGRAM_SESSION_STRING", "session")
    monkeypatch.setattr(index, "get_telethon_client_factory", lambda: FakeTelethon())

    with pytest.raises(index.SessionNotAuthorizedError):
        asyncio.run(index.get_telethon_client())
    assert FakeTelethon.disconnected is True


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


# ------------------------------------------------------------------- QStash
def test_create_and_schedule_task_persists_and_publishes(fake_redis, spy_publisher):
    state = {"message_text": "hi", "selected_groups": ["-1001", "-1001"],
             "interval_minutes": 5, "total_repeats": 3}
    task_id = asyncio.run(index.create_and_schedule_task(42, state))

    task = json.loads(fake_redis.store[f"broadcast:task:{task_id}"])
    assert task["status"] == "active"
    assert task["total_repeats"] == 3
    assert task["groups"] == ["-1001"]
    assert task_id in fake_redis.sets["broadcast:user:42:tasks"]
    # exactly ONE initial delivery, one full interval later, repeat 0 batch 0
    assert spy_publisher.batches == [(task_id, 0, 0)]
    assert 299 <= spy_publisher.calls[0]["delay_seconds"] <= 301


def test_duplicate_qstash_delivery_does_not_resend(fake_redis, monkeypatch, spy_publisher):
    """A repeated delivery of the same Message-Id must not re-send."""
    from fastapi.testclient import TestClient

    fake_redis.set("broadcast:task:t1", json.dumps(make_task()))

    sent = []
    install_fake_telethon(monkeypatch)
    monkeypatch.setattr(broadcast_runner, "_default_sender", sender_recorder(sent))

    client = TestClient(index.app)
    headers = {"Message-Id": "m1"}
    payload = {"task_id": "t1", "repeat_no": 0, "batch_no": 0}
    first = client.post("/api/process", json=payload, headers=headers)
    second = client.post("/api/process", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["ok"] is True
    assert second.json().get("status") == "duplicate_ignored"
    assert sent == ["-1001"]  # message sent exactly once despite two deliveries


def test_completed_task_stops_rescheduling(fake_redis, monkeypatch, spy_publisher):
    from fastapi.testclient import TestClient

    task = make_task(id="t2", completed_repeats=1)
    fake_redis.set("broadcast:task:t2", json.dumps(task))

    sent = []
    install_fake_telethon(monkeypatch)
    monkeypatch.setattr(broadcast_runner, "_default_sender", sender_recorder(sent))

    resp = TestClient(index.app).post(
        "/api/process", json={"task_id": "t2", "repeat_no": 1, "batch_no": 0})
    assert resp.json()["ok"] is True
    saved = json.loads(fake_redis.store["broadcast:task:t2"])
    assert saved["status"] == "completed"
    assert saved["completed_repeats"] == 2
    assert sent == ["-1001"]
    assert spy_publisher.batches == []  # no next-repeat job after final cycle


def test_early_delivery_waits_until_next_run(fake_redis, monkeypatch, spy_publisher):
    """A premature/replayed QStash request must not consume a send."""
    from fastapi.testclient import TestClient

    now = 1_800_000_000.0
    task = make_task(id="early", first_run_at=now + 75.2, next_run=now + 75.2)
    fake_redis.set("broadcast:task:early", json.dumps(task))

    sent = []
    install_fake_telethon(monkeypatch)
    monkeypatch.setattr(broadcast_runner, "_default_sender", sender_recorder(sent))
    monkeypatch.setattr(broadcast_runner.time, "time", lambda: now)

    response = TestClient(index.app).post(
        "/api/process", json={"task_id": "early", "repeat_no": 0, "batch_no": 0},
        headers={"Message-Id": "too-soon"})

    assert response.json()["status"] == "not_due_yet"
    assert sent == []
    assert spy_publisher.batches == [("early", 0, 0)]
    assert 74 < spy_publisher.calls[0]["delay_seconds"] <= 75.2
    assert index.get_task("early")["completed_repeats"] == 0
    assert "broadcast:processed:early:0" not in fake_redis.store


def test_stale_delivery_cannot_trigger_the_next_repeat(fake_redis, monkeypatch):
    """An old repeat-zero replay must be ignored, not invent a new run."""
    from fastapi.testclient import TestClient

    task = make_task(id="generation", completed_repeats=1, total_repeats=3)
    fake_redis.set("broadcast:task:generation", json.dumps(task))
    sent = []
    install_fake_telethon(monkeypatch)
    monkeypatch.setattr(broadcast_runner, "_default_sender", sender_recorder(sent))

    response = TestClient(index.app).post(
        "/api/process",
        json={"task_id": "generation", "expected_repeat": 0},
        headers={"Message-Id": "obsolete-repeat-zero"},
    )

    assert response.json()["status"] == "stale_delivery"
    assert sent == []
    assert index.get_task("generation")["completed_repeats"] == 1


def test_timer_runs_at_each_interval_exactly_requested_times(fake_redis, monkeypatch, spy_publisher):
    """Exercise the complete three-cycle lifecycle via the real endpoint."""
    from fastapi.testclient import TestClient

    clock = [1_800_000_000.0]
    task = make_task(id="lifecycle", total_repeats=3,
                     first_run_at=clock[0], next_run=clock[0])
    fake_redis.set("broadcast:task:lifecycle", json.dumps(task))
    sends = []

    async def sender(client, gid, text):
        sends.append((clock[0], gid, text))
        await client.send_message(gid, text)

    install_fake_telethon(monkeypatch)
    monkeypatch.setattr(broadcast_runner, "_default_sender", sender)
    monkeypatch.setattr(broadcast_runner.time, "time", lambda: clock[0])

    client = TestClient(index.app)
    for repeat in range(3):
        response = client.post(
            "/api/process",
            json={"task_id": "lifecycle", "repeat_no": repeat, "batch_no": 0},
            headers={"Message-Id": f"lifecycle-{repeat}"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "repeat_committed"
        saved = index.get_task("lifecycle")
        assert saved["completed_repeats"] == repeat + 1
        if repeat < 2:
            assert saved["status"] == "active"
            # deterministic schedule: base + N*interval — no drift
            assert saved["next_run"] == pytest.approx(1_800_000_000.0 + (repeat + 1) * 300)
            clock[0] = saved["next_run"]

    assert sends == [
        (1_800_000_000.0, "-1001", "m"),
        (1_800_000_300.0, "-1001", "m"),
        (1_800_000_600.0, "-1001", "m"),
    ]
    assert spy_publisher.batches == [("lifecycle", 1, 0), ("lifecycle", 2, 0)]
    assert index.get_task("lifecycle")["status"] == "completed"


def test_retryable_configuration_error_does_not_consume_repeat(fake_redis, monkeypatch):
    """A temporary configuration failure must remain retryable by QStash."""
    from fastapi.testclient import TestClient

    task = make_task(id="retryable")
    fake_redis.set("broadcast:task:retryable", json.dumps(task))

    async def unavailable():
        raise index.MissingEnvError("API_ID is not configured")

    monkeypatch.setattr(broadcast_runner, "get_telethon_client", unavailable)
    response = TestClient(index.app).post(
        "/api/process",
        json={"task_id": "retryable", "repeat_no": 0, "batch_no": 0},
        headers={"Message-Id": "retryable-zero"},
    )

    assert response.status_code == 503
    assert index.get_task("retryable")["completed_repeats"] == 0
    assert "broadcast:processed:retryable:0" not in fake_redis.store
    # delivery NOT marked seen -> provider retry can safely re-run it
    assert "broadcast:qstash_message:retryable-zero" not in fake_redis.store


def test_cancelled_task_not_executed(fake_redis, monkeypatch):
    from fastapi.testclient import TestClient

    task = make_task(id="t3", status="cancelled")
    fake_redis.set("broadcast:task:t3", json.dumps(task))

    executed = []
    install_fake_telethon(monkeypatch)
    monkeypatch.setattr(broadcast_runner, "_default_sender", sender_recorder(executed))

    resp = TestClient(index.app).post(
        "/api/process", json={"task_id": "t3", "repeat_no": 0, "batch_no": 0})
    assert resp.json()["status"] == "inactive_or_missing"
    assert executed == []
