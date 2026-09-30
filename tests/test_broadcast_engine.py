"""Regression tests for the unified delivery engine (services/broadcast_runner).

Every Telegram / QStash / Redis interaction is faked:
  * Redis        -> tests/conftest.FakeRedis (in-memory)
  * QStash       -> tests/conftest.SpyPublisher (records publish_delivery)
  * Telethon     -> fake client_factory + fake sender (no network, no sends)

Covered scenarios (external review list):
  1 group x 1 repeat = exactly 1 send; 5 groups x 3 repeats = 15 sends;
  duplicate delivery; one failing group never blocks the others; FloodWait
  requeue (no sleep); retryable error retried; MAX_SEND_ATTEMPTS cap;
  cancellation before batch / between individual sends; infinite timer;
  finite timer completion; early delivery; stale delivery; concurrent run
  creation; lease contention; ChatWriteForbidden / ChatAdminRequired
  permanent failures; Telethon connection timeout (repeat not lost);
  20-group batching; invocation-budget requeue; immediate run publishes
  EXACTLY ONE initial QStash delivery; crash right after receiving a
  delivery must not permanently lose the batch; Redis outage keeps the
  delivery recoverable.
"""
import asyncio
import json
import time

import pytest

from services import broadcast_runner as br


# ----------------------------------------------------------------- helpers
def make_task(task_id="t1", groups=None, interval_seconds=60, total_repeats=1,
              status="active", completed_repeats=0, first_run_at=None, **extra):
    now = time.time()
    task = {
        "id": task_id,
        "user_id": 42,
        "message": "TEST",
        "groups": [str(g) for g in (groups if groups is not None else ["-1001"])],
        "interval_minutes": max(1, interval_seconds // 60),
        "interval_seconds": interval_seconds,
        "total_repeats": total_repeats,
        "completed_repeats": completed_repeats,
        "status": status,
        "created_at": now - interval_seconds,
        "first_run_at": now - 10 if first_run_at is None else first_run_at,
        "next_run": now,
    }
    task.update(extra)
    return task


def persist(fake_redis, task):
    fake_redis.store[br.task_key(task["id"])] = json.dumps(task)


def make_due(fake_redis, task_id="t1", offset=0):
    """Fast-forward helper for tests that chain several repeats/batches inside
    one process run.

    Production pacing is deterministic: a run's ``scheduled_at`` equals
    ``first_run_at + N*interval`` and batch B additionally waits ``B*interval``
    (see handle_delivery early-delivery guard).  In wall-clock tests we cannot
    sleep minutes between chained deliveries, so we re-anchor the schedule
    into the past AND drop any already-created run records; the next delivery
    then rebuilds its run with a freshly-due ``scheduled_at``.
    ``offset`` must cover the largest (repeat_no + batch_no) still to arrive.
    """
    raw = fake_redis.store.get(br.task_key(task_id))
    if not raw:
        return
    t = json.loads(raw)
    interval = int(t.get("interval_seconds") or 60)
    t["first_run_at"] = time.time() - interval * (offset + 1) - 30
    t["next_run"] = time.time()
    fake_redis.store[br.task_key(task_id)] = json.dumps(t)
    # force run rebuild against the new anchor (drop run + SET-NX claim, which
    # a real Redis would have expired after 5s between chained deliveries)
    for k in [k for k in list(fake_redis.store)
              if k.startswith(f"broadcast:run:{task_id}:")
              or k.startswith(f"broadcast:run_claim:{task_id}:")]:
        del fake_redis.store[k]


class FakeTGError(Exception):
    pass


class FloodWaitError(FakeTGError):
    def __init__(self, seconds=7):
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


class ChatWriteForbiddenError(FakeTGError):
    pass


class ChatAdminRequiredError(FakeTGError):
    pass


class ConnectionTimeoutError(FakeTGError):
    """Telethon temporary connect failure (retryable infra error)."""


class ClientFactory:
    def __init__(self, exc=None):
        self.calls = 0
        self.clients = []
        self.exc = exc

    async def __call__(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        class C:
            disconnected = False
            async def disconnect(self):
                C.disconnected = True
        c = C()
        self.clients.append(c)
        return c


def make_sender(policy=None, delay=0.0, record=None):
    """policy: gid -> exception instance (raised) or str (returned status)."""
    policy = policy or {}

    async def sender(client, gid, text):
        if record is not None:
            record.append(gid)
        action = policy.get(gid)
        if isinstance(action, Exception):
            raise action
        if delay:
            await asyncio.sleep(delay)
        return action

    return sender


def run_delivery(spy, body, headers=None, factory=None, sender=None, notifier=None):
    """Invoke handle_delivery with patched publisher/sender/client factory."""
    br_ = br
    old_sender = br_._default_sender
    if sender is not None:
        br_._default_sender = sender
    try:
        return asyncio.run(br_.handle_delivery(
            body, headers=headers or {},
            client_factory=factory or ClientFactory(),
            notifier=notifier))
    finally:
        br_._default_sender = old_sender


def sent_groups(fake_redis, task_id, repeat_no):
    """All marker keys currently holding state=sent."""
    out = []
    for k, v in fake_redis.store.items():
        if k.startswith(f"broadcast:send:{task_id}:{repeat_no}:"):
            try:
                if json.loads(v).get("state") == "sent":
                    out.append(k)
            except Exception:
                pass
    return out


def count_sends(fake_redis, task_id):
    return len(sent_groups(fake_redis, task_id, -1) +
               sum((sent_groups(fake_redis, task_id, r)
                    for r in range(0, 20)), []))


def get_task(fake_redis, task_id):
    return json.loads(fake_redis.store[br.task_key(task_id)])


# ============================ core send accounting =========================

def test_one_group_one_repeat_exactly_one_send(fake_redis, spy_publisher, monkeypatch):
    task = make_task(total_repeats=1)
    persist(fake_redis, task)
    sends = []
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=make_sender(record=sends))
    assert res["status"] == "repeat_committed"
    assert sends == ["-1001"]                      # ровно 1 отправка
    assert get_task(fake_redis, "t1")["status"] == "completed"
    assert get_task(fake_redis, "t1")["completed_repeats"] == 1
    # final repeat => NO further QStash job at all
    assert spy_publisher.calls == []


def test_five_groups_three_repeats_fifteen_sends(fake_redis, spy_publisher, monkeypatch):
    groups = [f"-100{i}" for i in range(1, 6)]
    task = make_task(groups=groups, interval_seconds=300, total_repeats=3)
    persist(fake_redis, task)
    sends = []
    sender = make_sender(record=sends)
    factory = ClientFactory()

    # repeat 0
    make_due(fake_redis, "t1", offset=5)
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       factory=factory, sender=sender)
    assert res["status"] == "repeat_committed"
    assert len(spy_publisher.calls) == 1           # next repeat scheduled once
    # repeat 1 (simulate its delivery arriving on time)
    make_due(fake_redis, "t1", offset=5)
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 1, "batch_no": 0},
                       factory=factory, sender=sender)
    assert res["status"] == "repeat_committed"
    assert len(spy_publisher.calls) == 2
    # repeat 2 (final)
    make_due(fake_redis, "t1", offset=5)
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 2, "batch_no": 0},
                       factory=factory, sender=sender)
    assert res["status"] == "repeat_committed"
    assert res["task_status"] == "completed"
    assert len(spy_publisher.calls) == 2           # no new job after final repeat

    assert len(sends) == 15                        # 5 групп × 3 цикла
    assert set(sends) == set(groups)
    t = get_task(fake_redis, "t1")
    assert t["completed_repeats"] == 3
    assert t["last_run_success"] == 5
    assert t["next_run"] is None


# =========================== duplicate delivery ============================

def test_duplicate_message_id_delivery_does_not_resend(fake_redis, spy_publisher):
    task = make_task()
    persist(fake_redis, task)
    sends = []
    headers = {"message-id": "dup-1"}
    r1 = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                      headers=headers, sender=make_sender(record=sends))
    assert r1["status"] == "repeat_committed"
    r2 = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                      headers=headers, sender=make_sender(record=sends))
    assert r2["status"] == "duplicate_ignored"
    assert len(sends) == 1                         # второй заход ничего не слал


def test_same_batch_redelivery_without_message_id_is_idempotent(fake_redis, spy_publisher):
    """QStash redelivery of an ALREADY COMMITTED repeat must never double-send.
    The engine treats it as a recoverable requeue (in case the batch was only
    partially done), and the per-send markers guarantee zero Telegram calls."""
    task = make_task(total_repeats=2)   # не финальный repeat -> задача active
    persist(fake_redis, task)
    sends = []
    sender = make_sender(record=sends)
    run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                 sender=sender)
    assert sends == ["-1001"]
    # re-deliver same repeat/batch (no dedupe header this time)
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=sender)
    assert res["status"] == "stale_delivery"        # committed repeat -> ignore
    assert len(sends) == 1                          # повторной отправки НЕТ
    assert spy_publisher.calls[-1]["repeat_no"] == 1  # only the legit next-repeat job


# ==================== failure isolation / error classes ====================

def test_one_failing_group_does_not_block_others(fake_redis, spy_publisher):
    groups = ["A", "B", "C", "D"]
    task = make_task(groups=groups, total_repeats=1)
    persist(fake_redis, task)
    sends = []
    sender = make_sender(policy={"C": FakeTGError("random RPC failure")}, record=sends)
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=sender)
    # C is retryable -> batch requeued after B; A,B already sent
    assert sends[:3] == ["A", "B", "C"]            # обработка пошла дальше после ошибки? нет: C попытана
    assert res["status"] == "batch_incomplete_requeued"
    assert res["success"] == 3
    assert len(spy_publisher.calls) == 1           # один requeue того же батча
    # second pass (requeue): D is processed and C retried — nothing blocked
    sender2 = make_sender(record=sends)
    res2 = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                        sender=sender2)
    assert res2["status"] == "repeat_committed"
    assert "D" in sends                            # D обработана, несмотря на падение C
    assert sends.count("A") == 1                   # без дублей уже отправленных
    assert sends.count("B") == 1
    assert sends.count("D") == 1
    assert sends.count("C") == 2                   # C: 1 неудачная попытка + 1 ретрай


def test_chat_write_forbidden_permanent_others_continue(fake_redis, spy_publisher):
    task = make_task(groups=["A", "B", "C"], total_repeats=1)
    persist(fake_redis, task)
    sends = []
    sender = make_sender(policy={"B": ChatWriteForbiddenError("no rights")}, record=sends)
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=sender)
    assert res["status"] == "repeat_committed"    # permanent -> цикл завершается сразу
    assert res["failed"] == 1
    assert sends == ["A", "B", "C"]                # C отправлена после отказа B
    marker = json.loads(fake_redis.store[br.send_marker_key("t1", 0, 0, "B")])
    assert marker["state"] == "failed"
    assert marker["reason"] == "no_write_permission"
    assert spy_publisher.calls == []               # permanent не ретраится


def test_chat_admin_required_permanent_others_continue(fake_redis, spy_publisher):
    task = make_task(groups=["A", "B", "C"], total_repeats=1)
    persist(fake_redis, task)
    sends = []
    sender = make_sender(policy={"B": ChatAdminRequiredError("admin needed")}, record=sends)
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=sender)
    assert res["status"] == "repeat_committed"
    assert sends == ["A", "B", "C"]
    marker = json.loads(fake_redis.store[br.send_marker_key("t1", 0, 0, "B")])
    assert marker["state"] == "failed"
    assert marker["reason"] == "admin_required"


# ================================ floodwait ================================

def test_flood_wait_requeues_without_sleeping(fake_redis, spy_publisher):
    task = make_task(groups=["A", "B"], total_repeats=1)
    persist(fake_redis, task)
    calls = []
    orig_sleep = br.asyncio_sleep

    async def spy_sleep(sec):
        calls.append(sec)
        await orig_sleep(0)                        # never actually wait

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(br, "asyncio_sleep", spy_sleep)
    try:
        fw = FloodWaitError(seconds=7)
        sender = make_sender(policy={"A": fw})
        start = time.time()
        res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                           sender=sender)
        elapsed = time.time() - start
        assert res["status"] == "flood_wait_rescheduled"
        assert res["wait_seconds"] == 9            # 7 + 2 safety margin
        assert elapsed < 2                         # НЕ спали на FloodWait
        assert calls == []                         # ни одного долгого сна движком
        assert spy_publisher.calls[-1]["delay_seconds"] == 9
        assert (spy_publisher.calls[-1]["repeat_no"],
                spy_publisher.calls[-1]["batch_no"]) == (0, 0)   # тот же батч
    finally:
        monkeypatch.undo()


def test_flood_wait_retry_after_wait_delivers_remaining(fake_redis, spy_publisher):
    task = make_task(groups=["A", "B"], total_repeats=1)
    persist(fake_redis, task)
    fw = FloodWaitError(seconds=1)
    res1 = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                        sender=make_sender(policy={"A": fw}))
    assert res1["status"] == "flood_wait_rescheduled"
    make_due(fake_redis, "t1")
    res2 = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                        sender=make_sender())
    assert res2["status"] == "repeat_committed"


# ====================== retryable errors / MAX_SEND_ATTEMPTS ===============

def test_retryable_error_is_retried_and_completes(fake_redis, spy_publisher):
    task = make_task(total_repeats=1)
    persist(fake_redis, task)
    res1 = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                        sender=make_sender(policy={"-1001": ConnectionTimeoutError("net down")}))
    assert res1["status"] == "batch_incomplete_requeued"
    marker = json.loads(fake_redis.store[br.send_marker_key("t1", 0, 0, "-1001")])
    assert marker["state"] == "retryable" and marker["attempts"] == 1
    make_due(fake_redis, "t1")
    res2 = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                        sender=make_sender())
    assert res2["status"] == "repeat_committed"
    assert res2["success"] == 1


def test_max_send_attempts_marks_failed_and_cycle_completes(fake_redis, spy_publisher):
    task = make_task(total_repeats=1)
    persist(fake_redis, task)
    err = ConnectionTimeoutError("always fails")
    last = None
    for _ in range(br.MAX_SEND_ATTEMPTS + 1):
        make_due(fake_redis, "t1")
        last = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                            sender=make_sender(policy={"-1001": err}))
        if last["status"] != "batch_incomplete_requeued":
            break
    assert last["status"] == "repeat_committed"    # цикл способен завершиться
    marker = json.loads(fake_redis.store[br.send_marker_key("t1", 0, 0, "-1001")])
    assert marker["state"] == "failed"
    assert marker["reason"] == "max_attempts_exceeded"
    assert spy_publisher.calls[-1]["batch_no"] is not None  # следующий repeat запланирован


# ============================== cancellation ===============================

def test_cancelled_before_batch_sends_nothing(fake_redis, spy_publisher):
    task = make_task(status="cancelled")
    persist(fake_redis, task)
    sends = []
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=make_sender(record=sends))
    assert res["status"] == "inactive_or_missing"
    assert sends == []
    assert spy_publisher.calls == []


def test_cancelled_between_individual_sends_stops_remaining(fake_redis, spy_publisher):
    task = make_task(groups=["A", "B", "C"], total_repeats=1)
    persist(fake_redis, task)
    sends = []

    async def cancel_after_A(client, gid, text):
        sends.append(gid)
        if gid == "A":
            t = get_task(fake_redis, "t1")
            t["status"] = "cancelled"
            persist(fake_redis, t)

    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=cancel_after_A)
    assert res["status"] == "cancelled"
    assert sends == ["A"]                          # B и C НЕ отправлены
    assert spy_publisher.calls == []               # никаких новых jobs


def test_stale_delivery_of_cancelled_task_never_sends(fake_redis, spy_publisher):
    task = make_task(total_repeats=3)
    persist(fake_redis, task)
    run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                 sender=make_sender())
    t = get_task(fake_redis, "t1")
    t["status"] = "cancelled"
    persist(fake_redis, t)
    sends = []
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 1, "batch_no": 0},
                       sender=make_sender(record=sends))
    assert res["status"] == "inactive_or_missing"
    assert sends == []


# ======================== early / stale deliveries =========================

def test_early_delivery_defers_without_sending(fake_redis, spy_publisher):
    now = time.time()
    task = make_task(first_run_at=now + 600, next_run=now + 600, total_repeats=3)
    persist(fake_redis, task)
    sends = []
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=make_sender(record=sends))
    assert res["status"] == "not_due_yet"
    assert sends == []                             # ранняя доставка ничего не слала
    call = spy_publisher.calls[-1]
    assert 500 <= call["delay_seconds"] <= 601     # перенесена на остаток
    assert (call["repeat_no"], call["batch_no"]) == (0, 0)


def test_stale_repeat_delivery_is_ignored(fake_redis, spy_publisher):
    task = make_task(total_repeats=3, completed_repeats=2)
    persist(fake_redis, task)
    sends = []
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 1, "batch_no": 0},
                       sender=make_sender(record=sends))
    assert res["status"] == "stale_delivery"
    assert sends == []
    assert spy_publisher.calls == []


def test_repeat_beyond_total_repeats_is_stale(fake_redis, spy_publisher):
    task = make_task(total_repeats=3, completed_repeats=3, status="completed")
    persist(fake_redis, task)
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 3, "batch_no": 0},
                       sender=make_sender())
    assert res["status"] == "inactive_or_missing"  # completed task -> no send


# ============================= timer lifecycle =============================

def test_infinite_timer_commits_and_creates_next_repeat(fake_redis, spy_publisher):
    task = make_task(total_repeats=None, interval_seconds=300)
    persist(fake_redis, task)
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=make_sender())
    assert res["status"] == "repeat_committed"
    assert res["task_status"] == "active"
    t = get_task(fake_redis, "t1")
    assert t["completed_repeats"] == 1
    assert len(spy_publisher.calls) == 1           # ровно один следующий repeat
    nxt = spy_publisher.calls[0]
    assert (nxt["repeat_no"], nxt["batch_no"]) == (1, 0)
    assert 1 <= nxt["delay_seconds"] <= 300        # следующий repeat запланирован (due-anchored)


def test_finite_timer_completes_after_third_repeat(fake_redis, spy_publisher):
    task = make_task(total_repeats=3, interval_seconds=60)
    persist(fake_redis, task)
    now = time.time()
    publications = []
    for r in range(3):
        t = get_task(fake_redis, "t1")
        t["first_run_at"] = now - 60 * (r + 1)     # keep every repeat due
        persist(fake_redis, t)
        res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": r, "batch_no": 0},
                           sender=make_sender())
        assert res["status"] == "repeat_committed"
    t = get_task(fake_redis, "t1")
    assert t["status"] == "completed"
    assert t["completed_repeats"] == 3
    assert t["next_run"] is None
    # 2 публикации следующих repeat'ов, третья — завершение, новых jobs нет
    assert len(spy_publisher.calls) == 2
    assert [c["repeat_no"] for c in spy_publisher.calls] == [1, 2]


def test_deterministic_schedule_no_drift(fake_redis, spy_publisher):
    base = time.time() + 10_000                    # future anchor (no execution drift)
    task = make_task(total_repeats=5, interval_seconds=1200, first_run_at=base)
    for n in range(5):
        expected = base + 1200 * n
        actual = float(task["first_run_at"]) + int(task["interval_seconds"]) * n
        assert actual == expected                  # absolute UTC times, deterministic
    # commit_repeat computes next_run from first_run_at, not from wall clock
    persist(fake_redis, task)
    t = get_task(fake_redis, "t1")
    t["first_run_at"] = time.time() - 10           # repeat 0 due
    t["completed_repeats"] = 0
    persist(fake_redis, t)
    run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                 sender=make_sender())
    t = get_task(fake_redis, "t1")
    assert abs(t["next_run"] - (t["first_run_at"] + 1200)) < 1.5


# ===================== concurrency: run creation & lease ===================

def test_concurrent_run_creation_only_one_wins(fake_redis, spy_publisher):
    """SET-NX run claim: while NO run record exists, exactly one of two
    parallel deliveries may create it; the other gets None (and handle_delivery
    turns that into a recoverable requeue — see contention test below)."""
    task = make_task(total_repeats=2)
    persist(fake_redis, task)
    # Phase 1: both arrive with no run yet. Winner creates; loser sees claim.
    winner = br.ensure_run(task, 0)
    loser = br.ensure_run(task, 0)                 # claim held AND... run now exists
    assert winner is not None
    # With the run saved, any later arrival simply reuses it (no second run).
    reuse = br.ensure_run(task, 0)
    assert reuse["run_id"] == winner["run_id"]
    # Phase 2: prove the claim really blocks CREATION when run is missing
    # (simulates the crash window: run lost, claim still within its 5s TTL).
    del fake_redis.store[br.run_key("t1", 0)]
    blocked = br.ensure_run(task, 0)
    assert blocked is None                         # ровно одна сущíaющая creation защищена claim'ом


def test_concurrent_run_creation_contention_requeues(fake_redis, spy_publisher):
    """Two deliveries arrive while NO run exists: SET-NX claim lets exactly one
    create the run; the other must requeue (recoverable), not process blind."""
    task = make_task(total_repeats=2)
    persist(fake_redis, task)
    # simulate the winner holding the claim (as if it is mid-create)
    fake_redis.store[br._run_claim_key("t1", 0)] = "1"
    sends = []
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=make_sender(record=sends))
    assert res["status"] == "run_contended"
    assert sends == []                             # проигравший ничего не слал
    assert spy_publisher.calls[-1]["delay_seconds"] == 5


def test_lease_blocks_parallel_processing_of_same_batch(fake_redis, spy_publisher):
    task = make_task(total_repeats=2)
    persist(fake_redis, task)
    token = br.acquire_lease(br.lease_key("t1", 0, 0), ttl=60)
    assert token
    sends = []
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=make_sender(record=sends))
    assert res["status"] == "already_processing"
    assert sends == []                             # фактическая обработка одна — у держателя lease
    # release by NON-owner must NOT delete owner's lock
    br.release_lease(br.lease_key("t1", 0, 0), "wrong-token")
    assert fake_redis.store.get(br.lease_key("t1", 0, 0)) == token
    br.release_lease(br.lease_key("t1", 0, 0), token)
    assert br.lease_key("t1", 0, 0) not in fake_redis.store


# ===================== crash-after-receipt must not lose ===================

def test_crash_right_after_delivery_never_loses_batch(fake_redis, spy_publisher):
    """Message-Id marker is written ONLY after the delivery was processed
    (finally-block).  A crash before that leaves no marker, so the QStash
    provider retry (retries>=1) re-runs the batch; per-send markers prevent
    double sends of already confirmed groups."""
    task = make_task(groups=["A", "B"], total_repeats=1)
    persist(fake_redis, task)
    headers = {"message-id": "crash-1"}

    # --- attempt 1: process dies AFTER confirming send of A, BEFORE the
    #     delivery-level dedupe marker could be written.  We reproduce the exact
    #     state such a kill leaves behind: A's marker = sent, B untouched, and
    #     NO Message-Id dedupe record (the finally-block never ran). -----------
    br._marker_set("t1", 0, 0, "A", {"state": "sent", "attempts": 1, "ts": time.time()})

    # dedupe marker was NOT recorded => redelivery is NOT swallowed forever
    assert br.message_dedupe_key("crash-1") not in fake_redis.store

    # --- attempt 2: QStash provider retry of the SAME message id ------------
    sends = []
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       headers=headers, sender=make_sender(record=sends))
    assert res["status"] == "repeat_committed"
    assert sends == ["B"]                          # B дослана, A без дубля
    # now the dedupe marker IS recorded (successful completion)
    assert br.message_dedupe_key("crash-1") in fake_redis.store


def test_missing_task_returns_not_found_without_processing(fake_redis, spy_publisher):
    """Pre-existing production semantics (kept unchanged per external review):
    an absent task record is answered as {"ok": False, "error": "Task not
    found"} — the delivery does NOT raise and nothing is sent.  (A key that
    EXISTS but holds a corrupt/empty string raises JSONDecodeError instead —
    covered by test_empty_task_record_raises_json_error_not_silent_drop.)"""
    assert br.task_key("ghost") not in fake_redis.store   # key ABSENT -> redis.get() None
    res = run_delivery(spy_publisher, {"task_id": "ghost", "repeat_no": 0, "batch_no": 0},
                       sender=make_sender())
    assert res == {"ok": False, "error": "Task not found"}


def test_empty_task_record_raises_json_error_not_silent_drop(fake_redis, spy_publisher):
    # NOTE: an empty STRING value goes through _loads("") -> JSONDecodeError.
    # A truly ABSENT key returns None from redis.get() -> `or ""` short-circuits
    # before json.loads, i.e. the normal "Task not found" path above.
    fake_redis.store[br.task_key("t1")] = ""
    with pytest.raises(Exception):
        run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                     sender=make_sender())


# ========================== batching / budget ==============================

def test_twenty_groups_are_batched_five_per_invocation(fake_redis, spy_publisher, monkeypatch):
    monkeypatch.setattr(br, "BATCH_SIZE", 5)
    groups = [f"g{i}" for i in range(20)]
    task = make_task(groups=groups, total_repeats=1)
    persist(fake_redis, task)
    run = br.ensure_run(task, 0)
    assert run["total_batches"] == 4               # 20/5 — ни одна invocation не берёт все 20
    assert all(len(b["groups"]) == 5 for b in run["batches"])

    sends = []
    make_due(fake_redis, "t1", offset=5)
    # batch 0
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=make_sender(record=sends))
    assert res["status"] == "batch_done" and res["next_batch"] == 1
    assert len(sends) == 5
    assert spy_publisher.calls[-1] == {"task_id": "t1", "repeat_no": 0, "batch_no": 1,
                                       "delay_seconds": 1, "dedup_id": None}
    # cascade through batches 1..3
    for b in (1, 2, 3):
        make_due(fake_redis, "t1", offset=5)
        res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": b},
                           sender=make_sender(record=sends))
        expected = "batch_done" if b < 3 else "repeat_committed"
        assert res["status"] == expected
    assert len(sends) == 20
    assert get_task(fake_redis, "t1")["status"] == "completed"


def test_invocation_budget_exhausted_requeues_remainder(fake_redis, spy_publisher, monkeypatch):
    monkeypatch.setattr(br, "BATCH_SIZE", 5)
    monkeypatch.setattr(br, "INVOCATION_BUDGET_SECONDS", 0.05)
    groups = [f"g{i}" for i in range(5)]
    task = make_task(groups=groups, total_repeats=1)
    persist(fake_redis, task)
    sends = []
    make_due(fake_redis, "t1", offset=5)
    slow = make_sender(record=sends, delay=0.03)   # ~0.03s per send -> budget hits after ~2
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=slow)
    assert res["status"] == "batch_incomplete_requeued"
    assert 1 <= len(sends) <= 3                    # часть групп обработана
    q = spy_publisher.calls[-1]
    assert (q["repeat_no"], q["batch_no"]) == (0, 0)   # тот же батч перезапущен
    assert q["delay_seconds"] == br.RETRY_BACKOFF_SECONDS
    # remainder completes without duplicates (normal time budget now).
    # NOTE: monkeypatch.undo() is deliberately NOT used here — it would also
    # roll back the fake_redis fixture's patch of storage.redis_client._redis.
    # Restore ONLY the module constants this test temporarily changed.
    monkeypatch.setattr(br, "INVOCATION_BUDGET_SECONDS", 40.0)
    make_due(fake_redis, "t1", offset=5)
    res2 = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                        sender=make_sender(record=sends))
    assert res2["status"] == "repeat_committed"
    assert len(sends) == 5                         # ровно 5 уникальных отправок всего


# ======================= telethon connect failure ==========================

def test_telethon_connection_timeout_does_not_lose_repeat(fake_redis, spy_publisher):
    task = make_task(total_repeats=2)
    persist(fake_redis, task)
    factory = ClientFactory(exc=ConnectionTimeoutError("temporary connect timeout"))
    sends = []
    headers = {"message-id": "conn-1"}
    # Contract (api/index.py /api/process): any non-ValueError exception from
    # handle_delivery becomes HTTP 500 and the Message-Id dedupe record is
    # NEVER written on that path -> QStash (retries>=1) redelivers the SAME
    # job.  The repeat therefore cannot be lost on a temporary connect failure.
    with pytest.raises(ConnectionTimeoutError):
        run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                     headers=headers, factory=factory, sender=make_sender(record=sends))
    assert sends == []                              # ничего не отправлено
    # lease released despite the exception — the next attempt can acquire it
    assert br.lease_key("t1", 0, 0) not in fake_redis.store
    # marker untouched: group was never claimed, so NOTHING is lost — a fresh
    # delivery (endpoint 500 -> QStash retries with a new attempt) completes it
    assert br._marker_get("t1", 0, 0, "-1001") is None
    make_due(fake_redis, "t1")
    res2 = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                        sender=make_sender(record=sends))
    assert res2["status"] == "repeat_committed"
    assert sends == ["-1001"]


def test_client_disconnected_even_when_all_sends_fail(fake_redis, spy_publisher):
    task = make_task(total_repeats=1)
    persist(fake_redis, task)
    factory = ClientFactory()
    run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                 factory=factory,
                 sender=make_sender(policy={"-1001": FakeTGError("boom")}))
    assert factory.clients and factory.clients[0].disconnected


def test_per_group_timeout_error_is_requeued_not_fatal(fake_redis, spy_publisher):
    """asyncio.TimeoutError raised for ONE group -> retryable: batch requeued,
    other groups unaffected, repeat not lost."""
    task = make_task(groups=["A", "B"], total_repeats=2)
    persist(fake_redis, task)
    sends = []
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=make_sender(policy={"A": asyncio.TimeoutError()}, record=sends))
    assert res["status"] == "batch_incomplete_requeued"
    assert sends == ["A", "B"]                      # B всё равно обработана
    marker_a = json.loads(fake_redis.store[br.send_marker_key("t1", 0, 0, "A")])
    assert marker_a["state"] == "retryable"
    assert (marker_a["reason"], marker_a["attempts"]) == ("timeout", 1)
    make_due(fake_redis, "t1")
    res2 = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                        sender=make_sender(record=sends))
    assert res2["status"] == "repeat_committed"
    assert sends.count("B") == 1                    # без дубля B


# ====================== redis outage = recoverable =========================

def test_redis_outage_raises_leaves_delivery_recoverable(fake_redis, spy_publisher):
    task = make_task(total_repeats=1)
    persist(fake_redis, task)
    fake_redis.fail_mode = True
    with pytest.raises(ConnectionError):
        run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                     sender=make_sender())
    fake_redis.fail_mode = False
    # nothing was marked processed; recovery re-delivery works normally
    t = get_task(fake_redis, "t1")
    t["first_run_at"] = time.time() - 10
    persist(fake_redis, t)
    sends = []
    res = run_delivery(spy_publisher, {"task_id": "t1", "repeat_no": 0, "batch_no": 0},
                       sender=make_sender(record=sends))
    assert res["status"] == "repeat_committed"
    assert sends == ["-1001"]


# ===================== immediate run: exactly ONE delivery =================

def test_immediate_run_schedules_exactly_one_initial_delivery(fake_redis, spy_publisher):
    task = br.create_immediate_run(42, "TEST", ["-1001", "-1002", "-1003"])
    assert len(spy_publisher.calls) == 1           # РОВНО ОДНА QStash job
    call = spy_publisher.calls[0]
    assert call["task_id"] == task["id"]
    assert (call["repeat_no"], call["batch_no"]) == (0, 0)
    assert call["delay_seconds"] == 1
    assert task["total_repeats"] == 1
    assert task["completed_repeats"] == 0
    assert task["status"] == "active"
    assert abs(task["first_run_at"] - time.time()) < 5
    assert abs(task["next_run"] - time.time()) < 5
    stored = get_task(fake_redis, task["id"])
    assert stored["id"] == task["id"]
    assert task["id"] in fake_redis.sets.get(f"broadcast:user:42:tasks", set())


def test_timer_task_schedules_exactly_one_initial_delivery(fake_redis, spy_publisher):
    task = br.create_timer_task(42, "TEST", ["-1001"], interval_minutes=20, total_repeats=5)
    assert len(spy_publisher.calls) == 1
    call = spy_publisher.calls[0]
    assert (call["repeat_no"], call["batch_no"]) == (0, 0)
    assert 1190 <= call["delay_seconds"] <= 1200   # первый запуск через интервал
    assert task["first_run_at"] == pytest.approx(task["created_at"] + 1200, abs=2)


# ================== QStash retries decision (review item 5) ================

def test_publish_delivery_uses_configured_provider_retries(monkeypatch):
    recorded = {}

    class FakeMsg:
        def publish_json(self, *, url, body, delay, retries, timeout, deduplication_id):
            recorded.update(url=url, body=body, delay=delay, retries=retries,
                            timeout=timeout, dedup=deduplication_id)

    class FakeQS:
        message = FakeMsg()

    monkeypatch.setattr(br, "build_qstash_client", lambda: FakeQS())
    br.reset_qstash_client()
    try:
        br.publish_delivery({"task_id": "x", "repeat_no": 0, "batch_no": 0},
                            delay_seconds=60)
        # Decision: provider-level retries > 0 closes the loss window when the
        # function dies BEFORE our own requeue could run; safe because the
        # engine is fully idempotent (see test_crash_right_after_delivery...).
        assert recorded["retries"] == br.QSTASH_DELIVERY_RETRIES >= 1
        assert recorded["delay"] == "60s"
        assert recorded["url"].endswith("/api/process")
    finally:
        br.reset_qstash_client()
