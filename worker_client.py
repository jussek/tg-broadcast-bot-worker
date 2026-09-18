"""Worker client for communicating with Telegram Worker.

This module provides HTTP client functions to communicate with the persistent
Telegram Worker that runs Telethon. It also provides Redis functions for
distributed locking and caching only.

Redis is NOT used as primary storage - Supabase is the source of truth.
"""
import json
import os
import time
from typing import Optional

import aiohttp
from upstash_redis import Redis

WORKER_URL: Optional[str] = None
WORKER_SECRET: Optional[str] = None
TIMEOUT: Optional[aiohttp.ClientTimeout] = None
redis: Optional[Redis] = None


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
            # Allow missing secret in development
            WORKER_SECRET = "dev-secret"
        else:
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
            try:
                data = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as exc:
                body = (await response.text()).strip().replace("\n", " ")[:300]
                raise RuntimeError(
                    f"Worker returned a non-JSON HTTP {response.status} response: {body or '<empty>'}"
                ) from exc
            if not isinstance(data, dict):
                raise RuntimeError(f"Worker returned an invalid HTTP {response.status} response")
            if response.status >= 400 or not data.get("ok", False):
                raise RuntimeError(data.get("error") or f"Worker HTTP {response.status}")
            return data


async def get_chats():
    """Get chats from worker.
    
    Returns:
        List of chat dictionaries.
    """
    data = await _request("GET", "/chats")
    # Worker может вернуть {"ok": true, "chats": [...]} или просто список
    if isinstance(data, dict):
        return data.get("chats", [])
    if isinstance(data, list):
        return data
    raise RuntimeError("Worker returned an invalid chats response")


async def send_message(chat_ids: list, message: str):
    """Send message via worker."""
    return await _request("POST", "/send", json={"chat_ids": chat_ids, "message": message})


def _get_redis():
    global redis
    if redis is None:
        if Redis is None:
            raise RuntimeError("upstash_redis is not installed")
        url = os.getenv("UPSTASH_REDIS_REST_URL")
        token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
        if not url or not token:
            # Return None if Redis is not configured (optional for some operations)
            return None
        redis = Redis(url=url, token=token)
    return redis


# =========================================================
# REDIS LOCKS (for distributed task execution)
# =========================================================


def acquire_task_lock(task_id: str, seconds: int = 90) -> bool:
    """Acquire a lock for task execution. Returns True if lock acquired."""
    client = _get_redis()
    if not client:
        # If Redis is not configured, assume lock is always acquired (single instance)
        return True
    key = f"broadcast:lock:{task_id}"
    result = client.set(key, "1", nx=True, ex=seconds)
    return bool(result)


def release_task_lock(task_id: str):
    """Release task execution lock."""
    client = _get_redis()
    if client:
        client.delete(f"broadcast:lock:{task_id}")
