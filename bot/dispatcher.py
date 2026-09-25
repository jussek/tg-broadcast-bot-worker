"""Telegram bot dispatcher and initialization."""
import os
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = os.getenv("BOT_TOKEN")

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Lazy initialization - bot will be created when token is available
bot = None
if BOT_TOKEN:
    try:
        bot = Bot(
            token=BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
    except Exception:
        # Token validation will fail in local testing without real token
        # In Vercel, env vars are set properly
        pass
