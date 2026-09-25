"""PRIORITY 1-3: runtime import, light startup, webhook routing, secrets."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'api'))

import pytest
from fastapi.testclient import TestClient

import index as index


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(index, "TELEGRAM_WEBHOOK_SECRET", "topsecret")
    monkeypatch.setattr(index, "WEBHOOK_SETUP_SECRET", "setupsecret")
    return TestClient(index.app)


def test_health_has_no_network_calls(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    for key in ("bot_configured", "redis_configured", "telegram_configured", "qstash_configured"):
        assert key in body


def test_startup_does_not_call_set_webhook(client):
    """The lifespan startup must never call Telegram setWebhook (flood control)."""
    calls = []

    async def fake_set_webhook(*args, **kwargs):
        calls.append(args)

    monkey_token = getattr(index, "BOT_TOKEN", None)
    try:
        index.BOT_TOKEN = "123:token"
        index.APP_URL = "https://example.vercel.app"
        real = index.Bot
        index.Bot = type("SpyBot", (), {"set_webhook": staticmethod(fake_set_webhook),
                                        "get_webhook_info": lambda self: None,
                                        "session": SimpleNamespace(close=lambda: None)})
        with TestClient(index.app) as _c:
            pass
        assert calls == []
    finally:
        index.Bot = real
        index.BOT_TOKEN = monkey_token


def test_webhook_rejects_bad_secret(client):
    resp = client.post("/api/webhook", json={"update_id": 1})
    assert resp.status_code == 403


def test_webhook_rejects_invalid_json(client):
    resp = client.post("/api/webhook", content=b"not-json",
                       headers={"X-Telegram-Bot-Api-Secret-Token": "topsecret"})
    assert resp.status_code == 400


def test_webhook_feeds_dispatcher_with_callback_query(client, monkeypatch):
    fed = []

    async def fake_feed(bot, update):
        fed.append(update)

    class FakeBotSession:
        closed = False

        async def close(self):
            FakeBotSession.closed = True

    class FakeBot:
        def __init__(self, *args, **kwargs):
            self.session = FakeBotSession()

    monkeypatch.setattr(index, "BOT_TOKEN", "123:abc")
    monkeypatch.setattr(index.dp, "feed_update", fake_feed)
    monkeypatch.setattr(index, "create_bot", lambda: FakeBot())

    payload = {
        "update_id": 99,
        "callback_query": {
            "id": "cb1",
            "data": "new_broadcast",
            "from": {"id": 5, "is_bot": False, "first_name": "U"},
            "chat_instance": "x",
            "message": {
                "message_id": 1, "date": 0,
                "chat": {"id": 5, "type": "private"},
                "text": "menu",
            },
        },
    }
    resp = client.post("/api/webhook", json=payload,
                       headers={"X-Telegram-Bot-Api-Secret-Token": "topsecret"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(fed) == 1
    assert fed[0].callback_query.data == "new_broadcast"
    assert FakeBotSession.closed is True  # no unclosed aiohttp session


def test_setup_webhook_requires_secret(client):
    assert client.post("/api/setup-webhook").status_code == 403
    assert client.post("/api/setup-webhook", headers={"X-Setup-Secret": "wrong"}).status_code == 403


def test_webhook_info_requires_secret(client):
    assert client.get("/api/webhook-info").status_code == 403
    assert client.get("/api/webhook-info", headers={"X-Setup-Secret": "wrong"}).status_code == 403


def test_process_requires_task_id(client):
    resp = client.post("/api/process", json={})
    assert resp.status_code == 400
