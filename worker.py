import os
import sys
import logging
import asyncio
from aiohttp import web
from telethon import TelegramClient
from upstash_redis import Redis
from qstash import Client as QStashClient

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
redis = Redis({
    "url": UPSTASH_REDIS_REST_URL,
    "token": UPSTASH_REDIS_REST_TOKEN,
})

qstash = QStashClient(token=QSTASH_TOKEN)

client = TelegramClient(
    session=SESSION_STRING,
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
    try:
        data = await request.json()
        logger.info(f"Получен вебхук: {data}")
        
        # Запускаем отправку в фоне
        asyncio.create_task(send_message_task(data))
        
        return web.json_response({"status": "accepted"})
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}")
        return web.json_response({"error": str(e)}, status=500)

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
app.router.add_post('/webhook', handle_webhook)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8080))
    logger.info(f"🌐 Запуск сервера на порту {port}...")
    web.run_app(app, host='0.0.0.0', port=port)
