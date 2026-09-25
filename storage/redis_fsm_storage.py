"""Redis-backed aiogram FSM storage (serverless-safe state)."""
import asyncio
import json
from typing import Any, Dict

from aiogram.fsm.storage.base import BaseStorage, StorageKey

from .redis_client import get_redis


class RedisFSMStorage(BaseStorage):
    """Stores aiogram FSM state in Upstash Redis so it survives cold starts."""

    async def set_value(self, key: StorageKey, value: Dict[str, Any]) -> None:
        redis_key = f"fsm:{key.bot_id}:{key.user_id}:{key.chat_id}:state"
        await asyncio.to_thread(get_redis().set, redis_key, json.dumps(value))

    async def get_value(self, key: StorageKey) -> Dict[str, Any]:
        redis_key = f"fsm:{key.bot_id}:{key.user_id}:{key.chat_id}:state"
        raw = await asyncio.to_thread(get_redis().get, redis_key)
        if not raw:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode()
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
