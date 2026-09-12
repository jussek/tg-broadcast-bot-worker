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

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановка работы по сигналу пользователя")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
