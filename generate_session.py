import asyncio
import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

api_id_raw = os.getenv("API_ID")
api_hash = os.getenv("API_HASH")

if not api_id_raw or not api_hash:
    raise SystemExit("Заполни API_ID и API_HASH в файле .env")

try:
    api_id = int(api_id_raw)
except ValueError:
    raise SystemExit("API_ID должен быть числом.")

async def main():
    print("🔄 Подключение к Telegram...")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        # Отправит код подтверждения в приложение Telegram на вашем телефоне
        phone = input("Введите номер телефона (например, +79991234567): ")
        await client.send_code_request(phone)
        
        code = input("Введите код из приложения Telegram: ")
        await client.sign_in(phone, code)
        
        # Если включен 2FA (пароль облака), запросит его
        try:
            session_string = client.session.save()
            print("\n✅ УСПЕШНО! Ваша строка сессии:\n")
            print("="*60)
            print(session_string)
            print("="*60)
            print("\nСкопируйте эту строку и вставьте в файл .env в переменную TELEGRAM_SESSION_STRING")
        except Exception as e:
            print(f"Ошибка при сохранении сессии: {e}")

asyncio.run(main())
