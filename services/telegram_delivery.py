"""Telethon user-session delivery primitives (lifecycle + dialogs).

Single place where the Telethon client is built, connected, authorized and
disconnected.  Telethon 1.36 is pinned in requirements.txt — it does NOT
accept ``timeout=`` on ``connect()`` nor ``request_timeout=`` on the client
constructor, so all time bounds here use ``asyncio.wait_for`` instead.
"""

import asyncio
import logging
import os
from typing import Any, Dict, List

from telethon import TelegramClient
from telethon.sessions import StringSession

logger = logging.getLogger("broadcast.telegram")

# Per-operation wall-clock bounds (seconds). Generous enough for cold TLS
# handshakes, small enough to never approach the Vercel function limit.
TELETHON_CONNECT_TIMEOUT = float(os.getenv("TELETHON_CONNECT_TIMEOUT", "20"))
TELETHON_FETCH_TIMEOUT = float(os.getenv("TELETHON_FETCH_TIMEOUT", "25"))


class MissingEnvError(RuntimeError):
    """A required environment variable is not configured."""


class SessionNotAuthorizedError(RuntimeError):
    """Telethon session exists but is not authorized for a user account."""


def _require_env(value, name: str) -> str:
    if not value:
        raise MissingEnvError(f"{name} is not configured")
    return value


def get_telethon_client_factory() -> TelegramClient:
    """Construct a (not yet connected) TelegramClient for this invocation.

    Env vars are re-read on every call so tests and credential rotations take
    effect without re-importing the module.  ``API_ID``/``API_HASH``/
    ``TELEGRAM_SESSION_STRING`` may also be injected as module attributes
    (used by tests); environment values take precedence when present.
    """
    api_id = os.getenv("API_ID") or globals().get("API_ID")
    api_hash = os.getenv("API_HASH") or globals().get("API_HASH")
    session = (os.getenv("TELEGRAM_SESSION_STRING")
               or globals().get("TELEGRAM_SESSION_STRING"))
    return TelegramClient(StringSession(_require_env(session, "TELEGRAM_SESSION_STRING")),
                          int(_require_env(api_id, "API_ID")),
                          _require_env(api_hash, "API_HASH"))


async def connect_authorized_client(client: TelegramClient) -> TelegramClient:
    """Connect a client and verify it belongs to an authorized account.

    The connect itself is bounded by asyncio.wait_for (Telethon's connect has
    no timeout kwarg).  On ANY failure the client is disconnected in a
    finally-style cleanup so no half-open socket leaks into the next serverless
    event loop usage.
    """
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_CONNECT_TIMEOUT)
        authorized = await asyncio.wait_for(client.is_user_authorized(),
                                           timeout=TELETHON_CONNECT_TIMEOUT)
        if not authorized:
            raise SessionNotAuthorizedError(
                "Telegram user session is not authorized. "
                "Пересоздайте TELEGRAM_SESSION_STRING на авторизованном устройстве."
            )
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            logger.exception("Failed to disconnect Telethon client after error")
        raise
    return client


async def get_telethon_client() -> TelegramClient:
    """Create a connected, authorized Telethon client for this invocation."""
    return await connect_authorized_client(get_telethon_client_factory())


async def fetch_user_dialogs(client) -> List[Dict[str, str]]:
    """Iterate ALL dialogs (no artificial limit); normalize groups/channels."""
    dialogs: List[Dict[str, str]] = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            dialogs.append({
                "id": str(dialog.id),
                "title": dialog.title or dialog.name or str(dialog.id),
            })
    return dialogs


async def fetch_dialogs_with_timeout(client) -> List[Dict[str, str]]:
    """Dialog fetch bounded by a wall-clock timeout for webhook safety."""
    return await asyncio.wait_for(fetch_user_dialogs(client), timeout=TELETHON_FETCH_TIMEOUT)
