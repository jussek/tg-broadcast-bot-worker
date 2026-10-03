"""Pin each broadcast task to the Telegram account active at creation time."""

from services import broadcast_runner as br, telegram_delivery
from storage.telegram_accounts import get_active_account_id


def install_task_account_pinning(legacy) -> None:
    if getattr(legacy, "_TASK_ACCOUNT_PINNING_INSTALLED", False):
        return
    legacy._TASK_ACCOUNT_PINNING_INSTALLED = True

    previous_immediate = legacy.create_immediate_run
    previous_timer = legacy.create_timer_task
    previous_delivery = legacy.handle_delivery

    def _save_pin(task):
        owner_id = int(task.get("user_id") or 0)
        account_id = get_active_account_id(owner_id)
        if account_id is not None:
            task["telegram_account_id"] = int(account_id)
            legacy.save_task(task)
        return task

    def create_immediate_run(*args, **kwargs):
        return _save_pin(previous_immediate(*args, **kwargs))

    def create_timer_task(*args, **kwargs):
        return _save_pin(previous_timer(*args, **kwargs))

    async def handle_delivery(body, *, headers=None, client_factory=None, notifier=None):
        if client_factory is None:
            task_id = br._as_str(body.get("task_id") or "")
            raw = br.redis_client.get_redis().get(br.task_key(task_id)) if task_id else None
            if raw is not None:
                task = br.normalize_task(br._loads(raw))
                account_id = task.get("telegram_account_id")
                owner_id = int(task.get("user_id") or 0)
                if account_id not in (None, ""):
                    pinned_id = int(account_id)

                    async def pinned_factory():
                        return await telegram_delivery.get_telethon_client(
                            user_id=owner_id,
                            account_id=pinned_id,
                        )

                    client_factory = pinned_factory
        return await previous_delivery(
            body,
            headers=headers,
            client_factory=client_factory,
            notifier=notifier,
        )

    legacy.create_immediate_run = create_immediate_run
    legacy.create_timer_task = create_timer_task
    legacy.handle_delivery = handle_delivery
    br.handle_delivery = handle_delivery
