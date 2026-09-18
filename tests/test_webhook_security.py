"""Tests for Telegram webhook request validation."""
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from api.index import app, verify_telegram_webhook


def webhook_request(secret: str | None = None) -> Request:
    headers = [] if secret is None else [(b"x-telegram-bot-api-secret-token", secret.encode())]
    return Request({"type": "http", "method": "POST", "headers": headers})


class WebhookSecurityTests(unittest.TestCase):
    def test_accepts_request_without_configured_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            verify_telegram_webhook(webhook_request())

    def test_rejects_missing_or_invalid_configured_secret(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "expected"}, clear=True):
            for request in (webhook_request(), webhook_request("wrong")):
                with self.assertRaises(HTTPException) as raised:
                    verify_telegram_webhook(request)
                self.assertEqual(raised.exception.status_code, 403)

    def test_accepts_matching_configured_secret(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "expected"}, clear=True):
            verify_telegram_webhook(webhook_request("expected"))

    def test_vercel_function_path_is_a_protected_webhook_alias(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "expected"}, clear=True):
            response = TestClient(app).post("/api/index.py", json={})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Invalid Telegram webhook secret")


if __name__ == "__main__":
    unittest.main()
