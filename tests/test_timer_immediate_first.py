"""Regression tests for immediate-first timer scheduling."""

import asyncio
import json
import time

from services import broadcast_runner as br


class ClientFactory:
    async def __call__(self):
        class Client:
            async def disconnect(self):
                pass

        return Client()


def run_delivery(body, sender):
    old_sender = br._default_sender
    br._default_sender = sender
    try:
        return asyncio.run(
            br.handle_delivery(body, client_factory=ClientFactory(), notifier=None)
        )
    finally:
        br._default_sender = old_sender


def test_timer_first_cycle_is_scheduled_immediately(fake_redis, spy_publisher):
    before = time.time()
    task = br.create_timer_task(
        42,
        "TEST",
        ["-1001"],
        interval_minutes=20,
        total_repeats=5,
    )
    after = time.time()

    assert len(spy_publisher.calls) == 1
    first = spy_publisher.calls[0]
    assert (first["repeat_no"], first["batch_no"]) == (0, 0)
    assert first["delay_seconds"] == 1
    assert before <= task["first_run_at"] <= after
    assert before <= task["next_run"] <= after
    assert task["total_repeats"] == 5


def test_after_immediate_cycle_next_repeat_uses_interval(fake_redis, spy_publisher):
    task = br.create_timer_task(
        42,
        "TEST",
        ["-1001"],
        interval_minutes=20,
        total_repeats=2,
    )
    sent = []

    async def sender(client, gid, text):
        sent.append(gid)

    result = run_delivery(
        {"task_id": task["id"], "repeat_no": 0, "batch_no": 0}, sender
    )

    assert result["status"] == "repeat_committed"
    assert sent == ["-1001"]
    assert len(spy_publisher.calls) == 2

    initial, next_repeat = spy_publisher.calls
    assert initial["delay_seconds"] == 1
    assert (next_repeat["repeat_no"], next_repeat["batch_no"]) == (1, 0)
    assert 1190 <= next_repeat["delay_seconds"] <= 1200

    stored = json.loads(fake_redis.store[br.task_key(task["id"])])
    assert stored["completed_repeats"] == 1
    assert stored["status"] == "active"
    assert stored["next_run"] >= task["created_at"] + 1190


def test_one_repeat_means_one_immediate_send_only(fake_redis, spy_publisher):
    task = br.create_timer_task(
        42,
        "TEST",
        ["-1001"],
        interval_minutes=20,
        total_repeats=1,
    )
    sent = []

    async def sender(client, gid, text):
        sent.append(gid)

    result = run_delivery(
        {"task_id": task["id"], "repeat_no": 0, "batch_no": 0}, sender
    )

    assert result["status"] == "repeat_committed"
    assert result["task_status"] == "completed"
    assert sent == ["-1001"]
    # Only the creation delivery exists; no repeat 1 is scheduled.
    assert len(spy_publisher.calls) == 1


def test_infinite_timer_sends_now_then_remains_active(fake_redis, spy_publisher):
    task = br.create_timer_task(
        42,
        "TEST",
        ["-1001"],
        interval_minutes=10,
        total_repeats=None,
    )

    async def sender(client, gid, text):
        pass

    result = run_delivery(
        {"task_id": task["id"], "repeat_no": 0, "batch_no": 0}, sender
    )

    assert result["status"] == "repeat_committed"
    assert result["task_status"] == "active"
    assert len(spy_publisher.calls) == 2
    next_repeat = spy_publisher.calls[-1]
    assert next_repeat["repeat_no"] == 1
    assert 590 <= next_repeat["delay_seconds"] <= 600
