import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()


def _credentials():
    api_id_raw = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    session_string = os.getenv("TELEGRAM_SESSION_STRING")
    if not api_id_raw:
        raise RuntimeError("API_ID не найден.")
    if not api_hash:
        raise RuntimeError("API_HASH не найден.")
    if not session_string:
        raise RuntimeError("TELEGRAM_SESSION_STRING не найден.")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise RuntimeError("API_ID должен быть числом.") from exc
    return api_id, api_hash, session_string.strip()


def build_client() -> TelegramClient:
    api_id, api_hash, session_string = _credentials()
    return TelegramClient(StringSession(session_string), api_id, api_hash)


async def create_client():
    client = build_client()
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram session не авторизована.")
    return client


async def send_to_groups(client, message: str, group_ids: list):
    success = 0
    failed = 0
    results = []

    for group_id in group_ids:
        chat_id = int(group_id)
        try:
            await client.send_message(chat_id, message)
            success += 1
            results.append({"group_id": chat_id, "status": "success"})
            print(f"Отправлено в {chat_id}")
        except Exception as e:
            failed += 1
            results.append({"group_id": chat_id, "status": "failed", "error": str(e)})
            print(f"Ошибка {chat_id}: {e}")
        await asyncio.sleep(1)

    return {
        "success": success,
        "failed": failed,
        "results": results,
    }


async def list_group_dialogs():
    client = await create_client()
    try:
        result = []
        async for dialog in client.iter_dialogs():
            if dialog.is_group:
                result.append({
                    "id": int(dialog.id),
                    "name": dialog.name or str(dialog.id),
                })
        return result
    finally:
        await client.disconnect()
