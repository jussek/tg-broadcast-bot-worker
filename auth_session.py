import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

api_id_raw = os.getenv("API_ID")
api_hash = os.getenv("API_HASH")
phone = os.getenv("PHONE")

if not api_id_raw or not api_hash or not phone:
    raise SystemExit(
        "Заполни API_ID, API_HASH и PHONE в файле .env"
    )

try:
    api_id = int(api_id_raw)
except ValueError:
    raise SystemExit("API_ID должен быть числом.")

async def main():
    client = TelegramClient(StringSession(), api_id, api_hash)

    print("Подключаемся к Telegram...")
    await client.start(phone=phone)

    me = await client.get_me()

    print()
    print("Авторизация успешна!")
    print(f"Аккаунт: {me.first_name or ''} {me.last_name or ''}".strip())
    print(f"Username: @{me.username}" if me.username else "Username: отсутствует")
    print(f"ID: {me.id}")
    print()

    session_string = client.session.save()

    Path("session_string.txt").write_text(
        session_string,
        encoding="utf-8"
    )

    print("SESSION STRING сохранён в:")
    print(Path("session_string.txt").resolve())
    print()
    print("ВАЖНО: никому не отправляй этот файл и сам SESSION STRING.")
    print("Он даёт доступ к авторизованной Telegram-сессии.")

    await client.disconnect()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
