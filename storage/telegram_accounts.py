"""Encrypted Telegram account/session storage in Redis.

One bot owner may connect multiple Telegram accounts. Each account keeps its own
encrypted StringSession. A compatibility mirror stores the currently active
account under the original per-user key so existing code keeps working.
Short-lived login challenges are encrypted as well.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken

from .redis_client import get_redis

logger = logging.getLogger("broadcast.telegram_accounts")

ACCOUNT_PREFIX = "broadcast:telegram-account:"
ACCOUNT_ITEM_PREFIX = "broadcast:telegram-account-item:"
ACCOUNT_INDEX_PREFIX = "broadcast:telegram-account-index:"
ACTIVE_ACCOUNT_PREFIX = "broadcast:telegram-active-account:"
CHALLENGE_PREFIX = "broadcast:telegram-connect:"
CHALLENGE_TTL = 15 * 60
CONNECT_RATE_TTL = 45


class TelegramAccountNotConnectedError(RuntimeError):
    pass


class SessionEncryptionError(RuntimeError):
    pass


def account_key(user_id: int) -> str:
    """Compatibility key containing the currently active account record."""
    return f"{ACCOUNT_PREFIX}{int(user_id)}"


def account_item_key(user_id: int, telegram_user_id: int) -> str:
    return f"{ACCOUNT_ITEM_PREFIX}{int(user_id)}:{int(telegram_user_id)}"


def account_index_key(user_id: int) -> str:
    return f"{ACCOUNT_INDEX_PREFIX}{int(user_id)}"


def active_account_key(user_id: int) -> str:
    return f"{ACTIVE_ACCOUNT_PREFIX}{int(user_id)}"


def challenge_key(token: str) -> str:
    return f"{CHALLENGE_PREFIX}{token}"


def connect_rate_key(user_id: int) -> str:
    return f"broadcast:telegram-connect-rate:{int(user_id)}"


def _derive_fernet_key() -> bytes:
    configured = (os.getenv("SESSION_ENCRYPTION_KEY") or "").strip()
    if configured:
        raw = configured.encode("utf-8")
        try:
            Fernet(raw)
        except Exception as exc:
            raise SessionEncryptionError("SESSION_ENCRYPTION_KEY is not a valid Fernet key") from exc
        return raw

    fallback = (os.getenv("WEBHOOK_SETUP_SECRET") or "").strip()
    if not fallback:
        raise SessionEncryptionError(
            "SESSION_ENCRYPTION_KEY is not configured and WEBHOOK_SETUP_SECRET is unavailable"
        )
    digest = hashlib.sha256(
        ("tg-broadcast-personal-session-v1:" + fallback).encode("utf-8")
    ).digest()
    logger.warning(
        "SESSION_ENCRYPTION_KEY is not configured; deriving the session key from WEBHOOK_SETUP_SECRET."
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
        raise SessionEncryptionError("Telegram session cannot be decrypted with the configured key") from exc


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: Any):
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, (dict, list, int)):
        return value
    return json.loads(value)


def _load_index(user_id: int) -> List[int]:
    raw = _loads(get_redis().get(account_index_key(user_id)))
    if not raw:
        return []
    return [int(x) for x in raw]


def _save_index(user_id: int, values: List[int]) -> None:
    get_redis().set(account_index_key(user_id), _dumps(sorted(set(int(x) for x in values))))


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
    if not session_string:
        raise ValueError("session_string is required")
    owner_id = int(user_id)
    account_id = int(telegram_user_id if telegram_user_id is not None else owner_id)
    now = time.time()
    record = {
        "user_id": owner_id,
        "telegram_user_id": account_id,
        "username": username,
        "first_name": first_name,
        "phone_masked": mask_phone(phone),
        "session_ciphertext": _encrypt_text(session_string),
        "status": "connected",
        "connected_at": now,
        "updated_at": now,
    }
    redis = get_redis()
    redis.set(account_item_key(owner_id, account_id), _dumps(record))
    ids = _load_index(owner_id)
    if account_id not in ids:
        ids.append(account_id)
    _save_index(owner_id, ids)
    redis.set(active_account_key(owner_id), str(account_id))
    redis.set(account_key(owner_id), _dumps(record))
    return public_account(record)


def load_account(user_id: int, account_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    if account_id is None:
        return _loads(get_redis().get(account_key(user_id)))
    return _loads(get_redis().get(account_item_key(user_id, account_id)))


def public_account(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not record:
        return None
    return {key: value for key, value in record.items() if key != "session_ciphertext"}


def get_public_account(user_id: int) -> Optional[Dict[str, Any]]:
    return public_account(load_account(user_id))


def list_accounts(user_id: int) -> List[Dict[str, Any]]:
    ids = _load_index(user_id)
    if not ids:
        legacy = load_account(user_id)
        return [public_account(legacy)] if legacy else []
    out = []
    for account_id in ids:
        record = load_account(user_id, account_id)
        if record:
            item = public_account(record)
            item["active"] = account_id == get_active_account_id(user_id)
            out.append(item)
    return out


def get_active_account_id(user_id: int) -> Optional[int]:
    raw = get_redis().get(active_account_key(user_id))
    if raw is not None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    record = load_account(user_id)
    if record and record.get("telegram_user_id") is not None:
        return int(record["telegram_user_id"])
    return None


def set_active_account(user_id: int, account_id: int) -> Dict[str, Any]:
    owner_id = int(user_id)
    account_id = int(account_id)
    record = load_account(owner_id, account_id)
    if not record:
        raise TelegramAccountNotConnectedError("Telegram account is not connected")
    redis = get_redis()
    redis.set(active_account_key(owner_id), str(account_id))
    redis.set(account_key(owner_id), _dumps(record))
    clear_group_cache(owner_id)
    return public_account(record)


def is_connected(user_id: int, account_id: Optional[int] = None) -> bool:
    record = load_account(user_id, account_id)
    return bool(record and record.get("status") == "connected" and record.get("session_ciphertext"))


def get_session_string(user_id: int, account_id: Optional[int] = None) -> str:
    record = load_account(user_id, account_id)
    if not record or record.get("status") != "connected":
        raise TelegramAccountNotConnectedError(
            "Telegram-аккаунт не подключён. Откройте «👤 Telegram-аккаунт» и выполните подключение."
        )
    encrypted = record.get("session_ciphertext")
    if not encrypted:
        raise TelegramAccountNotConnectedError("Telegram session is missing")
    return _decrypt_text(encrypted)


def delete_account(user_id: int, account_id: Optional[int] = None) -> None:
    owner_id = int(user_id)
    redis = get_redis()
    target_id = int(account_id) if account_id is not None else get_active_account_id(owner_id)
    ids = _load_index(owner_id)

    if target_id is None:
        redis.delete(account_key(owner_id))
        return

    redis.delete(account_item_key(owner_id, target_id))
    ids = [x for x in ids if int(x) != target_id]
    if ids:
        _save_index(owner_id, ids)
        set_active_account(owner_id, ids[0])
    else:
        redis.delete(account_index_key(owner_id))
        redis.delete(active_account_key(owner_id))
        redis.delete(account_key(owner_id))
        clear_group_cache(owner_id)


def clear_group_cache(user_id: int) -> None:
    get_redis().delete(f"broadcast:groups:{int(user_id)}")


def create_connect_challenge(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    payload = {"user_id": int(user_id), "stage": "phone", "created_at": time.time()}
    save_connect_challenge(token, payload)
    return token


def save_connect_challenge(token: str, payload: Dict[str, Any]) -> None:
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
    return bool(
        get_redis().set(
            connect_rate_key(user_id),
            str(int(time.time())),
            nx=True,
            ex=CONNECT_RATE_TTL,
        )
    )
