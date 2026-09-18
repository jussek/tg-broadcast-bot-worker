import os
import asyncio

from dotenv import load_dotenv

from telethon import TelegramClient
from telethon.sessions import StringSession


load_dotenv()


API_ID = int(
    os.getenv("API_ID")
)

API_HASH = os.getenv(
    "API_HASH"
)


SESSION_STRING = os.getenv(
    "TELEGRAM_SESSION_STRING"
)


async def create_client():

    if not SESSION_STRING:

        raise RuntimeError(
            "TELEGRAM_SESSION_STRING "
            "не найден."
        )

    client = TelegramClient(
        StringSession(
            SESSION_STRING
        ),
        API_ID,
        API_HASH
    )

    await client.connect()

    if not await client.is_user_authorized():

        await client.disconnect()

        raise RuntimeError(
            "Telegram session "
            "не авторизована."
        )

    return client


async def send_to_groups(
    client,
    message: str,
    group_ids: list[int]
):

    success = 0
    failed = 0

    results = []

    for group_id in group_ids:

        try:

            await client.send_message(
                group_id,
                message
            )

            success += 1

            results.append({
                "group_id": group_id,
                "status": "success"
            })

            print(
                f"Отправлено в {group_id}"
            )

        except Exception as e:

            failed += 1

            results.append({
                "group_id": group_id,
                "status": "failed",
                "error": str(e)
            })

            print(
                f"Ошибка {group_id}: {e}"
            )

        # 1 секунда между группами
        await asyncio.sleep(1)

    return {
        "success": success,
        "failed": failed,
        "results": results
    }