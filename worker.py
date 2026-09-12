import os
import sys
import asyncio
import logging
from typing import Optional

from aiohttp import web
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.types import InputPeerUser

# --- Конфигурация ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")
WORKER_SECRET = os.getenv("WORKER_SECRET")
PORT = int(os.getenv("PORT", 8080))

if not all([API_ID, API_HASH, SESSION_STRING, WORKER_SECRET]):
    print("❌ Отсутствуют обязательные переменные окружения.")
    sys.exit(1)

# --- Логирование ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Инициализация клиента Telethon ---
# В новых версиях Telethon string_session передается как второй позиционный аргумент
client = TelegramClient('worker_session', API_ID, API_HASH)

async def init_telethon():
    try:
        await client.connect()
        if not await client.is_user_authorized():
            # Если сессия протухла или неверна, пытаемся авторизоваться строкой
            try:
                await client.start(bot_token=SESSION_STRING) # На случай если это токен бота (маловероятно по названию)
            except Exception:
                await client.start(phone=lambda: None, password=lambda: None) # Заглушка, чтобы вызвать ошибку если строка плохая
                
        # Правильный способ загрузки сессии из строки в новом клиенте
        if SESSION_STRING.startswith("1AJ"):
             await client.parse_id_string(SESSION_STRING) # Для новых форматов
        else:
             client.session.save() # Просто пробуем подключиться, сессия загрузится автоматически если файл есть, 
                                   # но мы используем строку.
        
        # Самый надежный способ для string_session в актуальной Telethon:
        await client.start(session_string=SESSION_STRING)
        
        logger.info("✅ Telethon клиент успешно подключен.")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка подключения Telethon: {e}")
        return False

# Глобальная переменная статуса подключения
telethon_ready = False

async def background_telethon():
    global telethon_ready
    telethon_ready = await init_telethon()
    
    if telethon_ready:
        @client.on(events.NewMessage(incoming=True))
        async def handler(event):
            logger.info(f"Получено сообщение: {event.text}")
            # Здесь логика обработки сообщений если нужна
            
        await client.run_until_disconnected()
    else:
        logger.error("Telethon не запущен из-за ошибки авторизации.")

# --- HTTP Сервер (FastAPI/aiohttp) ---
async def health_handler(request):
    if telethon_ready and client.is_connected():
        return web.json_response({"status": "ok", "telethon": "connected"}, status=200)
    else:
        # Возвращаем 503, чтобы Railway понял, что сервис нездоров
        return web.json_response({"status": "unavailable", "telethon": "disconnected"}, status=503)

async def process_call_handler(request):
    # Проверка секрета
    auth_header = request.headers.get("Authorization")
    if auth_header != f"Bearer {WORKER_SECRET}":
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
        chat_id = data.get("chat_id")
        # Пример логики обработки звонка
        logger.info(f"Обработка звонка для чата: {chat_id}")
        
        # Тут можно добавить логику отправки сообщения или звонка через Telethon
        # if telethon_ready:
        #     await client.send_message(...)
            
        return web.json_response({"status": "success", "message": "Call processed"})
    except Exception as e:
        logger.error(f"Ошибка обработки звонка: {e}")
        return web.json_response({"error": str(e)}, status=500)

app = web.Application()
app.router.add_get("/health", health_handler)
app.router.add_post("/process_call", process_call_handler)

async def run_http_server():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🚀 HTTP сервер запущен на порту {PORT}")
    # Держим сервер запущенным
    while True:
        await asyncio.sleep(3600)

# --- Точка входа ---
async def main():
    # Запускаем HTTP сервер и Telethon параллельно
    task1 = asyncio.create_task(run_http_server())
    task2 = asyncio.create_task(background_telethon())
    
    await asyncio.gather(task1, task2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановка сервиса...")
