import os
import json
import logging
import time
import uuid
import html
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command

from upstash_redis import Redis
from qstash import QStash
from telethon import TelegramClient
from telethon.sessions import StringSession

# Configure logging before it is used during module import.  Vercel sets
# VERCEL=1 (not "true"), so the previous ordering could crash cold starts.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
QSTASH_TOKEN = os.getenv("QSTASH_TOKEN")
APP_URL = os.getenv("APP_URL", "https://tg-broadcast-bot-worker.vercel.app").rstrip("/")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
QSTASH_VERIFICATION_KEY = os.getenv("QSTASH_VERIFICATION_KEY")

# Lazy initialization check - will be validated on first request in Vercel environment
IS_VERCEL_ENV = os.getenv("VERCEL", "").lower() in {"1", "true", "yes"}

if not IS_VERCEL_ENV and not all([BOT_TOKEN, API_ID, API_HASH, TELEGRAM_SESSION_STRING, UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN, QSTASH_TOKEN]):
    logger.warning("Missing required environment variables. App will fail on first request if not set.")

# --- Init Clients (Lazy Initialization for Local Testing) ---
# In Vercel, env vars are always set. For local testing, we defer initialization.
def get_redis():
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        raise RuntimeError("Redis credentials not set")
    return Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)

def get_qstash():
    if not QSTASH_TOKEN:
        raise RuntimeError("QStash token not set")
    return QStash(token=QSTASH_TOKEN)

def get_bot():
    if not BOT_TOKEN:
        raise RuntimeError("Bot token not set")
    return Bot(token=BOT_TOKEN)

# Initialize only if env vars are present (Vercel production)
# Keep all external clients lazy.  Constructing them during import makes a
# serverless cold start fail before FastAPI can report a useful error.
redis = None
qstash = None
bot = None

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Helper to ensure clients are initialized
def ensure_redis():
    global redis
    if redis is None:
        redis = get_redis()


def ensure_qstash():
    global qstash
    if qstash is None:
        qstash = get_qstash()


def ensure_bot():
    global bot
    if bot is None:
        bot = get_bot()


def ensure_clients():
    """Backward-compatible initializer for operations needing every client."""
    ensure_redis()
    ensure_qstash()
    ensure_bot()

# --- Telethon Helper ---
async def get_telethon_client() -> TelegramClient:
    if not API_ID or not API_HASH or not TELEGRAM_SESSION_STRING:
        raise RuntimeError("Telegram API credentials are not configured")
    client = TelegramClient(StringSession(TELEGRAM_SESSION_STRING), int(API_ID), API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise Exception("Telethon user not authorized")
    return client

# --- Redis Helpers (State & Data) ---
def get_user_state_key(user_id: int) -> str:
    return f"broadcast:state:{user_id}"

def get_user_state(user_id: int) -> Optional[Dict[str, Any]]:
    ensure_redis()
    data = redis.get(get_user_state_key(user_id))
    if not data:
        return None
    # Upstash clients may return a decoded object or the JSON string that was
    # stored with ``set`` depending on the client/runtime version.
    if isinstance(data, dict):
        return data
    if isinstance(data, bytes):
        data = data.decode()
    return json.loads(data)

def set_user_state(user_id: int, state: Dict[str, Any]):
    ensure_redis()
    # Convert sets to lists for JSON serialization
    clean_state = {}
    for k, v in state.items():
        if isinstance(v, set):
            clean_state[k] = list(v)
        else:
            clean_state[k] = v
    redis.set(get_user_state_key(user_id), json.dumps(clean_state))

def clear_user_state(user_id: int):
    ensure_redis()
    redis.delete(get_user_state_key(user_id))

def get_last_message_key(user_id: int) -> str:
    return f"broadcast:user:{user_id}:last_message"

def save_last_message(user_id: int, message_text: str):
    ensure_redis()
    redis.set(get_last_message_key(user_id), message_text)

def get_last_message(user_id: int) -> Optional[str]:
    ensure_redis()
    return redis.get(get_last_message_key(user_id))

# Task helpers
def get_task_key(task_id: str) -> str:
    return f"broadcast:task:{task_id}"

def get_task_lock_key(task_id: str) -> str:
    return f"broadcast:lock:{task_id}"

def acquire_task_lock(task_id: str, ttl: int = 60) -> bool:
    ensure_redis()
    key = get_task_lock_key(task_id)
    # NX = only set if not exists, EX = expiration in seconds
    result = redis.set(key, "locked", nx=True, ex=ttl)
    return result in (True, 1, "OK")

def release_task_lock(task_id: str):
    ensure_redis()
    redis.delete(get_task_lock_key(task_id))

def get_task(task_id: str) -> Optional[Dict]:
    ensure_redis()
    data = redis.get(get_task_key(task_id))
    return json.loads(data) if data else None

def save_task(task_id: str, task_data: Dict):
    ensure_redis()
    redis.set(get_task_key(task_id), json.dumps(task_data))

# Template helpers
def get_templates_key(user_id: int) -> str:
    return f"broadcast:templates:{user_id}"

def get_user_templates(user_id: int) -> list:
    ensure_redis()
    data = redis.get(get_templates_key(user_id))
    return json.loads(data) if data else []

def save_user_templates(user_id: int, templates: list):
    ensure_redis()
    redis.set(get_templates_key(user_id), json.dumps(templates))

def get_group_lists_key(user_id: int) -> str:
    return f"broadcast:group-lists:{user_id}"

def get_group_lists(user_id: int) -> list:
    ensure_redis()
    data = redis.get(get_group_lists_key(user_id))
    return json.loads(data) if data else []

def save_group_lists(user_id: int, group_lists: list):
    ensure_redis()
    redis.set(get_group_lists_key(user_id), json.dumps(group_lists, ensure_ascii=False))

def get_user_tasks_key(user_id: int) -> str:
    return f"broadcast:user:{user_id}:tasks"

def get_user_tasks(user_id: int) -> list:
    ensure_redis()
    task_ids = redis.smembers(get_user_tasks_key(user_id)) or []
    tasks = []
    for task_id in task_ids:
        if isinstance(task_id, bytes):
            task_id = task_id.decode()
        task = get_task(str(task_id))
        if task:
            tasks.append(task)
    return sorted(tasks, key=lambda task: task.get("created_at", 0), reverse=True)

# --- Keyboards ---
def main_menu_keyboard():
    kb = [
        [InlineKeyboardButton(text="📨 Новая рассылка", callback_data="new_broadcast")],
        [InlineKeyboardButton(text="📚 Мои шаблоны", callback_data="templates")],
        [InlineKeyboardButton(text="🔁 Последнее сообщение", callback_data="last_message")],
        [InlineKeyboardButton(text="📊 Мои таймеры", callback_data="tasks")],
        [InlineKeyboardButton(text="📋 Мои группы", callback_data="groups")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def broadcast_action_keyboard():
    kb = [
        [InlineKeyboardButton(text="📝 Написать сообщение", callback_data="write_message")],
        [InlineKeyboardButton(text="📚 Выбрать шаблон", callback_data="select_template")],
        [InlineKeyboardButton(text="🔁 Использовать последнее", callback_data="use_last_message")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def cancel_keyboard():
    kb = [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def groups_selection_keyboard(selected_ids: list, all_groups: list, page: int = 0):
    page_size = 20
    page_count = max(1, (len(all_groups) + page_size - 1) // page_size)
    page = max(0, min(page, page_count - 1))
    kb = []
    for g in all_groups[page * page_size:(page + 1) * page_size]:
        gid = str(g.id)
        is_selected = gid in selected_ids
        symbol = "☑️" if is_selected else "☐"
        cb_data = f"toggle_group:{gid}"
        title = getattr(g, "title", None) or getattr(g, "name", None) or gid
        kb.append([InlineKeyboardButton(text=f"{symbol} {title}", callback_data=cb_data)])
    
    if page_count > 1:
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(text="◀️", callback_data=f"groups_page:{page - 1}"))
        navigation.append(InlineKeyboardButton(text=f"Страница {page + 1}/{page_count}", callback_data="ignore"))
        if page < page_count - 1:
            navigation.append(InlineKeyboardButton(text="▶️", callback_data=f"groups_page:{page + 1}"))
        kb.append(navigation)
    kb.append([InlineKeyboardButton(text="☑️ Выбрать все", callback_data="select_all_groups")])
    kb.append([InlineKeyboardButton(text="➡️ Продолжить", callback_data="continue_to_send")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def group_lists_keyboard(group_lists: list, prefix: str, back_callback: str = "back_menu"):
    kb = [
        [InlineKeyboardButton(text=f"📋 {item['name']} ({len(item['groups'])})", callback_data=f"{prefix}:{item['id']}")]
        for item in group_lists
    ]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def group_source_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Выбрать каналы вручную", callback_data="select_live_groups")],
        [InlineKeyboardButton(text="📋 Использовать сохранённый список", callback_data="select_group_list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="new_broadcast")],
    ])

def send_decision_keyboard():
    kb = [
        [InlineKeyboardButton(text="⚡ Отправить сейчас", callback_data="send_now")],
        [InlineKeyboardButton(text="⏰ Задать таймер", callback_data="schedule_task")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def parse_positive_integer(value: Optional[str]) -> Optional[int]:
    """Return a positive integer entered by a user, or ``None`` if it is invalid."""
    try:
        number = int((value or "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


async def create_and_schedule_task(user_id: int, state: Dict[str, Any]) -> str:
    """Persist a broadcast task and publish its first QStash delivery."""
    interval_minutes = state["interval_minutes"]
    total_repeats = state["total_repeats"]
    task_id = str(uuid.uuid4())
    task_data = {
        "task_id": task_id,
        "user_id": user_id,
        "message": state["message_text"],
        "groups": state["selected_groups"],
        "interval_minutes": interval_minutes,
        "completed_repeats": 0,
        "total_repeats": total_repeats,
        "status": "active",
        "created_at": time.time(),
        "next_run": time.time() + interval_minutes * 60,
    }
    save_task(task_id, task_data)
    ensure_redis()
    redis.sadd(get_user_tasks_key(user_id), task_id)

    try:
        ensure_qstash()
        qstash.message.publish_json(
            url=f"{APP_URL}/api/process",
            body={"task_id": task_id},
            delay=f"{interval_minutes}m",
        )
    except Exception:
        task_data["status"] = "error"
        save_task(task_id, task_data)
        raise
    return task_id

# --- Handlers ---

@dp.message(Command("start", "s"))
async def cmd_start(message: types.Message):
    try:
        clear_user_state(message.from_user.id)
    except Exception:
        logger.exception("Unable to clear state for user %s", message.from_user.id)
    text = "🤖 Панель рассылки\n\nВыбери действие:"
    await message.answer(text, reply_markup=main_menu_keyboard())

@dp.callback_query(lambda c: c.data == "back_menu")
async def cb_back_menu(callback: types.CallbackQuery):
    clear_user_state(callback.from_user.id)
    text = "🤖 Панель рассылки\n\nВыбери действие:"
    await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "new_broadcast")
async def cb_new_broadcast(callback: types.CallbackQuery):
    await callback.message.edit_text("📋 Выбор действия для новой рассылки:", reply_markup=broadcast_action_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "write_message")
async def cb_write_message(callback: types.CallbackQuery):
    set_user_state(callback.from_user.id, {"step": "waiting_for_message"})
    await callback.message.edit_text("📝 Введите текст сообщения для рассылки:", reply_markup=cancel_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "use_last_message")
async def cb_use_last_message(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    last_msg = get_last_message(user_id)
    if not last_msg:
        await callback.answer("⚠️ Последнее сообщение не найдено.", show_alert=True)
        return
    
    set_user_state(user_id, {"step": "selecting_groups", "message_text": last_msg, "selected_groups": []})
    await callback.message.edit_text("Выберите источник каналов:", reply_markup=group_source_keyboard())
    await callback.answer()


@dp.callback_query(lambda c: c.data == "last_message")
async def cb_last_message(callback: types.CallbackQuery):
    """Open the last message flow from the main menu."""
    await cb_use_last_message(callback)

@dp.callback_query(lambda c: c.data == "select_template")
async def cb_select_template(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    templates = get_user_templates(user_id)
    if not templates:
        await callback.answer("У вас нет шаблонов.", show_alert=True)
        # Fallback to write message
        await cb_write_message(callback)
        return
    
    kb = []
    for t in templates:
        kb.append([InlineKeyboardButton(text=t['name'], callback_data=f"use_template:{t['id']}")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="new_broadcast")])
    
    await callback.message.edit_text("📚 Выберите шаблон:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("use_template:"))
async def cb_use_template(callback: types.CallbackQuery):
    template_id = callback.data.split(":")[1]
    user_id = callback.from_user.id
    templates = get_user_templates(user_id)
    template = next((t for t in templates if t['id'] == template_id), None)
    
    if not template:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return

    set_user_state(user_id, {"step": "selecting_groups", "message_text": template['message'], "selected_groups": []})
    await callback.message.edit_text("Выберите источник каналов:", reply_markup=group_source_keyboard())
    await callback.answer()


@dp.callback_query(lambda c: c.data == "select_live_groups")
async def cb_select_live_groups(callback: types.CallbackQuery):
    await fetch_and_show_groups(callback, callback.from_user.id)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "select_group_list")
async def cb_select_group_list(callback: types.CallbackQuery):
    group_lists = get_group_lists(callback.from_user.id)
    if not group_lists:
        await callback.answer("Сохранённых списков пока нет.", show_alert=True)
        return
    await callback.message.edit_text(
        "📋 Выберите список каналов для рассылки:",
        reply_markup=group_lists_keyboard(group_lists, "use_group_list", "new_broadcast"),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("use_group_list:"))
async def cb_use_group_list(callback: types.CallbackQuery):
    list_id = callback.data.split(":", 1)[1]
    group_list = next((item for item in get_group_lists(callback.from_user.id) if item["id"] == list_id), None)
    state = get_user_state(callback.from_user.id)
    if not group_list or not state or not state.get("message_text"):
        await callback.answer("Список или сообщение не найдены.", show_alert=True)
        return
    state["selected_groups"] = group_list["groups"]
    set_user_state(callback.from_user.id, state)
    await callback.message.edit_text(
        f"📋 Используется список «{group_list['name']}» ({len(group_list['groups'])} каналов).\n\nКак отправить сообщение?",
        reply_markup=send_decision_keyboard(),
    )
    await callback.answer()

async def fetch_and_show_groups(callback: types.CallbackQuery, user_id: int):
    try:
        client = await get_telethon_client()
        dialogs = []
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                dialogs.append(dialog)
        await client.disconnect()
        
        # Store full list in Redis temporarily (simplified for this example)
        # In prod, store IDs and names only
        group_data = [{"id": str(d.id), "title": d.name} for d in dialogs]
        ensure_redis()
        redis.set(f"broadcast:groups:{user_id}", json.dumps(group_data))
        
        state = get_user_state(user_id)
        selected = state.get("selected_groups", [])
        
        kb = groups_selection_keyboard(selected, dialogs, state.get("group_page", 0))
        await callback.message.edit_text("📋 Выберите группы для рассылки:", reply_markup=kb)
    except Exception as e:
        logger.error(f"Error fetching groups: {e}")
        await callback.answer("Ошибка получения групп.", show_alert=True)

@dp.callback_query(lambda c: c.data.startswith("toggle_group:"))
async def cb_toggle_group(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    gid = callback.data.split(":")[1]
    state = get_user_state(user_id)
    selected = state.get("selected_groups", [])
    
    if gid in selected:
        selected.remove(gid)
    else:
        selected.append(gid)
    
    state["selected_groups"] = selected
    set_user_state(user_id, state)
    
    # Refresh keyboard
    ensure_redis()
    group_data_json = redis.get(f"broadcast:groups:{user_id}")
    group_data = json.loads(group_data_json) if group_data_json else []
    # Reconstruct dummy objects for keyboard func
    dialogs = [type('obj', (object,), {'id': g['id'], 'title': g['title']}) for g in group_data]
    
    kb = groups_selection_keyboard(selected, dialogs, state.get("group_page", 0))
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "select_all_groups")
async def cb_select_all_groups(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    ensure_redis()
    group_data_json = redis.get(f"broadcast:groups:{user_id}")
    group_data = json.loads(group_data_json) if group_data_json else []
    
    all_ids = [g['id'] for g in group_data]
    state = get_user_state(user_id)
    state["selected_groups"] = all_ids
    set_user_state(user_id, state)
    
    dialogs = [type('obj', (object,), {'id': g['id'], 'title': g['title']}) for g in group_data]
    kb = groups_selection_keyboard(all_ids, dialogs, state.get("group_page", 0))
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("groups_page:"))
async def cb_groups_page(callback: types.CallbackQuery):
    page = int(callback.data.split(":", 1)[1])
    user_id = callback.from_user.id
    state = get_user_state(user_id)
    state["group_page"] = page
    set_user_state(user_id, state)
    ensure_redis()
    group_data_json = redis.get(f"broadcast:groups:{user_id}")
    group_data = json.loads(group_data_json) if group_data_json else []
    dialogs = [type('obj', (object,), {'id': group['id'], 'title': group['title']}) for group in group_data]
    await callback.message.edit_reply_markup(
        reply_markup=groups_selection_keyboard(state.get("selected_groups", []), dialogs, page)
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "ignore")
async def cb_ignore(callback: types.CallbackQuery):
    await callback.answer()

@dp.callback_query(lambda c: c.data == "continue_to_send")
async def cb_continue_to_send(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    state = get_user_state(user_id)
    if not state.get("selected_groups"):
        await callback.answer("Выберите хотя бы одну группу.", show_alert=True)
        return

    if state.get("step") == "selecting_group_list_channels":
        state["step"] = "waiting_for_group_list_name"
        set_user_state(user_id, state)
        await callback.message.edit_text("Введите название списка каналов:", reply_markup=cancel_keyboard())
        await callback.answer()
        return
    
    await callback.message.edit_text("Как отправить сообщение?", reply_markup=send_decision_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "send_now")
async def cb_send_now(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    state = get_user_state(user_id)
    msg_text = state.get("message_text")
    groups = state.get("selected_groups", [])
    
    if not msg_text or not groups:
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    await callback.message.edit_text("⏳ Отправка...")
    await callback.answer()
    
    success_count = 0
    error_count = 0
    
    try:
        client = await get_telethon_client()
        for gid in groups:
            try:
                # Telethon requires integer IDs usually, but string might work if formatted right. 
                # Assuming stored as string from dialog.id which can be negative.
                entity = await client.get_entity(int(gid))
                await client.send_message(entity, msg_text, parse_mode='html')
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to send to {gid}: {e}")
                error_count += 1
        await client.disconnect()
    except Exception as e:
        logger.error(f"Telethon error: {e}")
        await callback.message.edit_text(f"❌ Ошибка отправки: {str(e)}")
        clear_user_state(user_id)
        return

    # Save last message
    save_last_message(user_id, msg_text)
    clear_user_state(user_id)
    
    await callback.message.edit_text(f"✅ Рассылка завершена\n\n📨 Успешно: {success_count}\n❌ Ошибок: {error_count}", reply_markup=main_menu_keyboard())

@dp.callback_query(lambda c: c.data == "schedule_task")
async def cb_schedule_task(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    state = get_user_state(user_id)
    msg_text = state.get("message_text")
    groups = state.get("selected_groups", [])
    
    if not msg_text or not groups:
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    state["step"] = "waiting_for_interval"
    set_user_state(user_id, state)
    await callback.message.edit_text(
        "⏰ Введите интервал в минутах между отправками (целое число больше 0):",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "cancel")
async def cb_cancel(callback: types.CallbackQuery):
    clear_user_state(callback.from_user.id)
    await callback.message.edit_text("Отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "templates")
async def cb_templates(callback: types.CallbackQuery):
    templates = get_user_templates(callback.from_user.id)
    kb = [[InlineKeyboardButton(text="➕ Создать шаблон", callback_data="create_template")]]
    kb.extend([InlineKeyboardButton(text=f"📝 {item['name']}", callback_data=f"manage_template:{item['id']}")] for item in templates)
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")])
    await callback.message.edit_text("📚 Шаблоны сообщений:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()


@dp.callback_query(lambda c: c.data == "create_template")
async def cb_create_template(callback: types.CallbackQuery):
    set_user_state(callback.from_user.id, {"step": "waiting_for_template_name"})
    await callback.message.edit_text("Введите название шаблона:", reply_markup=cancel_keyboard())
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("manage_template:"))
async def cb_manage_template(callback: types.CallbackQuery):
    template_id = callback.data.split(":", 1)[1]
    template = next((item for item in get_user_templates(callback.from_user.id) if item["id"] == template_id), None)
    if not template:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_template:{template_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="templates")],
    ])
    await callback.message.edit_text(f"📝 <b>{template['name']}</b>\n\n{template['message']}", reply_markup=kb)
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("delete_template:"))
async def cb_delete_template(callback: types.CallbackQuery):
    template_id = callback.data.split(":", 1)[1]
    templates = [item for item in get_user_templates(callback.from_user.id) if item["id"] != template_id]
    save_user_templates(callback.from_user.id, templates)
    await cb_templates(callback)

def _task_status_label(status: str) -> str:
    return {
        "active": "🟢 Активен",
        "pending": "🟡 В очереди",
        "completed": "✅ Завершён",
        "cancelled": "🚫 Отменён",
        "error": "❌ Ошибка",
        "failed": "❌ Ошибка",
    }.get(status, f"⚪ {status or 'нет статуса'}")


def _format_task_details(task: Dict[str, Any]) -> str:
    task_id = task.get("task_id") or task.get("id") or "?"
    lines = [
        f"⏰ <b>Таймер {str(task_id)[:8]}</b>",
        f"Статус: {_task_status_label(task.get('status'))}",
        f"Интервал: {task.get('interval_minutes', 1)} мин.",
        f"Повторы: {task.get('completed_repeats', 0)}/{task.get('total_repeats', 1)}",
        f"Каналов: {len(task.get('groups', []))}",
    ]
    if task.get("status") == "active" and task.get("next_run"):
        lines.append(f"Следующий запуск: {time.strftime('%d.%m.%Y %H:%M UTC', time.gmtime(task['next_run']))}")
    message_preview = (task.get("message") or "").strip().replace("\n", " ")
    if message_preview:
        if len(message_preview) > 120:
            message_preview = message_preview[:120] + "…"
        lines.append(f"Сообщение: <i>{html.escape(message_preview)}</i>")
    return "\n".join(lines)


ACTIVE_TASK_STATUSES = ("active", "pending")


@dp.callback_query(lambda c: c.data == "tasks")
async def cb_tasks(callback: types.CallbackQuery):
    all_tasks = get_user_tasks(callback.from_user.id)
    # Показываем только действующие таймеры (активные и в очереди)
    tasks = [t for t in all_tasks if t.get("status") in ACTIVE_TASK_STATUSES]
    finished_count = len(all_tasks) - len(tasks)
    header_lines = ["📊 <b>Действующие таймеры</b>"]
    if not tasks:
        header_lines.append("\nУ вас нет действующих таймеров. Создайте новый через «📨 Новая рассылка» → «⏰ Задать таймер».")
        if finished_count:
            header_lines.append(f"Завершённых и отменённых таймеров в истории: {finished_count} (скрыты).")
    else:
        header_lines.append(f"\nДействующих: {len(tasks)}\n")
        for i, task in enumerate(tasks, 1):
            tid = str(task.get("task_id") or task.get("id") or "?")[:8]
            status = {"active": "🟢", "pending": "🟡"}.get(task.get("status"), "⚪")
            header_lines.append(
                f"{i}. {status} {tid} — {task.get('completed_repeats', 0)}/{task.get('total_repeats', 1)} повторов, "
                f"интервал {task.get('interval_minutes', 1)} мин."
            )
        if finished_count:
            header_lines.append(f"\nСкрыто завершённых и отменённых таймеров: {finished_count}.")
    kb = []
    for task in tasks:
        task_id = task.get("task_id") or task.get("id")
        if not task_id:
            continue
        row = [
            InlineKeyboardButton(text=f"ℹ️ {str(task_id)[:8]}", callback_data=f"task_details:{task_id}"),
            InlineKeyboardButton(text="🚫 Отменить", callback_data=f"cancel_task:{task_id}"),
        ]
        kb.append(row)
    kb.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="tasks")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")])
    await callback.message.edit_text(
        "\n".join(header_lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("task_details:"))
async def cb_task_details(callback: types.CallbackQuery):
    task_id = callback.data.split(":", 1)[1]
    task = get_task(task_id)
    if not task or int(task.get("user_id", -1)) != int(callback.from_user.id):
        await callback.answer("Таймер не найден.", show_alert=True)
        return
    kb = []
    if task.get("status") in ("active", "pending"):
        kb.append([InlineKeyboardButton(text="🚫 Отменить таймер", callback_data=f"cancel_task:{task_id}")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="tasks")])
    await callback.message.edit_text(
        _format_task_details(task),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("cancel_task:"))
async def cb_cancel_task(callback: types.CallbackQuery):
    task_id = callback.data.split(":", 1)[1]
    task = get_task(task_id)
    if not task or int(task.get("user_id", -1)) != int(callback.from_user.id):
        await callback.answer("Таймер не найден.", show_alert=True)
        return
    if task.get("status") not in ("active", "pending"):
        await callback.answer("Этот таймер уже не активен.", show_alert=True)
        return
    task["status"] = "cancelled"
    task["cancelled_at"] = time.time()
    save_task(task_id, task)
    await callback.answer("✅ Таймер отменён, рассылка остановлена.")
    await cb_tasks(callback)

@dp.callback_query(lambda c: c.data == "groups")
async def cb_groups(callback: types.CallbackQuery):
    group_lists = get_group_lists(callback.from_user.id)
    kb = [[InlineKeyboardButton(text="➕ Создать список", callback_data="create_group_list")]]
    kb.extend([InlineKeyboardButton(text=f"📋 {item['name']} ({len(item['groups'])})", callback_data=f"manage_group_list:{item['id']}")] for item in group_lists)
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")])
    await callback.message.edit_text("📋 Списки каналов для рассылки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()


@dp.callback_query(lambda c: c.data == "create_group_list")
async def cb_create_group_list(callback: types.CallbackQuery):
    set_user_state(callback.from_user.id, {"step": "selecting_group_list_channels", "selected_groups": []})
    await fetch_and_show_groups(callback, callback.from_user.id)
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("manage_group_list:"))
async def cb_manage_group_list(callback: types.CallbackQuery):
    list_id = callback.data.split(":", 1)[1]
    group_list = next((item for item in get_group_lists(callback.from_user.id) if item["id"] == list_id), None)
    if not group_list:
        await callback.answer("Список не найден.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить список", callback_data=f"delete_group_list:{list_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="groups")],
    ])
    await callback.message.edit_text(f"📋 <b>{group_list['name']}</b>\nКаналов в списке: {len(group_list['groups'])}", reply_markup=kb)
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("delete_group_list:"))
async def cb_delete_group_list(callback: types.CallbackQuery):
    list_id = callback.data.split(":", 1)[1]
    save_group_lists(callback.from_user.id, [item for item in get_group_lists(callback.from_user.id) if item["id"] != list_id])
    await cb_groups(callback)

@dp.callback_query(lambda c: c.data == "back_broadcast")
async def cb_back_broadcast(callback: types.CallbackQuery):
    await callback.message.edit_text("📋 Выбор действия для новой рассылки:", reply_markup=broadcast_action_keyboard())
    await callback.answer()

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    state = get_user_state(user_id)

    if state and state.get("step") == "waiting_for_template_name":
        name = (message.text or "").strip()
        if not name:
            await message.answer("Введите непустое название шаблона.", reply_markup=cancel_keyboard())
            return
        state["template_name"] = name
        state["step"] = "waiting_for_template_message"
        set_user_state(user_id, state)
        await message.answer("Введите текст шаблона:", reply_markup=cancel_keyboard())
        return

    if state and state.get("step") == "waiting_for_template_message":
        text = message.text or message.caption
        if not text:
            await message.answer("Отправьте текст шаблона.", reply_markup=cancel_keyboard())
            return
        templates = get_user_templates(user_id)
        templates.append({"id": str(uuid.uuid4()), "name": state["template_name"], "message": html.escape(text)})
        save_user_templates(user_id, templates)
        clear_user_state(user_id)
        await message.answer("✅ Шаблон сохранён.", reply_markup=main_menu_keyboard())
        return

    if state and state.get("step") == "waiting_for_group_list_name":
        name = (message.text or "").strip()
        if not name:
            await message.answer("Введите непустое название списка.", reply_markup=cancel_keyboard())
            return
        group_lists = get_group_lists(user_id)
        group_lists.append({"id": str(uuid.uuid4()), "name": name, "groups": state["selected_groups"]})
        save_group_lists(user_id, group_lists)
        clear_user_state(user_id)
        await message.answer(f"✅ Список «{name}» сохранён.", reply_markup=main_menu_keyboard())
        return

    if state and state.get("step") == "waiting_for_interval":
        interval_minutes = parse_positive_integer(message.text)
        if interval_minutes is None:
            await message.answer("Введите целое число минут больше 0.", reply_markup=cancel_keyboard())
            return

        state["interval_minutes"] = interval_minutes
        state["step"] = "waiting_for_repeats"
        set_user_state(user_id, state)
        await message.answer(
            "🔁 Введите количество отправок (целое число больше 0):",
            reply_markup=cancel_keyboard(),
        )
        return

    if state and state.get("step") == "waiting_for_repeats":
        total_repeats = parse_positive_integer(message.text)
        if total_repeats is None:
            await message.answer("Введите целое число повторов больше 0.", reply_markup=cancel_keyboard())
            return

        state["total_repeats"] = total_repeats
        try:
            task_id = await create_and_schedule_task(user_id, state)
        except Exception as e:
            logger.error(f"QStash error: {e}")
            await message.answer(f"❌ Ошибка планировщика: {e}", reply_markup=cancel_keyboard())
            return

        clear_user_state(user_id)
        await message.answer(
            f"⏰ Таймер установлен. Задача {task_id[:8]} начнётся через "
            f"{state['interval_minutes']} мин. и выполнится {total_repeats} раз.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if state and state.get("step") == "waiting_for_message":
        text = message.text or message.caption
        if not text:
            await message.answer("Пожалуйста, отправьте текст.")
            return
        
        # Escape HTML just in case, though Telethon handles it mostly
        safe_text = html.escape(text) 
        
        set_user_state(user_id, {"step": "selecting_groups", "message_text": safe_text, "selected_groups": []})
        
        await message.answer("Выберите источник каналов:", reply_markup=group_source_keyboard())
    else:
        await message.answer("Неизвестная команда. Нажмите /start")

# --- FastAPI App ---
# Keep the constructor assignment simple: Vercel discovers FastAPI entry points
# statically and expects a module-level ``app = FastAPI()`` declaration.
app = FastAPI()
app.title = "Telegram Broadcast Bot"

@app.get("/")
async def root():
    return {"ok": True, "service": "telegram-broadcast-bot"}

@app.get("/health")
async def health_check():
    try:
        ensure_redis()
        redis.ping()
        redis_status = "ok"
    except:
        redis_status = "error"
    
    return {
        "status": "ok",
        "redis": redis_status,
        "bot_configured": bool(BOT_TOKEN)
    }

@app.post("/api/webhook")
async def webhook_handler(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(None)):
    if TELEGRAM_WEBHOOK_SECRET and x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret token")
    
    try:
        body = await request.json()
        ensure_bot()
        update = Update(**body)
        await dp.feed_update(bot, update)
        return JSONResponse(content={"ok": True})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/process")
async def process_task(request: Request):
    # Basic QStash verification could go here if needed
    try:
        body = await request.json()
        task_id = body.get("task_id")
        
        if not task_id:
            raise HTTPException(status_code=400, detail="No task_id")
        
        # Acquire lock
        if not acquire_task_lock(task_id, ttl=60):
            logger.info(f"Task {task_id} already processing")
            return JSONResponse(content={"ok": True, "status": "already_processing"})
        
        task = get_task(task_id)
        if not task or task.get("status") != "active":
            release_task_lock(task_id)
            return JSONResponse(content={"ok": False, "error": "Task not found or inactive"})
        
        # Execute sending
        success_count = 0
        error_count = 0
        
        try:
            client = await get_telethon_client()
            for gid in task.get("groups", []):
                try:
                    entity = await client.get_entity(int(gid))
                    await client.send_message(entity, task.get("message"), parse_mode='html')
                    success_count += 1
                except Exception as e:
                    logger.error(f"Send error to {gid}: {e}")
                    error_count += 1
            await client.disconnect()
        except Exception as e:
            logger.error(f"Telethon execution error: {e}")
            task["status"] = "error"
            save_task(task_id, task)
            release_task_lock(task_id)
            return JSONResponse(content={"ok": False, "error": str(e)})
        
        # Update task
        task["completed_repeats"] = task.get("completed_repeats", 0) + 1
        if task["completed_repeats"] >= task.get("total_repeats", 1):
            task["status"] = "completed"
        else:
            task["status"] = "active"
            interval_minutes = task.get("interval_minutes", 1)
            task["next_run"] = time.time() + interval_minutes * 60
            try:
                ensure_qstash()
                qstash.message.publish_json(
                    url=f"{APP_URL}/api/process",
                    body={"task_id": task_id},
                    delay=f"{interval_minutes}m",
                )
            except Exception as e:
                task["status"] = "error"
                save_task(task_id, task)
                release_task_lock(task_id)
                logger.error(f"Unable to reschedule task {task_id}: {e}")
                return JSONResponse(content={"ok": False, "error": str(e)})
            
        save_task(task_id, task)
        release_task_lock(task_id)
        
        return JSONResponse(content={"ok": True, "sent": success_count, "errors": error_count})
        
    except Exception as e:
        logger.error(f"Process error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.on_event("startup")
async def on_startup():
    if BOT_TOKEN and APP_URL:
        ensure_bot()
        webhook_url = f"{APP_URL}/api/webhook"
        await bot.set_webhook(webhook_url, secret_token=TELEGRAM_WEBHOOK_SECRET)
        logger.info(f"Webhook set to {webhook_url}")
