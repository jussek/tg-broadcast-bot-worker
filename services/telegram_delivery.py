"""Telethon user-session delivery primitives (lifecycle + dialogs).

Each bot owner may have multiple encrypted Telegram StringSessions. Production
broadcast tasks can pin an exact Telegram account id so switching the active
account later does not change the sender of an already-created task.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, List, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession

from storage.telegram_accounts import (
    SessionEncryptionError,
    TelegramAccountNotConnectedError,
    get_session_string,
)

logger = logging.getLogger("broadcast.telegram")

TELETHON_CONNECT_TIMEOUT = float(os.getenv("TELETHON_CONNECT_TIMEOUT", "20"))
TELETHON_FETCH_TIMEOUT = float(os.getenv("TELETHON_FETCH_TIMEOUT", "25"))


class MissingEnvError(RuntimeError):
    pass


class SessionNotAuthorizedError(RuntimeError):
    pass


def _require_env(value, name: str) -> str:
    if not value:
        raise MissingEnvError(f"{name} is not configured")
    return value


def _session_for_user(user_id: Optional[int], account_id: Optional[int] = None) -> str:
    if user_id is None:
        return _require_env(
            os.getenv("TELEGRAM_SESSION_STRING") or globals().get("TELEGRAM_SESSION_STRING"),
            "TELEGRAM_SESSION_STRING",
        )
    try:
        return get_session_string(int(user_id), account_id=account_id)
    except (TelegramAccountNotConnectedError, SessionEncryptionError) as exc:
        raise SessionNotAuthorizedError(str(exc)) from exc


def get_telethon_client_factory(
    user_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> TelegramClient:
    api_id = os.getenv("API_ID") or globals().get("API_ID")
    api_hash = os.getenv("API_HASH") or globals().get("API_HASH")
    session = _session_for_user(user_id, account_id=account_id)
    return TelegramClient(
        StringSession(session),
        int(_require_env(api_id, "API_ID")),
        _require_env(api_hash, "API_HASH"),
    )


async def connect_authorized_client(client: TelegramClient) -> TelegramClient:
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_CONNECT_TIMEOUT)
        authorized = await asyncio.wait_for(
            client.is_user_authorized(), timeout=TELETHON_CONNECT_TIMEOUT
        )
        if not authorized:
            raise SessionNotAuthorizedError(
                "Telegram-сессия больше не авторизована. Переподключите аккаунт "
                "в разделе «👤 Telegram-аккаунт»."
            )
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            logger.exception("Failed to disconnect Telethon client after error")
        raise
    return client


async def get_telethon_client(
    user_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> TelegramClient:
    if user_id is None:
        client = get_telethon_client_factory()
    else:
        client = get_telethon_client_factory(user_id, account_id=account_id)
    return await connect_authorized_client(client)


def _obviously_writable_dialog(dialog) -> bool:
    entity = getattr(dialog, "entity", None)
    if dialog.is_channel and getattr(entity, "broadcast", False):
        return bool(getattr(entity, "creator", False) or getattr(entity, "admin_rights", None))
    return bool(dialog.is_group or dialog.is_channel)


async def fetch_user_dialogs(client) -> List[Dict[str, str]]:
    dialogs: List[Dict[str, str]] = []
    async for dialog in client.iter_dialogs():
        if not _obviously_writable_dialog(dialog):
            continue
        dialogs.append({
            "id": str(dialog.id),
            "title": dialog.title or dialog.name or str(dialog.id),
        })
    return dialogs


async def fetch_dialogs_with_timeout(client) -> List[Dict[str, str]]:
    return await asyncio.wait_for(fetch_user_dialogs(client), timeout=TELETHON_FETCH_TIMEOUT)
