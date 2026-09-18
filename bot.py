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
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup

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


class TemplateForm(StatesGroup):
    name = State()
    message = State()


class GroupSetForm(StatesGroup):
    name = State()
    groups = State()


class BroadcastForm(StatesGroup):
    message = State()
    groups = State()
    interval = State()
    repeats = State()


def api_url(path: str) -> str:
    """Build a Vercel API URL or fail with a useful configuration error."""
    if not VERCEL_API_URL:
        raise RuntimeError("VERCEL_API_URL is not configured")
    return f"{VERCEL_API_URL.rstrip('/')}{path}"


def parse_chat_ids(value: str) -> list[int]:
    """Parse a comma- or space-separated list of Telegram chat IDs."""
    try:
        chat_ids = [int(item) for item in value.replace(",", " ").split()]
    except ValueError as exc:
        raise ValueError("Укажите числовые ID чатов через запятую.") from exc
    if not chat_ids:
        raise ValueError("Укажите хотя бы один ID чата.")
    return list(dict.fromkeys(chat_ids))


async def fetch_worker_chats() -> list[dict]:
    """Return chats from the API proxy, then directly from the Worker if needed."""
    headers = {"X-Worker-Secret": os.getenv("WORKER_SECRET", "")}
    proxy_error: Exception | None = None
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(api_url("/chats"), headers=headers) as response:
                data = await response.json(content_type=None)
                if response.status == 200 and data.get("ok", False):
                    return data.get("chats", [])
                proxy_error = RuntimeError(
                    data.get("detail") or data.get("error") or f"API returned {response.status}"
                )
        except Exception as exc:
            proxy_error = exc

        worker_url = os.getenv("TELEGRAM_WORKER_URL", "").rstrip("/")
        if worker_url:
            try:
                async with session.get(f"{worker_url}/chats", headers=headers) as response:
                    data = await response.json(content_type=None)
                    if response.status == 200 and data.get("ok", False):
                        logger.warning("Using direct Worker fallback after API proxy failure: %s", proxy_error)
                        return data.get("chats", [])
                    raise RuntimeError(data.get("error") or f"Worker returned {response.status}")
            except Exception as direct_error:
                raise RuntimeError(
                    f"Worker is unavailable through both API and direct URL: {direct_error}"
                ) from direct_error

    raise RuntimeError(f"Telegram Worker is unavailable: {proxy_error}")


def format_chats(chats: list[dict]) -> str:
    """Format a short list of chats to help users choose IDs."""
    return "\n".join(
        f"• {chat.get('title', 'Без названия')} — `{chat.get('id')}`"
        for chat in chats[:20]
    )


async def get_user_from_db(user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Get or create user via Vercel API."""
    if not VERCEL_API_URL:
        logger.warning("VERCEL_API_URL not configured")
        return None
        
    try:
        async with aiohttp.ClientSession() as session:
            # Try to get existing user
            async with session.get(api_url(f"/users/{user_id}")) as resp:
                if resp.status == 200:
                    return await resp.json()
            
            # Create new user if not exists
            user_data = {
                "user_id": user_id,
                "username": username,
                "first_name": first_name
            }
            async with session.post(api_url("/users"), json=user_data) as resp:
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


    @dp.message(Command("cancel"))
    async def cmd_cancel(message: types.Message, state: FSMContext):
        """Cancel an in-progress creation flow."""
        await state.clear()
        await message.answer("Действие отменено.")


    @dp.message(Command("new_template"))
    async def cmd_new_template(message: types.Message, state: FSMContext):
        """Start interactive template creation."""
        await state.set_state(TemplateForm.name)
        await message.answer("Введите название шаблона. Для отмены используйте /cancel.")


    @dp.message(TemplateForm.name)
    async def template_name(message: types.Message, state: FSMContext):
        name = (message.text or "").strip()
        if not name:
            await message.answer("Название не должно быть пустым.")
            return
        await state.update_data(name=name)
        await state.set_state(TemplateForm.message)
        await message.answer("Введите текст сообщения для шаблона.")


    @dp.message(TemplateForm.message)
    async def template_message(message: types.Message, state: FSMContext):
        text = (message.text or "").strip()
        if not text:
            await message.answer("Текст сообщения не должен быть пустым.")
            return
        data = await state.get_data()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url(f"/users/{message.from_user.id}/templates"),
                    json={"name": data["name"], "message": text},
                ) as response:
                    if response.status not in (200, 201):
                        raise RuntimeError(f"API returned {response.status}")
            await state.clear()
            await message.answer(f"✅ Шаблон «{data['name']}» создан.")
        except Exception as exc:
            logger.error("Error creating template: %s", exc)
            await message.answer("❌ Не удалось создать шаблон. Проверьте подключение к базе данных.")


    @dp.message(Command("new_group_set"))
    async def cmd_new_group_set(message: types.Message, state: FSMContext):
        """Start interactive group-set creation."""
        try:
            chats = await fetch_worker_chats()
        except Exception as exc:
            logger.error("Error getting chats for group set: %s", exc)
            await message.answer("❌ Worker недоступен. Проверьте TELEGRAM_WORKER_URL и WORKER_SECRET.")
            return
        if not chats:
            await message.answer("Чаты не найдены. Добавьте аккаунт Worker в группы или проверьте его Telegram-сессию.")
            return
        await state.update_data(chats=chats)
        await state.set_state(GroupSetForm.name)
        await message.answer("Введите название набора групп.")


    @dp.message(GroupSetForm.name)
    async def group_set_name(message: types.Message, state: FSMContext):
        name = (message.text or "").strip()
        if not name:
            await message.answer("Название не должно быть пустым.")
            return
        data = await state.get_data()
        await state.update_data(name=name)
        await state.set_state(GroupSetForm.groups)
        await message.answer(
            "Отправьте ID чатов через запятую:\n" + format_chats(data["chats"])
        )


    @dp.message(GroupSetForm.groups)
    async def group_set_groups(message: types.Message, state: FSMContext):
        try:
            groups = parse_chat_ids(message.text or "")
            data = await state.get_data()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url(f"/users/{message.from_user.id}/group-sets"),
                    json={"name": data["name"], "groups": groups},
                ) as response:
                    if response.status not in (200, 201):
                        raise RuntimeError(f"API returned {response.status}")
            await state.clear()
            await message.answer(f"✅ Набор «{data['name']}» создан ({len(groups)} чатов).")
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
        except Exception as exc:
            logger.error("Error creating group set: %s", exc)
            await message.answer("❌ Не удалось создать набор групп. Проверьте подключение к базе данных.")


    @dp.message(Command("new_broadcast"))
    async def cmd_new_broadcast(message: types.Message, state: FSMContext):
        """Start interactive broadcast scheduling."""
        try:
            chats = await fetch_worker_chats()
        except Exception as exc:
            logger.error("Error getting chats for broadcast: %s", exc)
            await message.answer("❌ Worker недоступен. Проверьте TELEGRAM_WORKER_URL и WORKER_SECRET.")
            return
        if not chats:
            await message.answer("Чаты не найдены. Добавьте аккаунт Worker в группы или проверьте его Telegram-сессию.")
            return
        await state.update_data(chats=chats)
        await state.set_state(BroadcastForm.message)
        await message.answer("Введите текст рассылки. Для отмены используйте /cancel.")


    @dp.message(BroadcastForm.message)
    async def broadcast_message(message: types.Message, state: FSMContext):
        text = (message.text or "").strip()
        if not text:
            await message.answer("Текст сообщения не должен быть пустым.")
            return
        data = await state.get_data()
        await state.update_data(message=text)
        await state.set_state(BroadcastForm.groups)
        await message.answer("Отправьте ID чатов через запятую:\n" + format_chats(data["chats"]))


    @dp.message(BroadcastForm.groups)
    async def broadcast_groups(message: types.Message, state: FSMContext):
        try:
            await state.update_data(groups=parse_chat_ids(message.text or ""))
            await state.set_state(BroadcastForm.interval)
            await message.answer("Введите интервал между рассылками в минутах (например, 60).")
        except ValueError as exc:
            await message.answer(f"❌ {exc}")


    @dp.message(BroadcastForm.interval)
    async def broadcast_interval(message: types.Message, state: FSMContext):
        try:
            interval = int(message.text or "")
            if interval < 1:
                raise ValueError
        except ValueError:
            await message.answer("Введите целое число не меньше 1.")
            return
        await state.update_data(interval_minutes=interval)
        await state.set_state(BroadcastForm.repeats)
        await message.answer("Введите количество повторов (например, 1).")


    @dp.message(BroadcastForm.repeats)
    async def broadcast_repeats(message: types.Message, state: FSMContext):
        try:
            repeats = int(message.text or "")
            if repeats < 1:
                raise ValueError
            data = await state.get_data()
            payload = {
                "message": data["message"], "groups": data["groups"],
                "interval_minutes": data["interval_minutes"], "repeats": repeats,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url(f"/users/{message.from_user.id}/broadcast-tasks"), json=payload
                ) as response:
                    if response.status not in (200, 201):
                        raise RuntimeError(f"API returned {response.status}")
            await state.clear()
            await message.answer("✅ Рассылка создана и будет выполнена по расписанию.")
        except ValueError:
            await message.answer("Введите целое число не меньше 1.")
        except Exception as exc:
            logger.error("Error creating broadcast: %s", exc)
            await message.answer("❌ Не удалось создать рассылку. Проверьте подключение к базе данных.")
    
    
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
            chats = await fetch_worker_chats()
            if not chats:
                await callback.message.answer(
                    "⚠️ Worker подключён, но не вернул чатов. Добавьте Telegram-аккаунт Worker в группы "
                    "и проверьте TELEGRAM_SESSION_STRING."
                )
                return
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url(f"/users/{user_id}/sync-chats"), json={"chats": chats}
                ) as sync_response:
                    if sync_response.status in (200, 201):
                        await callback.message.answer(f"✅ Синхронизировано {len(chats)} чатов")
                    else:
                        await callback.message.answer("❌ Ошибка сохранения чатов")
        except Exception as e:
            logger.error(f"Error syncing chats: {e}")
            await callback.message.answer(
                "❌ Worker недоступен. Проверьте TELEGRAM_WORKER_URL и WORKER_SECRET."
            )
        
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
