import os
import sys
import logging
import asyncio
from aiohttp import web
from telethon import TelegramClient
from telethon.sessions import StringSession
from upstash_redis import Redis
from qstash import QStash as QStashClient

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Получение переменных окружения ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
QSTASH_TOKEN = os.getenv("QSTASH_TOKEN")
WORKER_SECRET = os.getenv("WORKER_SECRET")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

# --- Проверка обязательных переменных ---
required_vars = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_API_ID": TELEGRAM_API_ID,
    "TELEGRAM_API_HASH": TELEGRAM_API_HASH,
    "SESSION_STRING": SESSION_STRING,
    "UPSTASH_REDIS_REST_URL": UPSTASH_REDIS_REST_URL,
    "UPSTASH_REDIS_REST_TOKEN": UPSTASH_REDIS_REST_TOKEN,
    "QSTASH_TOKEN": QSTASH_TOKEN,
    "WORKER_SECRET": WORKER_SECRET,
}

missing_vars = [key for key, value in required_vars.items() if not value]

if missing_vars:
    logger.error(f"❌ Отсутствуют необходимые переменные окружения: {', '.join(missing_vars)}")
    logger.error("Проверьте настройки в панели Render (Environment Variables).")
    sys.exit(1)

# Преобразование API_ID в int
try:
    TELEGRAM_API_ID = int(TELEGRAM_API_ID)
except ValueError:
    logger.error("❌ TELEGRAM_API_ID должен быть числом!")
    sys.exit(1)

# --- Инициализация клиентов ---
redis = Redis(
    url=UPSTASH_REDIS_REST_URL,
    token=UPSTASH_REDIS_REST_TOKEN,
)

qstash = QStashClient(token=QSTASH_TOKEN)

client = TelegramClient(
    StringSession(SESSION_STRING),
    api_id=TELEGRAM_API_ID,
    api_hash=TELEGRAM_API_HASH
)

# --- Логика воркера ---
async def send_message_task(message_data):
    """Отправка сообщения через Telethon"""
    try:
        chat_id = message_data.get("chat_id")
        text = message_data.get("text")
        
        if not chat_id or not text:
            logger.warning("Получены некорректные данные для отправки")
            return

        await client.send_message(chat_id, text)
        logger.info(f"✅ Сообщение отправлено в чат {chat_id}")
        
        # Логирование в Redis (опционально)
        await redis.set(f"log:{chat_id}", "sent")
        
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения: {e}")

async def handle_webhook(request):
    """Обработчик вебхука от QStash/Vercel"""
    # Проверка секрета
    worker_secret = request.headers.get("X-Worker-Secret")
    if not worker_secret or worker_secret != WORKER_SECRET:
        logger.warning("Попытка доступа без правильного секрета")
        return web.json_response({"error": "Unauthorized"}, status=401)
    
    try:
        data = await request.json()
        logger.info(f"Получен вебхук: {data}")
        
        # Запускаем отправку в фоне
        asyncio.create_task(send_message_task(data))
        
        return web.json_response({"status": "accepted"})
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_send(request):
    """Обработчик отправки сообщений от Vercel"""
    worker_secret = request.headers.get("X-Worker-Secret")
    if not worker_secret or worker_secret != WORKER_SECRET:
        return web.json_response({"error": "Unauthorized", "ok": False}, status=401)
    
    try:
        data = await request.json()
        chat_ids = data.get("chat_ids", [])
        message = data.get("message", "")
        
        if not chat_ids or not message:
            return web.json_response({"error": "Invalid data", "ok": False}, status=400)
        
        # Отправляем сообщения параллельно
        async def send_to_chat(chat_id):
            try:
                await client.send_message(chat_id, message)
                return True
            except Exception as e:
                logger.error(f"Ошибка отправки в {chat_id}: {e}")
                return False
        
        results = await asyncio.gather(*[send_to_chat(cid) for cid in chat_ids], return_exceptions=True)
        success_count = sum(1 for r in results if r is True)
        
        logger.info(f"Отправлено {success_count}/{len(chat_ids)} сообщений")
        return web.json_response({"ok": True, "sent": success_count, "total": len(chat_ids)})
    except Exception as e:
        logger.error(f"Ошибка handle_send: {e}")
        return web.json_response({"error": str(e), "ok": False}, status=500)

async def handle_get_chats(request):
    """Получение списка чатов для Vercel"""
    worker_secret = request.headers.get("X-Worker-Secret")
    if not worker_secret or worker_secret != WORKER_SECRET:
        return web.json_response({"error": "Unauthorized", "ok": False}, status=401)
    
    try:
        dialogs = await client.get_dialogs()
        chats = []
        for dialog in dialogs:
            chat = dialog.chat
            chat_type = "channel" if chat.broadcast else ("group" if chat.megagroup else "private")
            chats.append({
                "id": str(chat.id),
                "title": getattr(chat, "title", None) or getattr(chat, "username", "Unknown"),
                "type": chat_type,
                "username": getattr(chat, "username", None),
            })
        return web.json_response({"ok": True, "chats": chats})
    except Exception as e:
        logger.error(f"Ошибка получения чатов: {e}")
        return web.json_response({"error": str(e), "ok": False, "chats": []}, status=500)

async def handle_health(request):
    """Health check endpoint"""
    return web.json_response({"status": "ok"})

async def on_startup(app):
    """Запуск клиента Telegram при старте"""
    logger.info("🚀 Запуск Telegram клиента...")
    await client.start(bot_token=TELEGRAM_BOT_TOKEN)
    logger.info("✅ Telegram клиент подключен!")
    
    # Проверка подключения к Redis
    try:
        await redis.ping()
        logger.info("✅ Redis подключен!")
    except Exception as e:
        logger.warning(f"⚠️ Ошибка подключения к Redis: {e}")

async def on_shutdown(app):
    """Остановка клиента при выключении"""
    logger.info("🛑 Остановка Telegram клиента...")
    await client.disconnect()

# --- Создание приложения ---
app = web.Application()
app.router.add_post('/webhook', handle_webhook)  # Для QStash
app.router.add_post('/send', handle_send)        # Для Vercel API
app.router.add_get('/chats', handle_get_chats)   # Для Vercel API
app.router.add_get('/health', handle_health)     # Health check для Render
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8080))
    logger.info(f"🌐 Запуск сервера на порту {port}...")
    web.run_app(app, host='0.0.0.0', port=port)
