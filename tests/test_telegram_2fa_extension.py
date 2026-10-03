from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import multiuser_qr_extension as login_layer
from services import telegram_2fa_extension as twofa


def test_code_page_posts_to_native_verify_code_route():
    page = twofa._code_or_password_page(
        "token-abcdefghijklmnopqrstuvwxyz",
        {"stage": "code", "phone": "+37120000000"},
    )

    assert "verify-code" in page
    assert "requires_2fa" in page
    assert "verify-login" not in page


def test_password_page_is_rendered_for_password_stage():
    page = twofa._code_or_password_page(
        "token-abcdefghijklmnopqrstuvwxyz",
        {"stage": "password", "phone": "+37120000000", "password_attempts": 1},
    )

    assert "type='password'" in page
    assert "verify-password" in page
    assert "credentials:'same-origin'" in page
    assert "current-password" in page


def test_install_replaces_legacy_verify_code_route():
    app = FastAPI()

    @app.post("/connect/{token}/verify-code")
    async def legacy_verify_code(token: str):
        return {"legacy": True}

    legacy = SimpleNamespace(app=app)
    original_renderer = login_layer._connect_page_html
    try:
        twofa.install_twofa(legacy)
        routes = [
            route
            for route in app.router.routes
            if getattr(route, "path", None) == "/connect/{token}/verify-code"
            and "POST" in (getattr(route, "methods", None) or set())
        ]
    finally:
        login_layer._connect_page_html = original_renderer

    assert len(routes) == 1
    assert routes[0].endpoint.__name__ == "verify_code"


def test_verify_code_moves_challenge_to_password_stage(monkeypatch):
    challenge = {
        "stage": "code",
        "user_id": 77,
        "phone": "+37120000000",
        "phone_code_hash": "test-hash",
        "temp_session": "test-session",
    }
    captured = {}

    class FakeSession:
        def save(self):
            return "pending-session"

    class FakeClient:
        def __init__(self):
            self.session = FakeSession()
            self.disconnected = False

        async def connect(self):
            return None

        async def sign_in(self, **kwargs):
            raise twofa.SessionPasswordNeededError(request=None)

        async def disconnect(self):
            self.disconnected = True

    fake_client = FakeClient()

    monkeypatch.setattr(login_layer, "_load_valid_challenge", lambda token: challenge)
    monkeypatch.setattr(login_layer, "_telegram_client", lambda session: fake_client)
    monkeypatch.setattr(
        twofa,
        "save_connect_challenge",
        lambda token, value: captured.update(token=token, challenge=dict(value)),
    )

    app = FastAPI()

    @app.post("/connect/{token}/verify-code")
    async def legacy_verify_code(token: str):
        return {"legacy": True}

    legacy = SimpleNamespace(app=app)
    original_renderer = login_layer._connect_page_html
    try:
        twofa.install_twofa(legacy)
        with TestClient(app) as client:
            response = client.post(
                "/connect/token-abcdefghijklmnopqrstuvwxyz/verify-code",
                json={"code": "12345"},
            )
    finally:
        login_layer._connect_page_html = original_renderer

    assert response.status_code == 200
    assert response.json() == {"ok": True, "requires_2fa": True, "stage": "password"}
    assert captured["challenge"]["stage"] == "password"
    assert captured["challenge"]["temp_session"] == "pending-session"
    assert captured["challenge"]["password_attempts"] == 0
    assert "password" not in captured["challenge"]
    assert fake_client.disconnected is True
