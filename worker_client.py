import os
from typing import Optional

import aiohttp

WORKER_URL: Optional[str] = None
WORKER_SECRET: Optional[str] = None
TIMEOUT: Optional[aiohttp.ClientTimeout] = None


def _get_worker_url() -> str:
    """Get worker URL with lazy initialization."""
    global WORKER_URL
    if WORKER_URL is None:
        url = os.environ.get("TELEGRAM_WORKER_URL")
        if not url:
            raise RuntimeError(
                "TELEGRAM_WORKER_URL is required. Set it in environment variables."
            )
        WORKER_URL = url.rstrip("/")
    return WORKER_URL


def _get_worker_secret() -> str:
    """Get worker secret with lazy initialization."""
    global WORKER_SECRET
    if WORKER_SECRET is None:
        secret = os.environ.get("WORKER_SECRET")
        if not secret:
            raise RuntimeError(
                "WORKER_SECRET is required. Set it in environment variables."
            )
        WORKER_SECRET = secret
    return WORKER_SECRET


def _get_timeout() -> aiohttp.ClientTimeout:
    """Get timeout with lazy initialization."""
    global TIMEOUT
    if TIMEOUT is None:
        timeout_seconds = int(os.getenv("WORKER_TIMEOUT_SECONDS", "90"))
        TIMEOUT = aiohttp.ClientTimeout(total=timeout_seconds)
    return TIMEOUT


async def _request(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["X-Worker-Secret"] = _get_worker_secret()
    async with aiohttp.ClientSession(timeout=_get_timeout()) as session:
        async with session.request(method, f"{_get_worker_url()}{path}", headers=headers, **kwargs) as response:
            data = await response.json(content_type=None)
            if response.status >= 400 or not data.get("ok", False):
                raise RuntimeError(data.get("error", f"Worker HTTP {response.status}"))
            return data


async def get_chats():
    data = await _request("GET", "/chats")
    return data.get("chats", [])


async def send_message(chat_ids, message: str):
    return await _request("POST", "/send", json={"chat_ids": list(chat_ids), "message": message})
