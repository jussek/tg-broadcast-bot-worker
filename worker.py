import os
import asyncio
import logging
from telethon import TelegramClient
from telethon.sessions import StringSession
from fastapi import FastAPI, HTTPException, Depends, Header
from uvicorn import Config, Server
from typing import Optional

# Настройки логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Переменные окружения
from typing import Optional

from aiohttp import web
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.types import InputPeerUser

# --- Конфигурация ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")
WORKER_SECRET = os.getenv("WORKER_SECRET")
PORT = int(os.getenv("PORT", 8080))

if not all([API_ID, API_HASH, SESSION_STRING, WORKER_SECRET]):
    logger.error("Отсутствуют необходимые переменные окружения!")
    exit(1)

# Инициализация клиента Telethon
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# Инициализация FastAPI
app = FastAPI(title="Telegram Voice Worker")

async def verify_secret(x_worker_secret: Optional[str] = Header(None)):
    if x_worker_secret != WORKER_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    return True

@app.get("/health")
async def health_check():
    """Проверка здоровья для Railway"""
    try:
        is_connected = client.is_connected()
    except Exception:
        is_connected = False
    
    if not is_connected:
        # Возвращаем 503, если клиент не подключен, чтобы Railway видел проблему
        raise HTTPException(status_code=503, detail="Telethon client not connected")
    
    return {"status": "healthy", "telethon_connected": True}

@app.post("/process_call", dependencies=[Depends(verify_secret)])
async def process_call(data: dict):
    """Эндпоинт для приема задачи на звонок"""
    logger.info(f"Получена задача на звонок: {data}")
    # TODO: Добавить логику звонка здесь
    return {"status": "accepted", "message": "Call processing started"}

async def run_telethon_client():
    """Запуск клиента Telethon в фоне"""
    logger.info("Запуск клиента Telethon...")
    try:
        await client.start()
        logger.info("Клиент Telethon успешно запущен.")
        
        # Проверяем подключение после старта
        if not client.is_connected():
            logger.error("Клиент Telethon не смог подключиться после start()")
            return
        
        # Держим соединение живым, пока работает сервер
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Ошибка при запуске Telethon: {e}", exc_info=True)
        # Не завершаем процесс полностью, даём шанс на рестарт или диагностику
        # Но healthcheck будет показывать ошибку

async def run_server():
    """Запуск HTTP сервера"""
    config = Config(app=app, host="0.0.0.0", port=PORT, log_level="info")
    server = Server(config=config)
    logger.info(f"HTTP сервер запущен на порту {PORT}")
    await server.serve()

async def main():
    """Одновременный запуск сервера и клиента"""
    # Запускаем сервер и клиент параллельно
    await asyncio.gather(
        run_server(),
        run_telethon_client()
    )
    print("❌ Отсутствуют обязательные переменные окружения.")
    sys.exit(1)

# --- Логирование ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Инициализация клиента Telethon ---
# Сессия восстанавливается из строки через StringSession, а не через
# несуществующий аргумент string_session.
client = TelegramClient(
    StringSession(SESSION_STRING),
    api_id=int(API_ID),
    api_hash=API_HASH
)

async def init_telethon():
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error("❌ Сессия невалидна или истекла (TELEGRAM_SESSION_STRING).")
            return False

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
        logger.info("Остановка работы по сигналу пользователя")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
        logger.info("Остановка сервиса...")
