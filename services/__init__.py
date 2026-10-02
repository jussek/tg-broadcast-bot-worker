"""Services module — unified broadcast delivery engine.

Single source of truth for scheduling/executing broadcasts:
  * services/broadcast_runner.py  — QStash planning, runs, batches, idempotency
  * services/telegram_delivery.py — per-user Telethon client lifecycle
  * services/delivery_fixes.py    — production hardening for entity resolution
                                    and within-repeat batch sequencing
  * services/timer_behavior.py    — timer starts immediately, then uses interval
"""
from . import broadcast_runner
from . import telegram_delivery
from .delivery_fixes import apply_delivery_fixes
from .timer_behavior import apply_timer_behavior

# Make the runner's injectable client hook user-aware before installing the
# delivery wrapper. Tests may still monkeypatch this hook with a zero-argument
# factory; delivery_fixes preserves that dependency-injection contract.
broadcast_runner.get_telethon_client = telegram_delivery.get_telethon_client

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
