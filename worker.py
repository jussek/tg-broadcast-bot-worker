import os
import sys
import logging
import asyncio
from aiohttp import web
from telethon import TelegramClient
from telethon.sessions import StringSession
from upstash_redis import Redis

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Получение переменных окружения ---
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
WORKER_SECRET = os.getenv("WORKER_SECRET")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# --- Проверка обязательных переменных ---
required_vars = {
    "TELEGRAM_API_ID": TELEGRAM_API_ID,
    "TELEGRAM_API_HASH": TELEGRAM_API_HASH,
    "TELEGRAM_SESSION_STRING": TELEGRAM_SESSION_STRING,
    "UPSTASH_REDIS_REST_URL": UPSTASH_REDIS_REST_URL,
    "UPSTASH_REDIS_REST_TOKEN": UPSTASH_REDIS_REST_TOKEN,
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

client = TelegramClient(
    StringSession(TELEGRAM_SESSION_STRING),
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
        
        # Валидация входных данных
        if not chat_ids:
            return web.json_response({"error": "chat_ids is required", "ok": False, "success": 0, "failed": 0, "total": 0}, status=400)
        if not message:
            return web.json_response({"error": "message is required", "ok": False, "success": 0, "failed": 0, "total": 0}, status=400)
        
        # Проверка максимальной длины сообщения Telegram (4096 символов)
        if len(message) > 4096:
            return web.json_response({"error": "Message too long (max 4096 chars)", "ok": False, "success": 0, "failed": 0, "total": 0}, status=400)
        
        # Удаляем дубликаты chat_id
        unique_chat_ids = list(dict.fromkeys(chat_ids))
        
        errors = []
        success_count = 0
        
        # Отправляем сообщения последовательно для лучшего контроля ошибок
        for chat_id in unique_chat_ids:
            try:
                await client.send_message(chat_id, message)
                success_count += 1
                logger.info(f"✅ Сообщение отправлено в чат {chat_id}")
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Ошибка отправки в {chat_id}: {error_msg}")
                errors.append({
                    "chat_id": str(chat_id),
                    "error": error_msg
                })
        
        failed_count = len(errors)
        total_count = len(unique_chat_ids)
        
        result = {
            "ok": True,
            "success": success_count,
            "failed": failed_count,
            "total": total_count,
            "errors": errors
        }
        
        logger.info(f"Рассылка завершена: {success_count}/{total_count} успешно, {failed_count} ошибок")
        return web.json_response(result)
        
    except Exception as e:
        logger.error(f"Критическая ошибка handle_send: {e}")
        return web.json_response({"error": str(e), "ok": False, "success": 0, "failed": 0, "total": 0, "errors": []}, status=500)

async def handle_get_chats(request):
    """Получение списка чатов для Vercel"""
    worker_secret = request.headers.get("X-Worker-Secret")
    if not worker_secret or worker_secret != WORKER_SECRET:
        return web.json_response({"error": "Unauthorized", "ok": False, "chats": []}, status=401)
    
    try:
        dialogs = await client.get_dialogs()
        chats = []
        for dialog in dialogs:
            chat = dialog.chat
            # Безопасное определение типа чата
            is_broadcast = getattr(chat, "broadcast", False)
            is_megagroup = getattr(chat, "megagroup", False)
            
            if is_broadcast and not is_megagroup:
                chat_type = "channel"
            elif is_megagroup:
                chat_type = "group"
            else:
                chat_type = "private"
            
            # Безопасное получение title и username
            title = getattr(chat, "title", None) or getattr(chat, "username", "Unknown")
            username = getattr(chat, "username", None)
            
            chats.append({
                "id": str(chat.id),
                "title": title,
                "type": chat_type,
                "username": username,
            })
        
        logger.info(f"Получено {len(chats)} чатов")
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
    await client.start()
    
    # Проверка авторизации пользователя
    is_authorized = await client.is_user_authorized()
    if not is_authorized:
        logger.error("❌ SESSION_STRING невалидна или истекла!")
        logger.error("Сгенерируйте новую сессию через скрипт generate_session.py")
        sys.exit(1)
    
    logger.info("✅ Telegram клиент подключен и авторизован!")
    
    # Проверка подключения к Redis
    try:
        redis.ping()
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
