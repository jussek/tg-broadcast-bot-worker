"""Telegram Bot for Broadcast System using aiogram 3.x.

This module implements a Telegram bot that works with the Vercel API
to provide user interaction for the broadcast system.
"""
import os
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import ClientSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
VERCEL_API_URL = os.getenv("VERCEL_API_URL", "https://your-vercel-app.vercel.app")

if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is required. Set it in environment variables.")
    exit(1)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()


async def get_user_from_db(user_id: int, username: Optional[str] = None, first_name: Optional[str] = None):
    """Get or create user via Vercel API."""
    async with ClientSession() as session:
        try:
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
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.error(f"Error getting/creating user: {e}")
    
    return None


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


@dp.callback_query(lambda c: c.data == "templates")
async def cb_templates(callback: types.CallbackQuery):
    """Show templates menu."""
    user_id = callback.from_user.id
    
    async with ClientSession() as session:
        try:
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


@dp.callback_query(lambda c: c.data == "groups")
async def cb_groups(callback: types.CallbackQuery):
    """Show groups menu."""
    user_id = callback.from_user.id
    
    async with ClientSession() as session:
        try:
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


@dp.callback_query(lambda c: c.data == "broadcasts")
async def cb_broadcasts(callback: types.CallbackQuery):
    """Show broadcasts menu."""
    user_id = callback.from_user.id
    
    async with ClientSession() as session:
        try:
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


@dp.callback_query(lambda c: c.data == "history")
async def cb_history(callback: types.CallbackQuery):
    """Show history menu."""
    user_id = callback.from_user.id
    
    async with ClientSession() as session:
        try:
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


@dp.callback_query(lambda c: c.data == "sync_chats")
async def cb_sync_chats(callback: types.CallbackQuery):
    """Sync user chats from Telegram worker."""
    user_id = callback.from_user.id
    
    await callback.message.answer("🔄 Синхронизация чатов...")
    
    async with ClientSession() as session:
        try:
            # Get chats from worker via Vercel API
            async with session.get(f"{VERCEL_API_URL}/chats") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    chats = data.get("chats", [])
                    
                    # Sync to Supabase
                    async with session.post(
                        f"{VERCEL_API_URL}/users/{user_id}/sync-chats",
                        json={"chats": chats}
                    ) as sync_resp:
                        if sync_resp.status == 200:
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


async def main():
    """Main function to start the bot."""
    logger.info("Starting Telegram bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
