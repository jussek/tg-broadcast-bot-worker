"""Telegram service using Telethon for user API."""
import os
import asyncio
from typing import List, Dict, Optional
from telethon import TelegramClient
from telethon.sessions import StringSession


def _get_credentials() -> tuple:
    """Get Telegram API credentials from environment."""
    api_id_raw = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    session_string = os.getenv("TELEGRAM_SESSION_STRING")
    
    if not api_id_raw:
        raise RuntimeError("API_ID не найден")
    if not api_hash:
        raise RuntimeError("API_HASH не найден")
    if not session_string:
        raise RuntimeError("TELEGRAM_SESSION_STRING не найден")
    
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise RuntimeError("API_ID должен быть числом") from exc
    
    return api_id, api_hash, session_string.strip()


def build_client() -> TelegramClient:
    """Build Telethon client without connecting."""
    api_id, api_hash, session_string = _get_credentials()
    return TelegramClient(StringSession(session_string), api_id, api_hash)


async def get_telegram_client() -> TelegramClient:
    """Create and connect Telethon client."""
    client = build_client()
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram сессия не авторизована")
    return client


class TelegramService:
    """Service for Telegram operations."""
    
    def __init__(self):
        self._client: Optional[TelegramClient] = None
    
    async def connect(self) -> TelegramClient:
        """Connect to Telegram."""
        self._client = await get_telegram_client()
        return self._client
    
    async def disconnect(self):
        """Disconnect from Telegram."""
        if self._client:
            await self._client.disconnect()
            self._client = None
    
    async def list_groups(self) -> List[Dict]:
        """List user's groups and channels."""
        if not self._client:
            await self.connect()
        
        result = []
        async for dialog in self._client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                result.append({
                    "id": str(dialog.id),
                    "title": dialog.name or str(dialog.id),
                })
        return result
    
    async def send_message(self, chat_id: int, message: str, parse_mode: str = "html") -> bool:
        """Send message to a chat."""
        if not self._client:
            await self.connect()
        
        try:
            entity = await self._client.get_entity(chat_id)
            await self._client.send_message(entity, message, parse_mode=parse_mode)
            return True
        except Exception as e:
            print(f"Ошибка отправки в {chat_id}: {e}")
            return False
    
    async def send_to_groups(self, message: str, group_ids: list) -> Dict:
        """Send message to multiple groups."""
        success = 0
        failed = 0
        results = []
        
        for gid in group_ids:
            chat_id = int(gid)
            ok = await self.send_message(chat_id, message)
            if ok:
                success += 1
                results.append({"group_id": chat_id, "status": "success"})
            else:
                failed += 1
                results.append({"group_id": chat_id, "status": "failed"})
            await asyncio.sleep(0.5)  # Rate limiting
        
        return {"success": success, "failed": failed, "results": results}


# Convenience functions for backward compatibility
async def send_message_to_group(client: TelegramClient, chat_id: int, message: str) -> bool:
    """Send a single message to a group."""
    try:
        entity = await client.get_entity(chat_id)
        await client.send_message(entity, message, parse_mode="html")
        return True
    except Exception:
        return False


async def list_user_groups(client: TelegramClient) -> List[Dict]:
    """List user's groups."""
    result = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            result.append({
                "id": str(dialog.id),
                "name": dialog.name or str(dialog.id),
            })
    return result
