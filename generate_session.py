#!/usr/bin/env python3
"""
Скрипт для генерации новой сессии Telethon
Запустите локально (не на сервере), чтобы получить свежий SESSION_STRING
"""

import os
import sys
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

# Замените на ваши API credentials из https://my.telegram.org
API_ID = input("Введите TELEGRAM_API_ID: ").strip()
API_HASH = input("Введите TELEGRAM_API_HASH: ").strip()

if not API_ID or not API_HASH:
    print("❌ API_ID и API_HASH обязательны!")
    sys.exit(1)

try:
    API_ID = int(API_ID)
except ValueError:
    print("❌ API_ID должен быть числом!")
    sys.exit(1)

print("\n📱 Инициализация клиента...")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\n✅ Сессия успешно создана!")
    print("\n" + "="*60)
    print("ВАШ НОВЫЙ SESSION_STRING:")
    print("="*60)
    print(client.session.save())
    print("="*60)
    print("\n⚠️  Скопируйте эту строку и добавьте в переменную окружения:")
    print("    TELEGRAM_SESSION_STRING='<ваша_строка>'")
    print("\n💡 Советы по безопасности:")
    print("   1. Используйте эту сессию ТОЛЬКО в одном месте")
    print("   2. Не запускайте один и тот же сеанс с разных IP одновременно")
    print("   3. Для разработки используйте отдельную сессию")
    print("   4. После обновления сессии перезапустите ваше приложение")
