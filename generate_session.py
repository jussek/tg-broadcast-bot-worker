from telethon.sync import TelegramClient
from telethon.sessions import StringSession

# ВАШИ ДАННЫЕ (уже вставлены)
api_id = 32613699
api_hash = "6654f4c7713137d6f3f29e6148f9029d"

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

import asyncio
asyncio.run(main())
