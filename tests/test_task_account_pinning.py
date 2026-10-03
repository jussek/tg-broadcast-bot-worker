import asyncio
from types import SimpleNamespace

from services import task_account_pinning as pinning


def test_new_task_is_pinned_to_active_account(monkeypatch):
    saved = []

    legacy = SimpleNamespace(
        create_immediate_run=lambda *args, **kwargs: {"id": "t1", "user_id": 10},
        create_timer_task=lambda *args, **kwargs: {"id": "t2", "user_id": 10},
        handle_delivery=lambda *args, **kwargs: None,
        save_task=lambda task: saved.append(dict(task)),
    )

    monkeypatch.setattr(pinning, "get_active_account_id", lambda user_id: 222)
    monkeypatch.setattr(pinning.br, "handle_delivery", lambda *args, **kwargs: None)

    pinning.install_task_account_pinning(legacy)

    immediate = legacy.create_immediate_run(user_id=10, message="x", groups=["1"])
    timer = legacy.create_timer_task(10, "x", ["1"], 5, 3)

    assert immediate["telegram_account_id"] == 222
    assert timer["telegram_account_id"] == 222
    assert saved[-2]["telegram_account_id"] == 222
    assert saved[-1]["telegram_account_id"] == 222


def test_worker_uses_pinned_account_even_after_active_account_changes(monkeypatch):
    opened = []
    received_factories = []

    task = {
        "id": "task-1",
        "user_id": 10,
        "telegram_account_id": 222,
        "status": "active",
    }

    class FakeRedis:
        def get(self, key):
            return "stored-task"

    async def previous_delivery(body, *, headers=None, client_factory=None, notifier=None):
        received_factories.append(client_factory)
        if client_factory:
            await client_factory()
        return {"ok": True}

    async def fake_get_client(*, user_id=None, account_id=None):
        opened.append((user_id, account_id))
        return object()

    legacy = SimpleNamespace(
        create_immediate_run=lambda *args, **kwargs: {"id": "x", "user_id": 10},
        create_timer_task=lambda *args, **kwargs: {"id": "y", "user_id": 10},
        handle_delivery=previous_delivery,
        save_task=lambda task: None,
    )

    fake_redis_module = SimpleNamespace(get_redis=lambda: FakeRedis())
    monkeypatch.setattr(pinning.br, "redis_client", fake_redis_module)
    monkeypatch.setattr(pinning.br, "task_key", lambda task_id: task_id)
    monkeypatch.setattr(pinning.br, "_as_str", lambda value: str(value))
    monkeypatch.setattr(pinning.br, "_loads", lambda raw: task)
    monkeypatch.setattr(pinning.br, "normalize_task", lambda value: dict(value))
    monkeypatch.setattr(pinning.br, "handle_delivery", previous_delivery)
    monkeypatch.setattr(pinning.telegram_delivery, "get_telethon_client", fake_get_client)
    monkeypatch.setattr(pinning, "get_active_account_id", lambda user_id: 999)

    pinning.install_task_account_pinning(legacy)

    result = asyncio.run(legacy.handle_delivery({"task_id": "task-1"}))

    assert result == {"ok": True}
    assert received_factories and received_factories[0] is not None
    assert opened == [(10, 222)]
