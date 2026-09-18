"""Supabase-backed aiogram FSM storage for stateless webhook deployments."""
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from storage.supabase_storage import get_supabase_client, utc_now


class SupabaseFSMStorage(BaseStorage):
    """Persist bot conversation state across Vercel function invocations."""

    table_name = "bot_fsm_states"

    @staticmethod
    def _identity(key: StorageKey) -> dict[str, Any]:
        return {
            "bot_id": key.bot_id,
            "chat_id": key.chat_id,
            "user_id": key.user_id,
            "thread_id": key.thread_id or 0,
            "destiny": key.destiny,
        }

    def _get_record(self, key: StorageKey) -> dict[str, Any] | None:
        query = get_supabase_client().table(self.table_name).select("state,data")
        for field, value in self._identity(key).items():
            query = query.eq(field, value)
        response = query.limit(1).execute()
        return response.data[0] if response.data else None

    def _upsert(self, key: StorageKey, **values: Any) -> None:
        record = self._identity(key)
        record.update(values)
        record["updated_at"] = utc_now()
        get_supabase_client().table(self.table_name).upsert(
            record,
            on_conflict="bot_id,chat_id,user_id,thread_id,destiny",
        ).execute()

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        current = self._get_record(key)
        state_name = state.state if isinstance(state, State) else state
        self._upsert(key, state=state_name, data=(current or {}).get("data") or {})

    async def get_state(self, key: StorageKey) -> str | None:
        record = self._get_record(key)
        return record.get("state") if record else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        current = self._get_record(key)
        self._upsert(key, state=(current or {}).get("state"), data=dict(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        record = self._get_record(key)
        return dict((record or {}).get("data") or {})

    async def close(self) -> None:
        """Supabase's synchronous client does not need explicit closure."""
