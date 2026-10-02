"""Regression tests for delivery hardening added after production failures."""

import asyncio
import json
import time

from services import broadcast_runner as br


class ClientFactory:
    async def __call__(self):
        class Client:
            async def disconnect(self):
                return None

        return Client()


def _task(groups, *, interval_seconds=600):
    now = time.time()
    return {
        "id": "delivery-fix-task",
        "user_id": 42,
        "message": "TEST",
        "groups": list(groups),
        "interval_minutes": max(1, interval_seconds // 60),
        "interval_seconds": interval_seconds,
        "total_repeats": 1,
        "completed_repeats": 0,
        "status": "active",
        "created_at": now - interval_seconds,
        "first_run_at": now - 10,
        "next_run": now - 10,
    }


def _persist(fake_redis, task):
    fake_redis.store[br.task_key(task["id"])] = json.dumps(task)


def _sender(record):
    async def send(client, gid, text):
        record.append(gid)

    return send


def test_second_batch_does_not_wait_another_repeat_interval(fake_redis, spy_publisher, monkeypatch):
    """A repeat interval separates repeats, never batches of the same repeat."""
    monkeypatch.setattr(br, "BATCH_SIZE", 5)
    groups = [f"g{i}" for i in range(6)]
    task = _task(groups, interval_seconds=600)
    _persist(fake_redis, task)
    sends = []

    old_sender = br._default_sender
    br._default_sender = _sender(sends)
    try:
        first = asyncio.run(
            br.handle_delivery(
                {"task_id": task["id"], "repeat_no": 0, "batch_no": 0},
                client_factory=ClientFactory(),
            )
        )
        assert first["status"] == "batch_done"
        assert first["next_batch"] == 1
        assert sends == groups[:5]
        assert spy_publisher.calls[-1]["batch_no"] == 1
        assert spy_publisher.calls[-1]["delay_seconds"] == 1

        # No make_due()/clock fast-forward here.  The next batch must be
        # eligible immediately even though the repeat interval is 10 minutes.
        second = asyncio.run(
            br.handle_delivery(
                {"task_id": task["id"], "repeat_no": 0, "batch_no": 1},
                client_factory=ClientFactory(),
            )
        )
        assert second["status"] == "repeat_committed"
        assert sends == groups
    finally:
        br._default_sender = old_sender


def test_out_of_order_batch_waits_for_previous_batch(fake_redis, spy_publisher):
    task = _task([f"g{i}" for i in range(6)])
    _persist(fake_redis, task)
    sends = []

    old_sender = br._default_sender
    br._default_sender = _sender(sends)
    try:
        result = asyncio.run(
            br.handle_delivery(
                {"task_id": task["id"], "repeat_no": 0, "batch_no": 1},
                client_factory=ClientFactory(),
            )
        )
    finally:
        br._default_sender = old_sender

    assert result["status"] == "previous_batch_incomplete"
    assert sends == []
    assert spy_publisher.calls[-1]["batch_no"] == 1
    assert spy_publisher.calls[-1]["delay_seconds"] == 5


def test_default_sender_hydrates_entity_cache_after_access_hash_miss():
    calls = []

    class FakeClient:
        def __init__(self):
            self.warmed = False

        async def get_input_entity(self, gid):
            calls.append(("resolve", gid, self.warmed))
            if not self.warmed:
                raise ValueError("Could not find the input entity")
            return ("input-peer", gid)

        async def get_dialogs(self, limit=None):
            calls.append(("dialogs", limit))
            self.warmed = True
            return []

        async def send_message(self, target, text, parse_mode=None):
            calls.append(("send", target, text, parse_mode))

    client = FakeClient()
    asyncio.run(br._default_sender(client, "-100123456", "hello"))

    assert calls[0][0] == "resolve"
    assert calls[1] == ("dialogs", None)
    assert calls[2][0] == "resolve"
    assert calls[3] == ("send", ("input-peer", -100123456), "hello", "html")


def test_slow_mode_wait_is_rescheduled_like_flood_wait():
    class SlowModeWaitError(Exception):
        def __init__(self, seconds):
            self.seconds = seconds
            super().__init__(f"wait {seconds}")

    reason, permanent = br.classify_exception(SlowModeWaitError(20))
    assert reason == "flood_wait"
    assert permanent is False
