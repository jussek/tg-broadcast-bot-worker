"""Encrypted per-user Telegram account/session storage in Redis.

The bot user id is the ownership boundary.  Every authorized Telethon
StringSession is encrypted before it is persisted and is only decrypted on the
server immediately before a Telegram operation.

Redis is already the durable server-side store used by the application.  The
account records intentionally have no TTL; short-lived login challenges do.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from .redis_client import get_redis

logger = logging.getLogger("broadcast.telegram_accounts")

ACCOUNT_PREFIX = "broadcast:telegram-account:"
CHALLENGE_PREFIX = "broadcast:telegram-connect:"
CHALLENGE_TTL = 15 * 60
CONNECT_RATE_TTL = 45


class TelegramAccountNotConnectedError(RuntimeError):
    """The bot user has not connected a personal Telegram account."""


class SessionEncryptionError(RuntimeError):
    """The server cannot encrypt/decrypt Telegram session material."""


def account_key(user_id: int) -> str:
    return f"{ACCOUNT_PREFIX}{int(user_id)}"


def challenge_key(token: str) -> str:
    return f"{CHALLENGE_PREFIX}{token}"


def connect_rate_key(user_id: int) -> str:
    return f"broadcast:telegram-connect-rate:{int(user_id)}"


def _derive_fernet_key() -> bytes:
    """Return the encryption key used for Telegram session material.

    Preferred configuration is SESSION_ENCRYPTION_KEY, a standard Fernet key.
    For backwards-compatible zero-downtime rollout we can deterministically
    derive a dedicated encryption key from WEBHOOK_SETUP_SECRET.  That secret
    is already server-only and high entropy in production.  A dedicated key is
    still recommended so it can be rotated independently.
    """
    configured = (os.getenv("SESSION_ENCRYPTION_KEY") or "").strip()
    if configured:
        raw = configured.encode("utf-8")
        try:
            Fernet(raw)
        except Exception as exc:  # noqa: BLE001
            raise SessionEncryptionError(
                "SESSION_ENCRYPTION_KEY is not a valid Fernet key"
            ) from exc
        return raw

    fallback = (os.getenv("WEBHOOK_SETUP_SECRET") or "").strip()
    if not fallback:
        raise SessionEncryptionError(
            "SESSION_ENCRYPTION_KEY is not configured and WEBHOOK_SETUP_SECRET "
            "is unavailable for a safe fallback"
        )
    digest = hashlib.sha256(
        ("tg-broadcast-personal-session-v1:" + fallback).encode("utf-8")
    ).digest()
    logger.warning(
        "SESSION_ENCRYPTION_KEY is not configured; deriving the session key "
        "from WEBHOOK_SETUP_SECRET. Configure a dedicated key before rotating "
        "WEBHOOK_SETUP_SECRET."
    )
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_derive_fernet_key())


def _encrypt_text(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt_text(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SessionEncryptionError(
            "Telegram session cannot be decrypted with the configured key"
        ) from exc


def _dumps(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, dict):
        return value
    return json.loads(value)


def mask_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) <= 4:
        return "••••"
    return "+" + digits[:2] + "•" * max(4, len(digits) - 4) + digits[-2:]


def save_account(
    user_id: int,
    session_string: str,
    *,
    telegram_user_id: Optional[int] = None,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    phone: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist one authorized account for one bot user.

    The StringSession is never stored in plaintext.  Reconnecting a different
    Telegram account simply replaces the record owned by this bot user.
    """
    if not session_string:
        raise ValueError("session_string is required")
    now = time.time()
    record = {
        "user_id": int(user_id),
        "telegram_user_id": None if telegram_user_id is None else int(telegram_user_id),
        "username": username,
        "first_name": first_name,
        "phone_masked": mask_phone(phone),
        "session_ciphertext": _encrypt_text(session_string),
        "status": "connected",
        "connected_at": now,
        "updated_at": now,
    }
    get_redis().set(account_key(user_id), _dumps(record))
    return public_account(record)


def load_account(user_id: int) -> Optional[Dict[str, Any]]:
    return _loads(get_redis().get(account_key(user_id)))


def public_account(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not record:
        return None
    return {
        key: value
        for key, value in record.items()
        if key != "session_ciphertext"
    }


def get_public_account(user_id: int) -> Optional[Dict[str, Any]]:
    return public_account(load_account(user_id))


def is_connected(user_id: int) -> bool:
    record = load_account(user_id)
    return bool(
        record
        and record.get("status") == "connected"
        and record.get("session_ciphertext")
    )


def get_session_string(user_id: int) -> str:
    record = load_account(user_id)
    if not record or record.get("status") != "connected":
        raise TelegramAccountNotConnectedError(
            "Telegram-аккаунт не подключён. Откройте «👤 Telegram-аккаунт» "
            "и выполните подключение."
        )
    encrypted = record.get("session_ciphertext")
    if not encrypted:
        raise TelegramAccountNotConnectedError("Telegram session is missing")
    return _decrypt_text(encrypted)


def delete_account(user_id: int) -> None:
    get_redis().delete(account_key(user_id))


def clear_group_cache(user_id: int) -> None:
    get_redis().delete(f"broadcast:groups:{int(user_id)}")


def create_connect_challenge(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    payload = {
        "user_id": int(user_id),
        "stage": "phone",
        "created_at": time.time(),
    }
    save_connect_challenge(token, payload)
    return token


def save_connect_challenge(token: str, payload: Dict[str, Any]) -> None:
    """Persist an encrypted login challenge with a short TTL."""
    encoded = _encrypt_text(_dumps(dict(payload)))
    get_redis().set(challenge_key(token), encoded, ex=CHALLENGE_TTL)


def load_connect_challenge(token: str) -> Optional[Dict[str, Any]]:
    raw = get_redis().get(challenge_key(token))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(_decrypt_text(str(raw)))


def delete_connect_challenge(token: str) -> None:
    get_redis().delete(challenge_key(token))


def allow_send_code(user_id: int) -> bool:
    """Small anti-spam guard around Telegram send_code_request."""
    return bool(
        get_redis().set(
            connect_rate_key(user_id),
            str(int(time.time())),
            nx=True,
            ex=CONNECT_RATE_TTL,
        )
    )
