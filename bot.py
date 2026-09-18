"""Telegram Bot for Broadcast System using aiogram 3.x with Webhook support.

This module implements a Telegram bot that works with the Vercel API
to provide user interaction for the broadcast system.

For Vercel deployment: use webhook mode via FastAPI endpoint.
For local/standalone deployment: use polling mode.
"""
import os
import logging
import asyncio
import sys
from typing import Optional, Dict, Any

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
VERCEL_API_URL = os.getenv("VERCEL_API_URL")
USE_POLLING = os.getenv("BOT_USE_POLLING", "false").lower() == "true"

# Initialize bot and dispatcher only if token is provided
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None

if TELEGRAM_BOT_TOKEN:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
else:
    logger.warning("TELEGRAM_BOT_TOKEN not set. Bot will not start.")


async def get_user_from_db(user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Get or create user via Vercel API."""
    if not VERCEL_API_URL:
        logger.warning("VERCEL_API_URL not configured")
        return None
        
    try:
        async with aiohttp.ClientSession() as session:
            # Try to get existing user
            async with session.get(f"{VERCEL_API_URL}/users/{user_id}") as resp:
                if resp.status == 200:
                    return await resp.json()
            
            # Create new user if not exists
            user_data = {
                "user_id": user_id,
                "username": username,
                "first_name": first_name
            }
            async with session.post(f"{VERCEL_API_URL}/users", json=user_data) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
    except Exception as e:
        logger.error(f"Error getting/creating user: {e}")
    
    return None


if bot and dp:
    # Register handlers only if bot is initialized
    
    @dp.message(CommandStart())
    async def cmd_start(message: types.Message):
        """Handle /start command."""
        user = message.from_user
        
        # Get or create user in database
        user_data = await get_user_from_db(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои шаблоны", callback_data="templates")],
            [InlineKeyboardButton(text="📊 Группы", callback_data="groups")],
            [InlineKeyboardButton(text="📬 Рассылки", callback_data="broadcasts")],
            [InlineKeyboardButton(text="📜 История", callback_data="history")],
            [InlineKeyboardButton(text="🔄 Обновить чаты", callback_data="sync_chats")],
        ])
        
        welcome_text = f"👋 Привет, {user.first_name}!\n\n"
        welcome_text += "Это система массовой рассылки Telegram.\n\n"
        welcome_text += "Вы можете:\n"
        welcome_text += "• Создавать шаблоны сообщений\n"
        welcome_text += "• Управлять наборами групп\n"
        welcome_text += "• Планировать рассылки\n"
        welcome_text += "• Просматривать историю\n\n"
        welcome_text += "Выберите действие:"
        
        await message.answer(welcome_text, reply_markup=keyboard)
    
    
    @dp.callback_query(F.data == "templates")
    async def cb_templates(callback: types.CallbackQuery):
        """Show templates menu."""
        user_id = callback.from_user.id
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{VERCEL_API_URL}/users/{user_id}/templates") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        templates = data.get("templates", [])
                        
                        if templates:
                            text = f"📋 Ваши шаблоны ({len(templates)}):\n\n"
                            for t in templates[:5]:
                                text += f"• {t.get('name', 'Без имени')}\n"
                            text += "\nИспользуйте команду /new_template для создания."
                        else:
                            text = "📋 У вас пока нет шаблонов.\n\n"
                            text += "Используйте команду /new_template для создания первого шаблона."
                        
                        await callback.message.answer(text)
                    else:
                        await callback.message.answer("❌ Ошибка получения шаблонов")
        except Exception as e:
            logger.error(f"Error getting templates: {e}")
            await callback.message.answer("❌ Ошибка получения шаблонов")
        
        await callback.answer()
    
    
    @dp.callback_query(F.data == "groups")
    async def cb_groups(callback: types.CallbackQuery):
        """Show groups menu."""
        user_id = callback.from_user.id
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{VERCEL_API_URL}/users/{user_id}/group-sets") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        group_sets = data.get("group_sets", [])
                        
                        if group_sets:
                            text = f"📊 Ваши наборы групп ({len(group_sets)}):\n\n"
                            for gs in group_sets[:5]:
                                groups_count = len(gs.get('groups', []))
                                text += f"• {gs.get('name', 'Без имени')} ({groups_count} групп)\n"
                            text += "\nИспользуйте команду /new_group_set для создания."
                        else:
                            text = "📊 У вас пока нет наборов групп.\n\n"
                            text += "Сначала синхронизируйте чаты командой /sync_chats,\n"
                            text += "затем создайте набор групп командой /new_group_set."
                        
                        await callback.message.answer(text)
                    else:
                        await callback.message.answer("❌ Ошибка получения наборов групп")
        except Exception as e:
            logger.error(f"Error getting group sets: {e}")
            await callback.message.answer("❌ Ошибка получения наборов групп")
        
        await callback.answer()
    
    
    @dp.callback_query(F.data == "broadcasts")
    async def cb_broadcasts(callback: types.CallbackQuery):
        """Show broadcasts menu."""
        user_id = callback.from_user.id
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{VERCEL_API_URL}/users/{user_id}/broadcast-tasks") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        tasks = data.get("tasks", [])
                        
                        active = [t for t in tasks if t.get('status') == 'active']
                        completed = [t for t in tasks if t.get('status') == 'completed']
                        
                        text = f"📬 Ваши рассылки:\n\n"
                        text += f"Активные: {len(active)}\n"
                        text += f"Завершённые: {len(completed)}\n\n"
                        
                        if active:
                            text += "Активные задачи:\n"
                            for t in active[:3]:
                                repeats = t.get('completed_repeats', 0)
                                total = t.get('total_repeats', 0)
                                text += f"• {repeats}/{total} выполнено\n"
                        
                        text += "\nИспользуйте команду /new_broadcast для создания рассылки."
                        
                        await callback.message.answer(text)
                    else:
                        await callback.message.answer("❌ Ошибка получения задач")
        except Exception as e:
            logger.error(f"Error getting broadcast tasks: {e}")
            await callback.message.answer("❌ Ошибка получения задач")
        
        await callback.answer()
    
    
    @dp.callback_query(F.data == "history")
    async def cb_history(callback: types.CallbackQuery):
        """Show history menu."""
        user_id = callback.from_user.id
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{VERCEL_API_URL}/users/{user_id}/logs?limit=10") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logs = data.get("logs", [])
                        
                        if logs:
                            text = f"📜 Последние рассылки ({len(logs)}):\n\n"
                            for log in logs[:5]:
                                success = log.get('success_count', 0)
                                failed = log.get('failed_count', 0)
                                text += f"✅ {success} | ❌ {failed}\n"
                        else:
                            text = "📜 У вас пока нет истории рассылок."
                        
                        await callback.message.answer(text)
                    else:
                        await callback.message.answer("❌ Ошибка получения истории")
        except Exception as e:
            logger.error(f"Error getting logs: {e}")
            await callback.message.answer("❌ Ошибка получения истории")
        
        await callback.answer()
    
    
    @dp.callback_query(F.data == "sync_chats")
    async def cb_sync_chats(callback: types.CallbackQuery):
        """Sync user chats from Telegram worker."""
        user_id = callback.from_user.id
        
        await callback.message.answer("🔄 Синхронизация чатов...")
        
        try:
            async with aiohttp.ClientSession() as session:
                # Get chats from worker via Vercel API
                headers = {"X-Worker-Secret": os.getenv("WORKER_SECRET", "")}
                async with session.get(f"{VERCEL_API_URL}/chats", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        chats = data.get("chats", [])
                        
                        # Sync to Supabase via API endpoint
                        async with session.post(
                            f"{VERCEL_API_URL}/users/{user_id}/sync-chats",
                            json={"chats": chats}
                        ) as sync_resp:
                            if sync_resp.status in (200, 201):
                                await callback.message.answer(
                                    f"✅ Синхронизировано {len(chats)} чатов"
                                )
                            else:
                                await callback.message.answer("❌ Ошибка сохранения чатов")
                    else:
                        await callback.message.answer("❌ Ошибка получения чатов от воркера")
        except Exception as e:
            logger.error(f"Error syncing chats: {e}")
            await callback.message.answer(f"❌ Ошибка синхронизации: {e}")
        
        await callback.answer()
    
    
    async def on_startup():
        """Called on bot startup."""
        logger.info("Telegram bot started")
        if bot and USE_POLLING:
            await bot.delete_webhook()
            logger.info("Webhook deleted, using polling mode")
    
    
    async def on_shutdown():
        """Called on bot shutdown."""
        logger.info("Telegram bot shutting down")
        if bot:
            await bot.session.close()
    
    
def setup_bot() -> Optional[Dispatcher]:
    """Register lifecycle handlers and return the dispatcher."""
    if dp:
        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)
    return dp


def get_bot() -> Optional[Bot]:
    """Get bot instance."""
    return bot


def get_dispatcher() -> Optional[Dispatcher]:
    """Get dispatcher instance."""
    return dp


async def run_polling() -> None:
    """Run the bot locally when webhook mode is not used."""
    dispatcher = setup_bot()
    if not bot or not dispatcher:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to start the bot")
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    if not USE_POLLING:
        logger.error("Set BOT_USE_POLLING=true to run bot.py locally; use webhook mode on Vercel.")
        sys.exit(1)
    try:
        asyncio.run(run_polling())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Telegram bot stopped")
