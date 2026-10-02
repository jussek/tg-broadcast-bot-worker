"""Regression tests for multi-user Telegram account isolation."""

import asyncio

from services import broadcast_runner as br
from storage import telegram_accounts as accounts


def test_sessions_are_encrypted_and_isolated(fake_redis, monkeypatch):
    monkeypatch.delenv("SESSION_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("WEBHOOK_SETUP_SECRET", "unit-test-strong-server-secret")

    accounts.save_account(
        101,
        "session-for-user-101",
        telegram_user_id=10001,
        username="alice",
        phone="+37120000001",
    )
    accounts.save_account(
        202,
        "session-for-user-202",
        telegram_user_id=20002,
        username="bob",
        phone="+37120000002",
    )

    raw_101 = str(fake_redis.store[accounts.account_key(101)])
    raw_202 = str(fake_redis.store[accounts.account_key(202)])
    assert "session-for-user-101" not in raw_101
    assert "session-for-user-202" not in raw_202
    assert accounts.get_session_string(101) == "session-for-user-101"
    assert accounts.get_session_string(202) == "session-for-user-202"
    assert accounts.get_public_account(101)["username"] == "alice"
    assert "session_ciphertext" not in accounts.get_public_account(101)


def test_connect_challenge_is_encrypted_and_short_lived_data(fake_redis, monkeypatch):
    monkeypatch.delenv("SESSION_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("WEBHOOK_SETUP_SECRET", "unit-test-strong-server-secret")

    token = accounts.create_connect_challenge(777)
    raw = str(fake_redis.store[accounts.challenge_key(token)])
    assert "777" not in raw
    challenge = accounts.load_connect_challenge(token)
    assert challenge["user_id"] == 777
    assert challenge["stage"] == "phone"

    challenge.update({
        "stage": "code",
        "phone": "+37120000000",
        "phone_code_hash": "secret-hash",
        "temp_session": "temporary-session",
    })
    accounts.save_connect_challenge(token, challenge)
    raw = str(fake_redis.store[accounts.challenge_key(token)])
    assert "+37120000000" not in raw
    assert "temporary-session" not in raw
    restored = accounts.load_connect_challenge(token)
    assert restored["temp_session"] == "temporary-session"


def test_disconnect_removes_only_that_users_account(fake_redis, monkeypatch):
    monkeypatch.delenv("SESSION_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("WEBHOOK_SETUP_SECRET", "unit-test-strong-server-secret")
    accounts.save_account(1, "session-one")
    accounts.save_account(2, "session-two")

    accounts.delete_account(1)

    assert not accounts.is_connected(1)
    assert accounts.is_connected(2)
    assert accounts.get_session_string(2) == "session-two"


def test_telegram_delivery_resolves_session_by_requested_user(monkeypatch):
    import services.telegram_delivery as td

    seen = []

    def fake_get_session(user_id):
        seen.append(user_id)
        return f"session:{user_id}"

    monkeypatch.setattr(td, "get_session_string", fake_get_session)
    assert td._session_for_user(55) == "session:55"
    assert td._session_for_user(66) == "session:66"
    assert seen == [55, 66]


def test_worker_opens_task_owners_personal_account(fake_redis, spy_publisher, monkeypatch):
    seen_users = []
    sent = []

    class FakeClient:
        async def get_input_entity(self, gid):
            return gid

        async def send_message(self, target, text, parse_mode=None):
            sent.append((target, text))

        async def disconnect(self):
            pass

    async def personal_client(user_id=None):
        seen_users.append(user_id)
        return FakeClient()

    monkeypatch.setattr("services.telegram_delivery.get_telethon_client", personal_client)

    task = br.create_immediate_run(4242, "HELLO", ["-100123"])
    result = asyncio.run(
        br.handle_delivery({"task_id": task["id"], "repeat_no": 0, "batch_no": 0})
    )

    assert result["status"] == "repeat_committed"
    assert seen_users == [4242]
    assert sent == [(-100123, "HELLO")]
