"""Redis-backed aiogram FSM storage (serverless-safe state).

Implements the full ``BaseStorage`` interface of aiogram 3.x.  All Upstash
calls are synchronous, so they are off-loaded with ``asyncio.to_thread`` to
avoid blocking the event loop inside webhook handlers.
"""
import asyncio
import json
from typing import Any, Dict, Optional

from aiogram.fsm.storage.base import BaseStorage, StorageKey

from .redis_client import get_redis

DEFAULT_TTL = 60 * 60 * 24  # one day — plenty for interactive flows


def _key_to_string(key: StorageKey) -> str:
    thread = key.thread_id if getattr(key, "thread_id", None) is not None else "-"
    return f"fsm:{key.bot_id}:{key.chat_id}:{key.user_id}:{thread}:{key.destiny}"


class RedisFSMStorage(BaseStorage):
    """Stores aiogram FSM state/data in Upstash Redis so it survives cold starts."""

    def __init__(self, ttl: int = DEFAULT_TTL):
        self.ttl = ttl

    async def set_state(self, key: StorageKey, state: Optional[Any] = None) -> None:
        redis_key = f"{_key_to_string(key)}:state"
        value = state.value if hasattr(state, "value") else state
        if value is None:
            await asyncio.to_thread(get_redis().delete, redis_key)
            return
        await asyncio.to_thread(get_redis().set, redis_key, str(value), ex=self.ttl)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        redis_key = f"{_key_to_string(key)}:state"
        raw = await asyncio.to_thread(get_redis().get, redis_key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return str(raw)

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        redis_key = f"{_key_to_string(key)}:data"
        if not data:
            await asyncio.to_thread(get_redis().delete, redis_key)
            return
        await asyncio.to_thread(
            get_redis().set, redis_key, json.dumps(data, ensure_ascii=False, default=str), ex=self.ttl
        )

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        redis_key = f"{_key_to_string(key)}:data"
        raw = await asyncio.to_thread(get_redis().get, redis_key)
        if not raw:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode()
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def close(self) -> None:
        # The Redis client is shared process-wide and must stay open for the
        # next serverless invocation; nothing to close here.
        return None
