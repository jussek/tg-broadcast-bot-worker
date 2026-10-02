"""Production hardening for Telethon delivery and batch sequencing.

This module is intentionally small and applied from ``services.__init__`` so
existing imports keep working while the fixes remain isolated and regression
-testable.

Fixes:
* Resolve an InputPeer before sending.  ``StringSession`` carries Telegram
  authorization but a fresh serverless process may not have the channel
  access_hash/entity cache.  On a cache miss we encounter the account dialogs
  once, then resolve again and send to the resolved InputPeer.
* All batches of one repeat share the same scheduled_at.  The repeat interval
  separates repeats, not batches.  Later batches are chained by QStash with a
  short delay and are never delayed by another full repeat interval.
* A later batch cannot overtake an unfinished previous batch.
* Slow-mode waits are handled like FloodWait and rescheduled instead of being
  burned through the generic retry counter.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional


def apply_delivery_fixes(br) -> None:
    """Patch the broadcast runner once, preserving its public API."""
    if getattr(br, "_DELIVERY_FIXES_APPLIED", False):
        return

    legacy_classify_exception = br.classify_exception

    def classify_exception(exc: Exception):
        name = type(exc).__name__
        text = str(exc)
        if name in ("SlowModeWaitError", "FloodWaitError"):
            return "flood_wait", False
        if name == "ChatSendPlainForbiddenError" or "CHAT_SEND_PLAIN_FORBIDDEN" in text:
            return "no_write_permission", True
        return legacy_classify_exception(exc)

    async def resilient_default_sender(client, group_id: str, text: str) -> None:
        """Resolve the peer reliably in stateless/serverless Telethon clients.

        A StringSession is sufficient to authorize the account, but a new
        process can start without the entity/access_hash cache required for
        channels and supergroups.  We only enumerate dialogs after an actual
        cache miss, so the common path remains fast.
        """
        gid = int(group_id)
        target = gid
        get_input_entity = getattr(client, "get_input_entity", None)

        if get_input_entity is not None:
            try:
                target = await asyncio.wait_for(
                    get_input_entity(gid), timeout=br.TELETHON_OP_TIMEOUT
                )
            except (ValueError, TypeError):
                get_dialogs = getattr(client, "get_dialogs", None)
                if get_dialogs is None:
                    raise
                # Encounter dialogs once to hydrate Telethon's in-memory entity
                # cache (id + account-bound access_hash), then resolve again.
                await asyncio.wait_for(
                    get_dialogs(limit=None), timeout=max(br.TELETHON_OP_TIMEOUT, 25.0)
                )
                target = await asyncio.wait_for(
                    get_input_entity(gid), timeout=br.TELETHON_OP_TIMEOUT
                )

        await asyncio.wait_for(
            client.send_message(target, text, parse_mode="html"),
            timeout=br.TELETHON_OP_TIMEOUT,
        )

    def _previous_batch_finished(run: Dict[str, Any], batch_no: int) -> bool:
        if batch_no <= 0:
            return True
        prev_no = batch_no - 1
        previous = run["batches"][prev_no]["groups"]
        for gid in previous:
            marker = br._marker_get(run["task_id"], run["repeat_no"], prev_no, gid) or {}
            if marker.get("state") not in ("sent", "failed"):
                return False
        return True

    async def handle_delivery(
        body: Dict[str, Any],
        *,
        headers: Optional[Dict[str, str]] = None,
        client_factory=None,
        notifier=None,
    ) -> Dict[str, Any]:
        """Process one QStash batch with corrected within-repeat sequencing."""
        headers = headers or {}
        delivery_terminal = False
        task_id = br._as_str(body.get("task_id") or "")
        if not task_id:
            raise ValueError("No task_id")

        raw_repeat = body.get("repeat_no", body.get("expected_repeat"))
        raw_batch = body.get("batch_no", 0)
        try:
            batch_no = int(raw_batch or 0)
        except (TypeError, ValueError):
            raise ValueError("Invalid batch_no")

        message_id = headers.get("message-id") or headers.get("Message-Id")
        if message_id:
            try:
                if br.redis_client.get_redis().get(br.message_dedupe_key(message_id)):
                    return {"ok": True, "status": "duplicate_ignored"}
            except Exception:
                br.logger.exception("Message-Id dedupe check failed; continuing")

        raw_value = br.redis_client.get_redis().get(br.task_key(task_id))
        if raw_value is None:
            delivery_terminal = True
            return {"ok": False, "error": "Task not found"}

        task_raw = br._loads(raw_value)
        if not task_raw:
            delivery_terminal = True
            return {"ok": False, "error": "Task not found"}
        task = br.normalize_task(task_raw)

        if raw_repeat is None:
            repeat_no = int(task.get("completed_repeats", 0))
        else:
            try:
                repeat_no = int(raw_repeat)
            except (TypeError, ValueError):
                raise ValueError("Invalid repeat_no")

        if task.get("status") not in br.ACTIVE_STATUSES:
            delivery_terminal = True
            return {"ok": True, "status": "inactive_or_missing"}
        if repeat_no < int(task.get("completed_repeats", 0)):
            delivery_terminal = True
            return {"ok": True, "status": "stale_delivery"}
        if task.get("total_repeats") is not None and repeat_no >= int(task["total_repeats"]):
            delivery_terminal = True
            return {"ok": True, "status": "stale_delivery"}

        run = br.ensure_run(task, repeat_no)
        if run is None:
            br._requeue_batch(task_id, repeat_no, batch_no, 5)
            delivery_terminal = True
            return {"ok": True, "status": "run_contended"}
        if batch_no < 0 or batch_no >= len(run["batches"]):
            delivery_terminal = True
            return {"ok": True, "status": "stale_delivery"}
        if run.get("status") == "completed":
            br.commit_repeat(task, run)
            delivery_terminal = True
            return {"ok": True, "status": "already_completed"}

        interval = int(task.get("interval_seconds") or task.get("interval_minutes", 1) * 60)

        # The repeat interval applies to REPEATS only.  Every batch in this
        # repeat becomes eligible at the repeat's scheduled_at and subsequent
        # batches are chained with the explicit 1-second QStash delay below.
        due = float(run.get("scheduled_at") or 0)
        now = time.time()
        if due > now + 1:
            br._requeue_batch(task_id, repeat_no, batch_no, due - now)
            delivery_terminal = True
            return {"ok": True, "status": "not_due_yet"}

        # Protect ordering against a forged/out-of-order QStash delivery.  The
        # normal path publishes batch N only after N-1 reached terminal marker
        # states, but this guard keeps retries/replays safe too.
        if not _previous_batch_finished(run, batch_no):
            br._requeue_batch(task_id, repeat_no, batch_no, 5)
            delivery_terminal = True
            return {"ok": True, "status": "previous_batch_incomplete"}

        lk = br.lease_key(task_id, repeat_no, batch_no)
        token = br.acquire_lease(lk, ttl=interval + 120)
        if token is None:
            return {"ok": True, "status": "already_processing"}

        try:
            if client_factory is None:
                client_factory = br.get_telethon_client

            if run.get("started_at") is None:
                run["started_at"] = time.time()
                run["status"] = "processing"
                br.save_run(run)

            summary = await br.execute_batch(task, run, batch_no, client_factory, br._default_sender)

            if summary.get("flood_wait") is not None:
                wait = min(int(summary["flood_wait"]) + 2, 24 * 3600)
                br._requeue_batch(
                    task_id,
                    repeat_no,
                    batch_no,
                    wait,
                    dedup_id=f"{task_id}:{repeat_no}:{batch_no}:fw:{time.time():.0f}",
                )
                delivery_terminal = True
                return {
                    "ok": True,
                    "status": "flood_wait_rescheduled",
                    "wait_seconds": wait,
                }

            if summary.get("cancelled"):
                delivery_terminal = True
                return {"ok": True, "status": "cancelled"}

            if summary.get("budget_exhausted") or summary.get("retryable_failed"):
                br._requeue_batch(
                    task_id, repeat_no, batch_no, br.RETRY_BACKOFF_SECONDS
                )
                delivery_terminal = True
                return {
                    "ok": True,
                    "status": "batch_incomplete_requeued",
                    "success": summary["success"],
                    "retryable": summary["retryable_failed"],
                }

            next_batch = batch_no + 1
            if next_batch < len(run["batches"]):
                br.save_run(run)
                br._requeue_batch(task_id, repeat_no, next_batch, 1)
                if notifier and summary.get("results"):
                    await notifier(task, summary)
                delivery_terminal = True
                return {
                    "ok": True,
                    "status": "batch_done",
                    "success": summary["success"],
                    "failed": summary["permanent_failed"],
                    "next_batch": next_batch,
                }

            task = br.commit_repeat(task, run)
            if notifier and summary.get("results"):
                await notifier(task, summary)
            delivery_terminal = True
            return {
                "ok": True,
                "status": "repeat_committed",
                "completed_repeats": task["completed_repeats"],
                "task_status": task["status"],
                "success": summary["success"],
                "failed": summary["permanent_failed"],
            }
        finally:
            br.release_lease(lk, token)
            if message_id and delivery_terminal:
                try:
                    br.redis_client.get_redis().set(
                        br.message_dedupe_key(message_id), "1", ex=br.DEDUPE_TTL
                    )
                except Exception:
                    br.logger.exception("Failed to record Message-Id %s", message_id)

    br._legacy_handle_delivery = br.handle_delivery
    br._legacy_default_sender = br._default_sender
    br.classify_exception = classify_exception
    br._default_sender = resilient_default_sender
    br.handle_delivery = handle_delivery
    br._DELIVERY_FIXES_APPLIED = True
