"""Services module — unified broadcast delivery engine.

Single source of truth for scheduling/executing broadcasts:
  * services/broadcast_runner.py  — QStash planning, runs, batches, idempotency
  * services/telegram_delivery.py — Telethon client lifecycle
  * services/delivery_fixes.py    — production hardening for entity resolution
                                    and within-repeat batch sequencing
  * services/timer_behavior.py    — timer starts immediately, then uses interval

The previous duplicates (services/scheduler_service.py,
services/telegram_service.py, bot/dispatcher.py) were dead legacy code with a
second, unsafe scheduler implementation and have been removed after verifying
that no production or test code referenced them.
"""
from . import broadcast_runner
from .delivery_fixes import apply_delivery_fixes
from .timer_behavior import apply_timer_behavior

# Apply production hardening before callers import symbols directly from
# services.broadcast_runner.  The patches are idempotent and keep the module's
# public API stable for existing handlers/tests.
apply_delivery_fixes(broadcast_runner)
apply_timer_behavior(broadcast_runner)

MissingEnvError = broadcast_runner.MissingEnvError
SessionNotAuthorizedError = broadcast_runner.SessionNotAuthorizedError
create_timer_task = broadcast_runner.create_timer_task
create_immediate_run = broadcast_runner.create_immediate_run
handle_delivery = broadcast_runner.handle_delivery
normalize_task = broadcast_runner.normalize_task
schedule_batch = broadcast_runner.schedule_batch

__all__ = [
    "MissingEnvError",
    "SessionNotAuthorizedError",
    "create_timer_task",
    "create_immediate_run",
    "handle_delivery",
    "normalize_task",
    "schedule_batch",
]
