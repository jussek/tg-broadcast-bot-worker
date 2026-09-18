import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv
import os

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")


async def main():
    session_string = open("session_string.txt", "r", encoding="utf-8").read().strip()

    client = TelegramClient(
        StringSession(session_string),
        API_ID,
        API_HASH
    )

    await client.connect()

    if not await client.is_user_authorized():
        print("❌ Сессия не авторизована")
        return

    me = await client.get_me()

    print()
    print("👤 Аккаунт:")
    print(f"Имя: {me.first_name}")
    print(f"Username: @{me.username}" if me.username else "Username отсутствует")
    print()

    print("📋 Группы, в которых состоит аккаунт:")
    print("-" * 50)

    count = 0

    async for dialog in client.iter_dialogs():

        if dialog.is_group:
            count += 1

            print(
                f"{count}. {dialog.name} "
                f"(ID: {dialog.id})"
            )

    print("-" * 50)
    print(f"Всего групп: {count}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())