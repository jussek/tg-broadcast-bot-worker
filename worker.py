import os
import sys
import asyncio
import signal
from telethon import TelegramClient, events
from aiohttp import web

# Получение переменных окружения
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")
WORKER_SECRET = os.getenv("WORKER_SECRET")
PORT = int(os.getenv("PORT", 8080))

if not all([API_ID, API_HASH, TELEGRAM_SESSION_STRING, WORKER_SECRET]):
    print("ERROR: Missing required environment variables!")
    print("Required: API_ID, API_HASH, TELEGRAM_SESSION_STRING, WORKER_SECRET")
    sys.exit(1)

# Инициализация клиента Telethon
client = TelegramClient(
    session="worker_session",
    api_id=int(API_ID),
    api_hash=API_HASH,
    string_session=TELEGRAM_SESSION_STRING
)

# Хранилище для активных задач
active_tasks = {}

async def health_check(request):
    """Проверка здоровья воркера"""
    return web.json_response({"status": "ok", "active_tasks": len(active_tasks)})

async def handle_broadcast(request):
    """Обработка запроса на рассылку"""
    try:
        data = await request.json()
        
        # Проверка секрета
        if data.get("secret") != WORKER_SECRET:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        message = data.get("message")
        chat_ids = data.get("chat_ids", [])
        
        if not message or not chat_ids:
            return web.json_response({"error": "Missing message or chat_ids"}, status=400)
        
        success_count = 0
        failed_count = 0
        errors = []
        
        for chat_id in chat_ids:
            try:
                await client.send_message(int(chat_id), message)
                success_count += 1
            except Exception as e:
                failed_count += 1
                errors.append({"chat_id": chat_id, "error": str(e)})
        
        return web.json_response({
            "status": "completed",
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors[:10]  # Ограничим количество ошибок в ответе
        })
        
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def on_shutdown(app):
    """Очистка при завершении работы"""
    await client.disconnect()

def create_app():
    """Создание веб-приложения"""
    app = web.Application()
    app.router.add_get("/health", health_check)
    app.router.add_post("/broadcast", handle_broadcast)
    app.on_shutdown.append(on_shutdown)
    return app

async def main():
    """Основная функция запуска"""
    # Подключение к Telegram
    await client.start()
    print(f"Worker started. Session: {client.session}")
    
    # Создание и запуск веб-сервера
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    print(f"Web server running on port {PORT}")
    
    # Ожидание сигналов завершения
    stop_event = asyncio.Event()
    
    def signal_handler():
        stop_event.set()
    
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)
    
    await stop_event()
    await runner.cleanup()
    print("Worker stopped")

if __name__ == "__main__":
    asyncio.run(main())
