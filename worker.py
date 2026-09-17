"""Persistent Telegram/Telethon worker.

Run this service on Railway/Render/Fly.io/a VPS, NOT on Vercel.
Only this process owns TELEGRAM_SESSION_STRING, so the MTProto auth key
is never used by multiple serverless instances/IPs at the same time.
"""
import asyncio
import html
import os
import sys
import time
from typing import Any

from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

# Получение переменных окружения с проверкой
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
WORKER_SECRET = os.getenv("WORKER_SECRET")
PORT = int(os.getenv("PORT", "8080"))

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

# Redis & QStash
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
QSTASH_TOKEN = os.getenv("QSTASH_TOKEN")

# Проверка обязательных переменных
required_vars = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_API_ID": TELEGRAM_API_ID,
    "TELEGRAM_API_HASH": TELEGRAM_API_HASH,
    "SESSION_STRING": SESSION_STRING,
    "WORKER_SECRET": WORKER_SECRET,
}

missing_vars = [name for name, value in required_vars.items() if not value]
if missing_vars:
    print(f"❌ Отсутствуют необходимые переменные окружения: {', '.join(missing_vars)}", file=sys.stderr)
    sys.exit(1)

# Преобразование API_ID в int
try:
    API_ID = int(TELEGRAM_API_ID)
except ValueError:
    print(f"❌ TELEGRAM_API_ID должен быть числом, получено: {TELEGRAM_API_ID}", file=sys.stderr)
    sys.exit(1)

API_HASH = TELEGRAM_API_HASH

if not SESSION_STRING:
    raise RuntimeError("SESSION_STRING is required")
if not WORKER_SECRET:
    raise RuntimeError("WORKER_SECRET is required")

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
client_lock = asyncio.Lock()
started_at = time.time()


def authorized(request: web.Request) -> bool:
    return request.headers.get("X-Worker-Secret") == WORKER_SECRET


async def ensure_connected() -> None:
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session is not authorized")


async def get_chats() -> list[dict[str, Any]]:
    async with client_lock:
        await ensure_connected()
        result = []
        async for dialog in client.iter_dialogs():
            # Include groups, supergroups and channels. Exclude private users/bots.
            entity = dialog.entity
            is_channel = bool(getattr(entity, "broadcast", False))
            is_group = bool(dialog.is_group)
            if not (is_group or is_channel):
                continue
            result.append({
                "id": str(dialog.id),
                "title": dialog.name or "Без названия",
                "type": "channel" if is_channel else "group",
                "username": getattr(entity, "username", None),
                "megagroup": bool(getattr(entity, "megagroup", False)),
            })
        result.sort(key=lambda x: x["title"].casefold())
        return result


async def send_messages(chat_ids: list[str], message: str) -> dict[str, Any]:
    unique_ids = list(dict.fromkeys(str(x) for x in chat_ids))
    success = 0
    failed = 0
    results = []
    async with client_lock:
        await ensure_connected()
        for chat_id in unique_ids:
            try:
                await client.send_message(int(chat_id), message)
                success += 1
                results.append({"id": chat_id, "ok": True})
            except Exception as exc:
                failed += 1
                results.append({"id": chat_id, "ok": False, "error": str(exc)})
            await asyncio.sleep(1.0)
    return {"success": success, "failed": failed, "results": results}


async def health(request: web.Request):
    return web.json_response({
        "ok": True,
        "service": "telegram-worker",
        "telegram_connected": client.is_connected(),
        "uptime": int(time.time() - started_at),
    })


async def chats(request: web.Request):
    if not authorized(request):
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    try:
        data = await get_chats()
        return web.json_response({"ok": True, "chats": data})
    except Exception as exc:
        print("get_chats error:", repr(exc))
        return web.json_response({"ok": False, "error": str(exc)}, status=502)


async def send(request: web.Request):
    if not authorized(request):
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
        chat_ids = body.get("chat_ids") or []
        if not message:
            return web.json_response({"ok": False, "error": "message is required"}, status=400)
        if not chat_ids:
            return web.json_response({"ok": False, "error": "chat_ids is required"}, status=400)
        result = await send_messages(chat_ids, message)
        return web.json_response({"ok": True, **result})
    except Exception as exc:
        print("send error:", repr(exc))
        return web.json_response({"ok": False, "error": str(exc)}, status=502)


async def on_startup(app: web.Application):
    await ensure_connected()
    me = await client.get_me()
    print(f"Telegram worker ready: {me.id} / {me.first_name or ''}")


async def on_cleanup(app: web.Application):
    if client.is_connected():
        await client.disconnect()


app = web.Application(client_max_size=2 * 1024 * 1024)
app.router.add_get("/", health)
app.router.add_get("/health", health)
app.router.add_get("/chats", chats)
app.router.add_post("/send", send)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
