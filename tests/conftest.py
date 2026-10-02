"""Shared test fixtures: in-memory Redis fake + QStash publisher spy.

All tests run against fakes — no real Telegram / Redis / QStash traffic.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))


def pytest_collection_modifyitems(items):
    """Mark the superseded delayed-first timer assertion as expected failure.

    The old regression test explicitly requires the first timer delivery to
    wait one full interval.  Product behavior intentionally changed: the first
    cycle is now immediate, with dedicated coverage in
    ``test_timer_immediate_first.py``.  Keep the old test visible during the
    transition instead of silently deselecting it.
    """
    obsolete = (
        "tests/test_broadcast_engine.py::"
        "test_timer_task_schedules_exactly_one_initial_delivery"
    )
    for item in items:
        if item.nodeid.endswith(obsolete):
            item.add_marker(pytest.mark.xfail(
                reason="superseded: timer repeat 0 now starts immediately",
                strict=False,
            ))


class FakeRedis:
    """Minimal in-memory stand-in for the Upstash REST client surface used
    by storage/redis_client and services/broadcast_runner."""

    def __init__(self):
        self.store = {}
        self.sets = {}
        # When True, every method raises (simulates a temporary Redis outage).
        self.fail_mode = False

    def _check(self):
        if self.fail_mode:
            raise ConnectionError("simulated redis outage")

    def get(self, key):
        self._check()
        return self.store.get(key)

    def set(self, key, value, nx=False, ex=None):
        self._check()
        if nx and (key in self.store):
            return None
        self.store[key] = value
        return "OK"

    def delete(self, *keys):
        self._check()
        for k in keys:
            self.store.pop(k, None)

    def sadd(self, key, *values):
        self._check()
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.update(str(v) for v in values)
        return len(s) - before

    def smembers(self, key):
        self._check()
        return list(self.sets.get(key, set()))

    def srem(self, key, *values):
        self._check()
        s = self.sets.get(key, set())
        for v in values:
            s.discard(str(v))

    def eval(self, script, numkeys, *args):
        # Only the compare-and-delete release script is used; emulate it.
        self._check()
        key, token = args[0], args[1]
        if self.store.get(key) == token:
            self.store.pop(key, None)
            return 1
        return 0


@pytest.fixture()
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr("storage.redis_client._redis", r)
    yield r
    monkeypatch.setattr("storage.redis_client._redis", None)


class SpyPublisher:
    """Records publish_delivery calls instead of touching QStash."""

    def __init__(self):
        self.calls = []

    def __call__(self, body, *, delay_seconds, dedup_id=None):
        self.calls.append({
            "task_id": body.get("task_id"),
            "repeat_no": body.get("repeat_no"),
            "batch_no": body.get("batch_no"),
            "delay_seconds": delay_seconds,
            "dedup_id": dedup_id,
        })

    @property
    def batches(self):
        return [(c["task_id"], c["repeat_no"], c["batch_no"]) for c in self.calls]


@pytest.fixture()
def spy_publisher(monkeypatch):
    spy = SpyPublisher()
    monkeypatch.setattr("services.broadcast_runner.publish_delivery", spy)
    return spy
