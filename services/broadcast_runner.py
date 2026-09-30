"""Unified broadcast delivery engine — the SINGLE source of truth for
creating, planning, executing, rescheduling and cancelling broadcasts.

Both flows share this engine and differ only in ``due_at``:

  * "Send now"      -> create_run(due_at=now)
  * Timer broadcast -> create_run(due_at=created_at + interval)

Serverless-safe execution model (Vercel maxDuration = 60s):

  QStash delivery {task_id, repeat_no, batch_no}
    -> ONE short invocation processes ONE small batch of groups
    -> progress is persisted after every single send (run state in Redis)
    -> when the last batch of a repeat finishes, the repeat is committed
       (completed_repeats += 1, deterministic next_run = scheduled_at + N*interval)
    -> the next repeat's first batch is published to QStash with a delay
    -> the invocation ends.  No asyncio.sleep between repeats, ever.

Idempotency ladder (outermost -> innermost):

  1. QStash Message-Id dedupe          (exact duplicate deliveries)
  2. Run lease (owner-token SET NX EX) concurrent deliveries of one batch
  3. Per-send marker                   broadcast:send:{task}:{repeat}:{batch}:{group}
     pending -> processing -> sent ; permanent failures recorded per group.
     A marker is NEVER set to ``sent`` before Telegram confirmed the send,
     so a crash mid-batch resumes without duplicates and without lost sends.

Error policy:
  * One group failing never stops the rest of the batch.
  * Retryable (network/timeout/RPC/unknown) -> attempt counter, requeued via
    QStash after a backoff, up to MAX_SEND_ATTEMPTS total attempts.
  * FloodWait -> the whole batch is requeued once after exc.seconds (+2s
    safety margin) using the SAME batch job (no storm of parallel jobs).
  * Permanent (ChatWriteForbidden / ChatAdminRequired / CHAT_SEND_PLAIN_FORBIDDEN
    / PeerIdInvalid / ChannelPrivate / bad id) -> recorded, never retried.

Cancelled tasks: status is re-read from Redis before every batch AND before
every individual send — a cancelled task never sends another message.
"""

import json
import logging
import math
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from qstash import QStash

from storage import redis_client
from storage.models import _loads, get_task, save_task

logger = logging.getLogger("broadcast.runner")

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

#: Groups per QStash delivery.  ~5s Telethon connect + <=5*(send+0.3s pacing)
#: keeps every invocation far below the 60s Vercel limit even on slow networks.
BATCH_SIZE = int(os.getenv("BROADCAST_BATCH_SIZE", "5"))

#: Hard wall-clock budget for one invocation.  When exceeded we stop taking
#: new sends, persist progress and requeue the remainder — never risk a
#: Runtime Timeout error that would force a full-batch retry.
INVOCATION_BUDGET_SECONDS = float(os.getenv("BROADCAST_INVOCATION_BUDGET", "40"))

#: Per-attempt cap for a single Telethon operation (connect / send).
TELETHON_OP_TIMEOUT = float(os.getenv("BROADCAST_TELETHON_TIMEOUT", "20"))

MAX_SEND_ATTEMPTS = int(os.getenv("BROADCAST_MAX_SEND_ATTEMPTS", "4"))
RETRY_BACKOFF_SECONDS = 30

ACTIVE_STATUSES = ("active", "pending")

RUN_TTL = 30 * 24 * 3600           # run/marker retention: 30 days
DEDUPE_TTL = 7 * 24 * 3600         # message-id dedupe retention: 7 days


class MissingEnvError(RuntimeError):
    """A required environment variable is not configured (retryable)."""


class SessionNotAuthorizedError(RuntimeError):
    """Telethon session is unusable (non-retryable at runtime)."""


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def _as_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def normalize_delay(value) -> str:
    """Convert seconds (int/float/str) into a QStash duration string."""
    seconds = max(1, math.ceil(float(value)))
    return f"{seconds}s"


def format_duration(seconds) -> str:
    """Human-readable duration for keyboard labels (e.g. '2h 30m')."""
    try:
        total = int(max(1, round(float(seconds))))
    except (TypeError, ValueError):
        return "?"
    if total < 60:
        return f"{total} сек."
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} мин." + (f" {secs:02d} сек." if secs and minutes < 15 else "")
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч. {minutes:02d} мин."
    days, hours = divmod(hours, 24)
    return f"{days} дн. {hours:02d} ч."


# --------------------------------------------------------------------------
# Redis keys
# --------------------------------------------------------------------------

def task_key(task_id: str) -> str:
    return f"broadcast:task:{task_id}"


def run_key(task_id: str, repeat_no: int) -> str:
    return f"broadcast:run:{task_id}:{repeat_no}"


def send_marker_key(task_id: str, repeat_no: int, batch_no: int, group_id: str) -> str:
    return f"broadcast:send:{task_id}:{repeat_no}:{batch_no}:{group_id}"


def message_dedupe_key(message_id: str) -> str:
    return f"broadcast:qstash_message:{message_id}"


def lease_key(task_id: str, repeat_no: int, batch_no: int) -> str:
    return f"broadcast:lease:{task_id}:{repeat_no}:{batch_no}"


# --------------------------------------------------------------------------
# Lock primitives — owner token + compare-and-delete (never delete a foreign
# lock whose TTL already expired and was taken over by someone else).
# --------------------------------------------------------------------------

_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def acquire_lease(key: str, ttl: int) -> Optional[str]:
    """SET NX EX with a unique token; returns the token when acquired."""
    token = uuid.uuid4().hex
    try:
        ok = redis_client.get_redis().set(key, token, nx=True, ex=max(5, int(ttl)))
    except Exception:
        logger.exception("Redis unavailable while acquiring lease %s", key)
        raise
    return token if ok else None


def release_lease(key: str, token: str) -> None:
    """Release only if we still own the lock (Lua CAS; REST fallback)."""
    r = redis_client.get_redis()
    try:
        r.eval(_RELEASE_SCRIPT, 1, key, token)
        return
    except Exception:
        logger.debug("Lua EVAL unsupported by endpoint; falling back to GET/DEL")
    try:
        current = r.get(key)
        if current is not None and _as_str(current) == token:
            r.delete(key)
    except Exception:
        logger.exception("Failed to release lease %s", key)


# --------------------------------------------------------------------------
# QStash publisher (lazy, re-reads env so tests/rotation work)
# --------------------------------------------------------------------------

_qstash_client: Optional[QStash] = None


def reset_qstash_client() -> None:
    global _qstash_client
    _qstash_client = None


def build_qstash_client():
    """Create a QStash client from the current environment (re-read every
    call so tests can monkeypatch and credential rotations take effect)."""
    token = os.getenv("QSTASH_TOKEN")
    if not token:
        raise MissingEnvError("QSTASH_TOKEN is not configured")
    return QStash(token=token)


def get_qstash() -> QStash:
    global _qstash_client
    if _qstash_client is None:
        _qstash_client = build_qstash_client()
    return _qstash_client


def app_url() -> str:
    return (os.getenv("APP_URL") or "https://tg-broadcast-bot-worker.vercel.app").rstrip("/")


def process_endpoint() -> str:
    return f"{app_url()}/api/process"


def publish_delivery(body: Dict[str, Any], *, delay_seconds: float,
                     dedup_id: Optional[str] = None) -> None:
    """Publish one /api/process delivery with an absolute delay.

    ``retries=0`` deliberately: our own run-state + markers make redelivery
    safe, but a provider-level immediate retry storm adds nothing — the timer
    cadence itself is driven by explicit republishing.
    """
    get_qstash().message.publish_json(
        url=process_endpoint(),
        body=body,
        delay=normalize_delay(delay_seconds),
        retries=0,
        timeout="30s",
        deduplication_id=dedup_id,
    )
    logger.info(
        "event=qstash_publish task_id=%s repeat_no=%s batch_no=%s delay_s=%s",
        body.get("task_id"), body.get("repeat_no"), body.get("batch_no"),
        max(1, math.ceil(delay_seconds)),
    )


# Back-compatible wrappers used by task creation / rescheduling.
def schedule_batch(task_id: str, repeat_no: int, batch_no: int, delay_seconds: float) -> None:
    publish_delivery(
        {"task_id": task_id, "repeat_no": int(repeat_no), "batch_no": int(batch_no)},
        delay_seconds=delay_seconds,
    )


def schedule_first_batch(task: Dict[str, Any], due_at: float) -> None:
    schedule_batch(task["id"], 0, 0, max(1.0, due_at - time.time()))





# --------------------------------------------------------------------------
# Task schema — normalization gives OLD Redis records all new fields so
# existing active timers keep working after deployment (backward compat).
# --------------------------------------------------------------------------

def normalize_task(task: Dict[str, Any]) -> Dict[str, Any]:
    task.setdefault("id", "")
    task.setdefault("user_id", 0)
    task.setdefault("message", "")
    task["groups"] = list(dict.fromkeys(_as_str(g) for g in task.get("groups") or []))
    interval_minutes = int(task.get("interval_minutes") or 1)
    task["interval_minutes"] = interval_minutes
    # Canonical numeric field (older records only had interval_minutes).
    task["interval_seconds"] = int(task.get("interval_seconds") or interval_minutes * 60)
    total = task.get("total_repeats")
    task["total_repeats"] = None if total is None else int(total)
    task["completed_repeats"] = int(task.get("completed_repeats") or 0)
    task.setdefault("status", "active")
    task.setdefault("created_at", time.time())
    task.setdefault("first_run_at", None)
    task.setdefault("last_run_at", None)
    task.setdefault("last_run_success", 0)
    task.setdefault("last_run_failed", 0)
    task.setdefault("last_error", None)
    task.setdefault("next_run", float(task["created_at"]) + task["interval_seconds"])
    return task


# --------------------------------------------------------------------------
# Task creation (single entry point for timers)
# --------------------------------------------------------------------------

def create_timer_task(user_id: int, message: str, groups: List[str],
                      interval_minutes: int, total_repeats: Optional[int]) -> Dict[str, Any]:
    """Persist an active timer task and plan its first QStash delivery.

    First run happens ONE FULL INTERVAL after creation (the UI promises this).
    """
    task_id = str(uuid.uuid4())
    now = time.time()
    interval_seconds = max(1, int(interval_minutes) * 60)
    due_at = now + interval_seconds
    task = normalize_task({
        "id": task_id,
        "user_id": int(user_id),
        "message": message,
        "groups": [str(g) for g in dict.fromkeys(groups)],
        "interval_minutes": int(interval_minutes),
        "interval_seconds": interval_seconds,
        "total_repeats": None if total_repeats is None else int(total_repeats),
        "completed_repeats": 0,
        "status": "active",
        "created_at": now,
        "first_run_at": due_at,
        "next_run": due_at,
    })
    save_task(task)
    redis_client.get_redis().sadd(f"broadcast:user:{user_id}:tasks", task_id)
    try:
        schedule_first_batch(task, due_at)
    except Exception:
        task["status"] = "error"
        task["last_error"] = "QStash scheduling failed at creation"
        save_task(task)
        raise
    logger.info("event=task_created task_id=%s user_id=%s groups=%s interval_s=%s total=%s due_at=%.0f",
                task_id, user_id, len(task["groups"]), interval_seconds, task["total_repeats"], due_at)
    return task


def create_immediate_run(user_id: int, message: str, groups: List[str]) -> Dict[str, Any]:
    """"Send now" == a one-shot timer task with due_at = now (same engine)."""
    task = create_timer_task(user_id=user_id, message=message, groups=groups,
                             interval_minutes=1, total_repeats=1)
    # Re-plan the first batch for right now (interval semantics above are for
    # timers; an immediate run must not wait).
    schedule_batch(task["id"], 0, 0, 1)
    logger.info("event=immediate_run_created task_id=%s user_id=%s groups=%s",
                task["id"], user_id, len(task["groups"]))
    return task


# --------------------------------------------------------------------------
# Run state (progress of one full cycle over all groups)
# --------------------------------------------------------------------------

def chunk_groups(groups: List[str], size: int = BATCH_SIZE) -> List[List[str]]:
    size = max(1, int(size))
    return [groups[i:i + size] for i in range(0, len(groups), size)]


def get_run(task_id: str, repeat_no: int) -> Optional[Dict[str, Any]]:
    raw = redis_client.get_redis().get(run_key(task_id, repeat_no))
    return _loads(raw) if raw else None


def save_run(run: Dict[str, Any]) -> None:
    redis_client.get_redis().set(run_key(run["task_id"], run["repeat_no"]),
                                 _dumps(run), ex=RUN_TTL)


def create_run(task_id: str, repeat_no: int, scheduled_at: float,
               batches: List[List[str]]) -> Dict[str, Any]:
    run = {
        "task_id": task_id,
        "repeat_no": int(repeat_no),
        "run_id": uuid.uuid4().hex,
        "scheduled_at": float(scheduled_at),
        "started_at": None,
        "finished_at": None,
        "status": "pending",
        "total_batches": len(batches),
        "batches": [{"batch_no": i, "groups": chunk} for i, chunk in enumerate(batches)],
    }
    save_run(run)
    return run


def ensure_run(task: Dict[str, Any], repeat_no: int) -> Dict[str, Any]:
    """Return the run for this repeat, creating it deterministically.

    ``scheduled_at`` for repeat N is derived purely from created_at + N*interval
    so it is identical no matter which delivery (or replay) creates it.
    """
    run = get_run(task["id"], repeat_no)
    if run:
        return run
    interval = int(task.get("interval_seconds") or task.get("interval_minutes", 1) * 60)
    base = float(task.get("first_run_at") or task.get("created_at", time.time()))
    scheduled_at = base + interval * repeat_no
    batches = chunk_groups(list(task.get("groups") or []))
    logger.info("event=run_created task_id=%s repeat_no=%s scheduled_at=%.0f batches=%s",
                task["id"], repeat_no, scheduled_at, len(batches))
    return create_run(task["id"], repeat_no, scheduled_at, batches)


def _marker_get(task_id, repeat_no, batch_no, gid) -> Optional[str]:
    raw = redis_client.get_redis().get(send_marker_key(task_id, repeat_no, batch_no, gid))
    if raw is None:
        return None
    try:
        return _loads(raw)
    except Exception:
        logger.exception("Corrupted send marker for %s/%s/%s/%s", task_id, repeat_no, batch_no, gid)
        return None


def _marker_set(task_id, repeat_no, batch_no, gid, payload: Dict[str, Any]) -> None:
    redis_client.get_redis().set(send_marker_key(task_id, repeat_no, batch_no, gid),
                                 _dumps(payload), ex=RUN_TTL)


# --------------------------------------------------------------------------
# Batch execution
# --------------------------------------------------------------------------

def classify_exception(exc: Exception) -> Tuple[str, bool]:
    """Map a Telethon exception to (reason, is_permanent)."""
    name = type(exc).__name__
    text = str(exc)
    if name == "FloodWaitError":
        return "flood_wait", False
    if name in ("ChatWriteForbiddenError", "UserBannedInChannelError"):
        return "no_write_permission", True
    if name in ("ChatAdminRequiredError", "SenderAdminRequiredError",
                "SenderBotInvalidError", "ChannelsAdminPublicForbiddenError"):
        return "admin_required", True
    if name in ("ChannelPrivateError", "ChannelInvalidError"):
        return "inaccessible", True
    if name == "PeerIdInvalidError":
        return "inaccessible", True
    if name in ("ValueError", "TypeError"):
        return "invalid_group_id", True
    if name in ("ChatSendPlainForbidden",) or "CHAT_SEND_PLAIN_FORBIDDEN" in text:
        return "no_write_permission", True
    if name in ("TimedOutError", "ConnectionError", "OSError") or isinstance(exc, TimeoutError):
        return "timeout", False
    if name.endswith("Error") and "RPC" in name:
        return "telegram_error", False
    return "telegram_error", False


async def execute_batch(task: Dict[str, Any], run: Dict[str, Any], batch_no: int,
                        client_factory, sender) -> Dict[str, Any]:
    """Send the batch's not-yet-sent groups.  Returns a summary dict.

    ``client_factory()`` -> connected authorized Telethon client (async).
    ``sender(client, group_id, text)`` -> awaited send (raises on failure).

    Never raises for per-group errors; raises only for infrastructure errors
    (connection failure, FloodWait reschedule requested, budget exhausted),
    which callers translate into requeue decisions.
    """
    task_id = task["id"]
    repeat_no = int(run["repeat_no"])
    chunk = run["batches"][batch_no]["groups"]
    started = time.time()
    remaining = [gid for gid in chunk
                 if (_marker_get(task_id, repeat_no, batch_no, gid) or {}).get("state") != "sent"]

    summary = {"success": 0, "permanent_failed": 0, "retryable_failed": 0,
               "flood_wait": None, "budget_exhausted": False, "results": []}
    if not remaining:
        return summary

    client = await client_factory()
    try:
        for gid in remaining:
            # Fresh status check before EVERY send: a cancelled/completed task
            # must not deliver another message even mid-batch.
            fresh = _loads(redis_client.get_redis().get(task_key(task_id)) or "")
            if not fresh or fresh.get("status") not in ACTIVE_STATUSES:
                summary["cancelled"] = True
                break
            marker = _marker_get(task_id, repeat_no, batch_no, gid) or {"state": "pending", "attempts": 0}
            attempts = int(marker.get("attempts", 0)) + 1
            if marker.get("state") == "processing":
                # A previous invocation died between claim and confirm; the
                # attempt counter guards against endless loops.
                pass
            if attempts > MAX_SEND_ATTEMPTS:
                _marker_set(task_id, repeat_no, batch_no, gid,
                            {"state": "failed", "reason": "max_attempts_exceeded",
                             "attempts": attempts, "ts": time.time()})
                summary["permanent_failed"] += 1
                summary["results"].append({"group_id": gid, "status": "max_attempts_exceeded"})
                continue
            # Claim: pending/processing -> processing (NOT sent yet!).
            _marker_set(task_id, repeat_no, batch_no, gid,
                        {"state": "processing", "attempts": attempts, "ts": time.time()})
            t0 = time.time()
            try:
                await sender(client, gid, task.get("message", ""))
            except Exception as exc:  # noqa: BLE001 — classified below, never swallowed silently
                reason, permanent = classify_exception(exc)
                duration_ms = int((time.time() - t0) * 1000)
                if reason == "flood_wait":
                    wait_seconds = int(getattr(exc, "seconds", 60) or 60)
                    summary["flood_wait"] = wait_seconds
                    # Leave the marker in `processing`: the retry will pick it
                    # up again after the wait (attempt already counted).
                    logger.warning(
                        "event=flood_wait task_id=%s repeat_no=%s batch_no=%s group_id=%s wait_s=%s attempt=%s",
                        task_id, repeat_no, batch_no, gid, wait_seconds, attempts)
                    break  # session-wide flood: stop touching Telegram now
                if permanent:
                    _marker_set(task_id, repeat_no, batch_no, gid,
                                {"state": "failed", "reason": reason, "attempts": attempts,
                                 "ts": time.time()})
                    summary["permanent_failed"] += 1
                    logger.warning(
                        "event=send_permanent_failed task_id=%s repeat_no=%s batch_no=%s group_id=%s "
                        "reason=%s duration_ms=%s", task_id, repeat_no, batch_no, gid, reason, duration_ms)
                else:
                    _marker_set(task_id, repeat_no, batch_no, gid,
                                {"state": "retryable", "reason": reason, "attempts": attempts,
                                 "ts": time.time()})
                    summary["retryable_failed"] += 1
                    logger.warning(
                        "event=send_retryable_failed task_id=%s repeat_no=%s batch_no=%s group_id=%s "
                        "reason=%s attempt=%s duration_ms=%s",
                        task_id, repeat_no, batch_no, gid, reason, attempts, duration_ms)
                summary["results"].append({"group_id": gid, "status": reason})
                continue

            # Confirmed by Telegram — only NOW mark sent.
            _marker_set(task_id, repeat_no, batch_no, gid,
                        {"state": "sent", "attempts": attempts, "ts": time.time()})
            summary["success"] += 1
            summary["results"].append({"group_id": gid, "status": "sent"})
            logger.info(
                "event=send_ok task_id=%s run_id=%s repeat_no=%s batch_no=%s group_id=%s attempt=%s duration_ms=%s",
                task_id, run["run_id"], repeat_no, batch_no, gid, attempts,
                int((time.time() - t0) * 1000))
            if time.time() - started > INVOCATION_BUDGET_SECONDS:
                summary["budget_exhausted"] = True
                break
            await asyncio_sleep(0.3)
    finally:
        try:
            await sender_disconnect(client)
        except Exception:
            logger.exception("Telethon disconnect failed")
    return summary


async def sender_disconnect(client) -> None:
    disconnect = getattr(client, "disconnect", None)
    if disconnect is not None:
        await disconnect()


async def asyncio_sleep(seconds: float) -> None:
    """Short pacing sleep only (never used for timer intervals)."""
    import asyncio
    await asyncio.sleep(seconds)


# --------------------------------------------------------------------------
# Repeat commit + cascade
# --------------------------------------------------------------------------

def _count_batch_markers(task_id: str, run: Dict[str, Any], batch_no: int) -> Tuple[int, int]:
    sent = failed = 0
    for gid in run["batches"][batch_no]["groups"]:
        state = (_marker_get(task_id, run["repeat_no"], batch_no, gid) or {}).get("state")
        if state == "sent":
            sent += 1
        elif state == "failed":
            failed += 1
    return sent, failed


def _count_run_markers(task_id: str, run: Dict[str, Any]) -> Tuple[int, int]:
    sent = failed = 0
    for i in range(len(run["batches"])):
        s, f = _count_batch_markers(task_id, run, i)
        sent += s
        failed += f
    return sent, failed


def commit_repeat(task: Dict[str, Any], run: Dict[str, Any]) -> Dict[str, Any]:
    """Mark the repeat finished deterministically and plan the next one.

    Exactly-once guarantee: the task's completed_repeats may advance only to
    ``run.repeat_no + 1`` and only while the task is still active, so cascades
    can never double-increment or double-publish the next repeat.
    """
    task = normalize_task(task)
    task_id, repeat_no = task["id"], int(run["repeat_no"])
    sent, failed = _count_run_markers(task_id, run)

    if task.get("status") not in ACTIVE_STATUSES:
        return task  # cancelled while running — do not commit/reschedule

    if int(task.get("completed_repeats", 0)) >= repeat_no + 1:
        logger.info("event=commit_skipped task_id=%s repeat_no=%s (already committed)",
                    task_id, repeat_no)
        return task

    run["status"] = "completed"
    run["finished_at"] = time.time()
    save_run(run)

    task["completed_repeats"] = repeat_no + 1
    task["last_run_at"] = run["scheduled_at"]
    task["last_run_success"] = sent
    task["last_run_failed"] = failed
    task["last_error"] = None if failed == 0 else f"{failed} group(s) failed in repeat {repeat_no + 1}"

    total = task.get("total_repeats")
    if total is not None and task["completed_repeats"] >= int(total):
        task["status"] = "completed"
        task["next_run"] = None
        logger.info("event=task_completed task_id=%s repeats=%s", task_id, task["completed_repeats"])
    else:
        # Deterministic schedule: T, T+i, T+2i ... regardless of execution time.
        interval = int(task.get("interval_seconds") or task.get("interval_minutes", 1) * 60)
        base = float(task.get("first_run_at") or task.get("created_at", time.time()))
        next_run = base + interval * task["completed_repeats"]
        now = time.time()
        if next_run <= now:
            next_run = now + interval  # provider was very late; re-anchor
        task["status"] = "active"
        task["next_run"] = next_run
        schedule_batch(task_id, task["completed_repeats"], 0, max(1.0, next_run - now))
        logger.info("event=repeat_committed task_id=%s repeat_no=%s next_run=%.0f success=%s failed=%s",
                    task_id, repeat_no, next_run, sent, failed)
    save_task(task)
    return task


def _requeue_batch(task_id: str, repeat_no: int, batch_no: int, delay: float,
                   dedup_id: Optional[str] = None) -> None:
    publish_delivery({"task_id": task_id, "repeat_no": int(repeat_no), "batch_no": int(batch_no)},
                     delay_seconds=delay, dedup_id=dedup_id)


# --------------------------------------------------------------------------
# Top-level entry: handle one QStash delivery
# --------------------------------------------------------------------------

async def handle_delivery(body: Dict[str, Any], *, headers: Optional[Dict[str, str]] = None,
                          client_factory=None, notifier=None) -> Dict[str, Any]:
    """Process exactly one (task, repeat, batch) delivery.  Short-lived.

    Returns a JSON-friendly result dict; raising is reserved for genuinely
    retryable infrastructure problems (missing config), so QStash retries.
    """
    headers = headers or {}
    task_id = _as_str(body.get("task_id") or "")
    if not task_id:
        raise ValueError("No task_id")

    raw_repeat = body.get("repeat_no", body.get("expected_repeat"))
    raw_batch = body.get("batch_no", 0)
    try:
        batch_no = int(raw_batch or 0)
    except (TypeError, ValueError):
        raise ValueError("Invalid batch_no")

    # 1) exact-duplicate protection on the stable QStash Message-Id
    message_id = headers.get("message-id") or headers.get("Message-Id")
    if message_id:
        try:
            if redis_client.get_redis().get(message_dedupe_key(message_id)):
                return {"ok": True, "status": "duplicate_ignored"}
        except Exception:
            logger.exception("Message-Id dedupe check failed; continuing")

    task_raw = _loads(redis_client.get_redis().get(task_key(task_id)) or "")
    if not task_raw:
        return {"ok": False, "error": "Task not found"}
    task = normalize_task(task_raw)

    # 2) legacy payloads carry no repeat number: bind them to the current
    #    repeat instead of letting an old delivery invent a new one.
    if raw_repeat is None:
        repeat_no = int(task.get("completed_repeats", 0))
    else:
        try:
            repeat_no = int(raw_repeat)
        except (TypeError, ValueError):
            raise ValueError("Invalid repeat_no")

    if task.get("status") not in ACTIVE_STATUSES:
        return {"ok": True, "status": "inactive_or_missing"}
    if repeat_no < int(task.get("completed_repeats", 0)):
        return {"ok": True, "status": "stale_delivery"}
    if task.get("total_repeats") is not None and repeat_no >= int(task["total_repeats"]):
        return {"ok": True, "status": "stale_delivery"}

    run = ensure_run(task, repeat_no)
    if batch_no < 0 or batch_no >= len(run["batches"]):
        return {"ok": True, "status": "stale_delivery"}
    if run.get("status") == "completed":
        commit_repeat(task, run)
        return {"ok": True, "status": "already_completed"}

    interval = int(task.get("interval_seconds") or task.get("interval_minutes", 1) * 60)

    # 3) early delivery (clock skew / replay): defer, never send early.
    due = float(run.get("scheduled_at") or 0) + interval * batch_no
    now = time.time()
    if due > now + 1:
        _requeue_batch(task_id, repeat_no, batch_no, due - now)
        return {"ok": True, "status": "not_due_yet"}

    # 4) concurrency lease for this exact batch (retry/replay/manual overlap)
    lk = lease_key(task_id, repeat_no, batch_no)
    token = acquire_lease(lk, ttl=interval + 120)
    if token is None:
        return {"ok": True, "status": "already_processing"}

    try:
        if client_factory is None:
            from services.telegram_delivery import get_telethon_client as client_factory  # noqa

        if run.get("started_at") is None:
            run["started_at"] = time.time()
            run["status"] = "processing"
            save_run(run)

        summary = await execute_batch(task, run, batch_no, client_factory, _default_sender)

        if summary.get("flood_wait") is not None:
            wait = min(int(summary["flood_wait"]) + 2, 24 * 3600)
            _requeue_batch(task_id, repeat_no, batch_no, wait,
                           dedup_id=f"{task_id}:{repeat_no}:{batch_no}:fw:{time.time():.0f}")
            return {"ok": True, "status": "flood_wait_rescheduled", "wait_seconds": wait}

        if summary.get("cancelled"):
            return {"ok": True, "status": "cancelled"}

        if summary.get("budget_exhausted") or summary.get("retryable_failed"):
            # Not everything reached a final state — requeue this batch with a
            # backoff.  Already-sent groups are skipped via their markers, so
            # this neither duplicates nor loses sends.
            _requeue_batch(task_id, repeat_no, batch_no, RETRY_BACKOFF_SECONDS)
            return {"ok": True, "status": "batch_incomplete_requeued",
                    "success": summary["success"], "retryable": summary["retryable_failed"]}

        # Batch finished (all groups sent or permanently failed).
        next_batch = batch_no + 1
        if next_batch < len(run["batches"]):
            save_run(run)
            _requeue_batch(task_id, repeat_no, next_batch, 1)
            if notifier and summary.get("results"):
                await notifier(task, summary)
            return {"ok": True, "status": "batch_done", "success": summary["success"],
                    "failed": summary["permanent_failed"], "next_batch": next_batch}

        task = commit_repeat(task, run)
        if notifier and summary.get("results"):
            await notifier(task, summary)
        return {"ok": True, "status": "repeat_committed",
                "completed_repeats": task["completed_repeats"],
                "task_status": task["status"],
                "success": summary["success"], "failed": summary["permanent_failed"]}
    finally:
        release_lease(lk, token)
        if message_id:
            try:
                redis_client.get_redis().set(message_dedupe_key(message_id), "1", ex=DEDUPE_TTL)
            except Exception:
                logger.exception("Failed to record Message-Id %s", message_id)


async def _default_sender(client, group_id: str, text: str) -> None:
    """One Telethon send, bounded by asyncio.wait_for (Telethon 1.36 has no
    ``timeout=`` kwarg on connect/send — do NOT pass one)."""
    import asyncio
    await asyncio.wait_for(client.send_message(int(group_id), text, parse_mode="html"),
                           timeout=TELETHON_OP_TIMEOUT)
