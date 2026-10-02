"""Serverless-safe personal Telegram authorization helpers.

The temporary Telethon StringSession is serialized after every short request,
then encrypted by storage.telegram_accounts.  No long-lived socket or process
memory is required, which makes the flow suitable for Vercel functions.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

CONNECT_TIMEOUT = float(os.getenv("TELETHON_CONNECT_TIMEOUT", "20"))
LOGIN_TIMEOUT = float(os.getenv("TELETHON_LOGIN_TIMEOUT", "25"))


class LoginConfigurationError(RuntimeError):
    pass


@dataclass
class LoginResult:
    status: str
    session_string: Optional[str] = None
    account: Optional[Dict[str, Any]] = None


def _credentials() -> tuple[int, str]:
    api_id = (os.getenv("API_ID") or "").strip()
    api_hash = (os.getenv("API_HASH") or "").strip()
    if not api_id or not api_hash:
        raise LoginConfigurationError("API_ID/API_HASH are not configured")
    return int(api_id), api_hash


def _client(session_string: Optional[str] = None) -> TelegramClient:
    api_id, api_hash = _credentials()
    return TelegramClient(StringSession(session_string or ""), api_id, api_hash)


async def _connect(client: TelegramClient) -> None:
    await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)


async def send_login_code(phone: str) -> Dict[str, str]:
    client = _client()
    try:
        await _connect(client)
        sent = await asyncio.wait_for(
            client.send_code_request(phone), timeout=LOGIN_TIMEOUT
        )
        return {
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "temp_session": client.session.save(),
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def submit_login_code(
    *,
    temp_session: str,
    phone: str,
    phone_code_hash: str,
    code: str,
) -> LoginResult:
    client = _client(temp_session)
    try:
        await _connect(client)
        try:
            await asyncio.wait_for(
                client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=phone_code_hash,
                ),
                timeout=LOGIN_TIMEOUT,
            )
        except SessionPasswordNeededError:
            return LoginResult(
                status="password_required",
                session_string=client.session.save(),
            )
        return await _authorized_result(client)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def submit_2fa_password(*, temp_session: str, password: str) -> LoginResult:
    client = _client(temp_session)
    try:
        await _connect(client)
        await asyncio.wait_for(client.sign_in(password=password), timeout=LOGIN_TIMEOUT)
        return await _authorized_result(client)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _authorized_result(client: TelegramClient) -> LoginResult:
    authorized = await asyncio.wait_for(
        client.is_user_authorized(), timeout=LOGIN_TIMEOUT
    )
    if not authorized:
        raise RuntimeError("Telegram authorization did not complete")
    me = await asyncio.wait_for(client.get_me(), timeout=LOGIN_TIMEOUT)
    return LoginResult(
        status="authorized",
        session_string=client.session.save(),
        account={
            "telegram_user_id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None),
            "phone": getattr(me, "phone", None),
        },
    )
