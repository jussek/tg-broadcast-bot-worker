"""Timer scheduling behavior.

A timer sends its first cycle immediately, then subsequent cycles follow the
configured interval.  ``total_repeats`` remains the TOTAL number of sends:
for example, 5 means one immediate send plus four interval-based sends.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def apply_timer_behavior(br) -> None:
    """Make timer repeat 0 due immediately and keep later repeats interval-based."""
    if getattr(br, "_TIMER_IMMEDIATE_FIRST_SEND_APPLIED", False):
        return

    def create_timer_task(
        user_id: int,
        message: str,
        groups: List[str],
        interval_minutes: int,
        total_repeats: Optional[int],
    ) -> Dict[str, Any]:
        """Persist a timer whose first send starts immediately.

        Repeat 0 is scheduled for ``now`` (QStash receives a ~1 second delay).
        After repeat 0 is committed, the existing deterministic scheduler uses
        ``first_run_at + interval`` for repeat 1, then +2 intervals, etc.

        ``total_repeats`` includes the immediate send.  Thus a value of 5 means
        exactly 5 total cycles: now, +1 interval, +2, +3 and +4 intervals.
        """
        task_id = str(br.uuid.uuid4())
        now = br.time.time()
        interval_seconds = max(1, int(interval_minutes) * 60)
        due_at = now

        task = br.normalize_task({
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

        br.save_task(task)
        br.redis_client.get_redis().sadd(f"broadcast:user:{user_id}:tasks", task_id)
        try:
            br.schedule_first_batch(task, due_at)
        except Exception:
            task["status"] = "error"
            task["last_error"] = "QStash scheduling failed at creation"
            br.save_task(task)
            raise

        br.logger.info(
            "event=task_created_immediate_first task_id=%s user_id=%s groups=%s "
            "interval_s=%s total=%s due_at=%.0f",
            task_id,
            user_id,
            len(task["groups"]),
            interval_seconds,
            task["total_repeats"],
            due_at,
        )
        return task

    br._legacy_create_timer_task = br.create_timer_task
    br.create_timer_task = create_timer_task
    br._TIMER_IMMEDIATE_FIRST_SEND_APPLIED = True
