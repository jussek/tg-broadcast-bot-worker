from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import multiuser_qr_extension as login_layer
from services import telegram_2fa_extension as twofa


def test_password_page_uses_password_field_and_same_origin_post():
    page = twofa._code_or_password_page(
        "token-abcdefghijklmnopqrstuvwxyz",
        {"stage": "password", "phone": "+37120000000", "password_attempts": 1},
    )

    assert "type='password'" in page
    assert "verify-password" in page
    assert "credentials:'same-origin'" in page
    assert "current-password" in page
    assert "password_attempts" not in page


def test_code_page_uses_twofa_aware_verify_route():
    page = twofa._code_or_password_page(
        "token-abcdefghijklmnopqrstuvwxyz",
        {"stage": "code", "phone": "+37120000000"},
    )

    assert "verify-login" in page
    assert "requires_2fa" in page
    assert "verify-code" not in page


def test_verify_password_never_persists_password(monkeypatch):
    challenge = {
        "stage": "password",
        "user_id": 77,
        "phone": "+37120000000",
        "temp_session": "temporary-session",
        "password_attempts": 0,
    }
    captured = {}

    class FakeSession:
        def save(self):
            return "authorized-session"

    class FakeClient:
        def __init__(self):
            self.session = FakeSession()
            self.password = None
            self.disconnected = False

        async def connect(self):
            return None

        async def sign_in(self, *, password=None, **kwargs):
            self.password = password
            return None

        async def get_me(self):
            return SimpleNamespace(
                id=123456,
                username="owner_alt",
                first_name="Owner",
                phone="37120000000",
            )

        async def disconnect(self):
            self.disconnected = True

    fake_client = FakeClient()

    monkeypatch.setattr(login_layer, "_load_valid_challenge", lambda token: challenge)
    monkeypatch.setattr(login_layer, "_telegram_client", lambda session: fake_client)
    monkeypatch.setattr(twofa, "delete_connect_challenge", lambda token: captured.setdefault("deleted", token))
    monkeypatch.setattr(twofa, "clear_group_cache", lambda user_id: captured.setdefault("cleared", user_id))

    def fake_save_account(user_id, session_string, **kwargs):
        captured["saved"] = {
            "user_id": user_id,
            "session": session_string,
            **kwargs,
        }

    async def fake_notify_success(legacy, user_id, me):
        captured["notified"] = user_id

    monkeypatch.setattr(twofa, "save_account", fake_save_account)
    monkeypatch.setattr(login_layer, "_notify_success", fake_notify_success)

    original_renderer = login_layer._connect_page_html
    legacy = SimpleNamespace(app=FastAPI())
    try:
        twofa.install_twofa(legacy)
        with TestClient(legacy.app) as client:
            response = client.post(
                "/connect/token-abcdefghijklmnopqrstuvwxyz/verify-password",
                json={"password": "my-cloud-password"},
            )
    finally:
        login_layer._connect_page_html = original_renderer

    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert fake_client.password == "my-cloud-password"
    assert fake_client.disconnected is True
    assert captured["saved"]["session"] == "authorized-session"
    assert captured["saved"]["telegram_user_id"] == 123456
    assert captured["deleted"] == "token-abcdefghijklmnopqrstuvwxyz"
    assert captured["cleared"] == 77
    assert "password" not in challenge
    assert "my-cloud-password" not in repr(challenge)
