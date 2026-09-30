"""Services module — unified broadcast delivery engine.

Single source of truth for scheduling/executing broadcasts:
  * services/broadcast_runner.py  — QStash planning, runs, batches, idempotency
  * services/telegram_delivery.py — Telethon client lifecycle

The previous duplicates (services/scheduler_service.py,
services/telegram_service.py, bot/dispatcher.py) were dead legacy code with a
second, unsafe scheduler implementation and have been removed after verifying
that no production or test code referenced them.
"""
from .broadcast_runner import (
    MissingEnvError,
    SessionNotAuthorizedError,
    create_timer_task,
    create_immediate_run,
    handle_delivery,
    normalize_task,
    schedule_batch,
)

__all__ = [
    "MissingEnvError",
    "SessionNotAuthorizedError",
    "create_timer_task",
    "create_immediate_run",
    "handle_delivery",
    "normalize_task",
    "schedule_batch",
]
