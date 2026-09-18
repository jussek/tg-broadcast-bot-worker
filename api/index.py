import os
import json
import logging
import html
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from upstash_redis import Redis
from qstash import client as QStashClient
from telethon import TelegramClient

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
QSTASH_TOKEN = os.getenv("QSTASH_TOKEN")
APP_URL = os.getenv("APP_URL", "").rstrip("/")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
QSTASH_VERIFICATION_KEY = os.getenv("QSTASH_VERIFICATION_KEY") # Для проверки подписи QStash если нужно

if not all([BOT_TOKEN, API_ID, API_HASH, TELEGRAM_SESSION_STRING, UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN, QSTASH_TOKEN]):
    raise ValueError("Missing required environment variables")

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Init Clients ---
redis = Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
qstash = QStashClient(token=QSTASH_TOKEN)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage() # Для FSM контекста, состояние храним в Redis вручную
dp = Dispatcher(storage=storage)

# --- Telethon Helper ---
async def get_telethon_client() -> TelegramClient:
    client = TelegramClient(
        session="session_name", # Session name doesn't matter with string
        api_id=int(API_ID),
        api_hash=API_HASH,
        session_string=TELEGRAM_SESSION_STRING
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise Exception("Telethon user not authorized")
    return client

# --- Redis Helpers (State & Data) ---
def get_user_state_key(user_id: int) -> str:
    return f"broadcast:state:{user_id}"

def get_user_state(user_id: int) -> Optional[Dict[str, Any]]:
    data = redis.get(get_user_state_key(user_id))
    return json.loads(data) if data else None

def set_user_state(user_id: int, state: Dict[str, Any]):
    # Convert sets to lists for JSON serialization
    clean_state = {}
    for k, v in state.items():
        if isinstance(v, set):
            clean_state[k] = list(v)
        else:
            clean_state[k] = v
    redis.set(get_user_state_key(user_id), json.dumps(clean_state))

def clear_user_state(user_id: int):
    redis.delete(get_user_state_key(user_id))

def get_last_message_key(user_id: int) -> str:
    return f"broadcast:user:{user_id}:last_message"

def save_last_message(user_id: int, message_text: str):
    redis.set(get_last_message_key(user_id), message_text)

def get_last_message(user_id: int) -> Optional[str]:
    return redis.get(get_last_message_key(user_id))

# Task helpers
def get_task_key(task_id: str) -> str:
    return f"broadcast:task:{task_id}"

def get_task_lock_key(task_id: str) -> str:
    return f"broadcast:lock:{task_id}"

def acquire_task_lock(task_id: str, ttl: int = 60) -> bool:
    key = get_task_lock_key(task_id)
    # NX = only set if not exists, EX = expiration in seconds
    return redis.set(key, "locked", nx=True, ex=ttl) == 1

def release_task_lock(task_id: str):
    redis.delete(get_task_lock_key(task_id))

def get_task(task_id: str) -> Optional[Dict]:
    data = redis.get(get_task_key(task_id))
    return json.loads(data) if data else None

def save_task(task_id: str, task_data: Dict):
    redis.set(get_task_key(task_id), json.dumps(task_data))

# Template helpers
def get_templates_key(user_id: int) -> str:
    return f"broadcast:templates:{user_id}"

def get_user_templates(user_id: int) -> list:
    data = redis.get(get_templates_key(user_id))
    return json.loads(data) if data else []

def save_user_templates(user_id: int, templates: list):
    redis.set(get_templates_key(user_id), json.dumps(templates))

# --- Keyboards ---
def main_menu_keyboard():
    kb = [
        [InlineKeyboardButton(text="📨 Новая рассылка", callback_data="new_broadcast")],
        [InlineKeyboardButton(text="📚 Мои шаблоны", callback_data="templates")],
        [InlineKeyboardButton(text="🔁 Последнее сообщение", callback_data="last_message")],
        [InlineKeyboardButton(text="📊 Мои таймеры", callback_data="tasks")],
        [InlineKeyboardButton(text="📋 Мои группы", callback_data="groups")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
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

def groups_selection_keyboard(selected_ids: list, all_groups: list):
    kb = []
    for g in all_groups[:10]: # Limit to 10 for display
        gid = str(g.id)
        is_selected = gid in selected_ids
        symbol = "☑️" if is_selected else "☐"
        cb_data = f"toggle_group:{gid}"
        kb.append([InlineKeyboardButton(text=f"{symbol} {g.title}", callback_data=cb_data)])
    
    kb.append([InlineKeyboardButton(text="☑️ Выбрать все", callback_data="select_all_groups")])
    kb.append([InlineKeyboardButton(text="➡️ Продолжить", callback_data="continue_to_send")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def send_decision_keyboard():
    kb = [
        [InlineKeyboardButton(text="⚡ Отправить сейчас", callback_data="send_now")],
        [InlineKeyboardButton(text="⏰ Задать таймер", callback_data="schedule_task")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- Handlers ---

@dp.message_handler(commands=["start", "s"])
async def cmd_start(message: types.Message):
    await clear_user_state(message.from_user.id)
    text = "🤖 Панель рассылки\n\nВыбери действие:"
    await message.answer(text, reply_markup=main_menu_keyboard())

@dp.callback_query_handler(lambda c: c.data == "back_menu")
async def cb_back_menu(callback: types.CallbackQuery):
    await clear_user_state(callback.from_user.id)
    text = "🤖 Панель рассылки\n\nВыбери действие:"
    await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "new_broadcast")
async def cb_new_broadcast(callback: types.CallbackQuery):
    await callback.message.edit_text("📋 Выбор действия для новой рассылки:", reply_markup=broadcast_action_keyboard())
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "write_message")
async def cb_write_message(callback: types.CallbackQuery):
    await set_user_state(callback.from_user.id, {"step": "waiting_for_message"})
    await callback.message.edit_text("📝 Введите текст сообщения для рассылки:", reply_markup=cancel_keyboard())
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "use_last_message")
async def cb_use_last_message(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    last_msg = get_last_message(user_id)
    if not last_msg:
        await callback.answer("⚠️ Последнее сообщение не найдено.", show_alert=True)
        return
    
    await set_user_state(user_id, {"step": "selecting_groups", "message_text": last_msg, "selected_groups": []})
    await fetch_and_show_groups(callback, user_id)
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "select_template")
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

@dp.callback_query_handler(lambda c: c.data.startswith("use_template:"))
async def cb_use_template(callback: types.CallbackQuery):
    template_id = callback.data.split(":")[1]
    user_id = callback.from_user.id
    templates = get_user_templates(user_id)
    template = next((t for t in templates if t['id'] == template_id), None)
    
    if not template:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return

    await set_user_state(user_id, {"step": "selecting_groups", "message_text": template['message'], "selected_groups": []})
    await fetch_and_show_groups(callback, user_id)
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
        redis.set(f"broadcast:groups:{user_id}", json.dumps(group_data))
        
        state = get_user_state(user_id)
        selected = state.get("selected_groups", [])
        
        kb = groups_selection_keyboard(selected, dialogs)
        await callback.message.edit_text("📋 Выберите группы для рассылки:", reply_markup=kb)
    except Exception as e:
        logger.error(f"Error fetching groups: {e}")
        await callback.answer("Ошибка получения групп.", show_alert=True)

@dp.callback_query_handler(lambda c: c.data.startswith("toggle_group:"))
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
    await set_user_state(user_id, state)
    
    # Refresh keyboard
    group_data_json = redis.get(f"broadcast:groups:{user_id}")
    group_data = json.loads(group_data_json) if group_data_json else []
    # Reconstruct dummy objects for keyboard func
    dialogs = [type('obj', (object,), {'id': g['id'], 'title': g['title']}) for g in group_data]
    
    kb = groups_selection_keyboard(selected, dialogs)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "select_all_groups")
async def cb_select_all_groups(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    group_data_json = redis.get(f"broadcast:groups:{user_id}")
    group_data = json.loads(group_data_json) if group_data_json else []
    
    all_ids = [g['id'] for g in group_data]
    state = get_user_state(user_id)
    state["selected_groups"] = all_ids
    await set_user_state(user_id, state)
    
    dialogs = [type('obj', (object,), {'id': g['id'], 'title': g['title']}) for g in group_data]
    kb = groups_selection_keyboard(all_ids, dialogs)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "continue_to_send")
async def cb_continue_to_send(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    state = get_user_state(user_id)
    if not state.get("selected_groups"):
        await callback.answer("Выберите хотя бы одну группу.", show_alert=True)
        return
    
    await callback.message.edit_text("Как отправить сообщение?", reply_markup=send_decision_keyboard())
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "send_now")
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
        await clear_user_state(user_id)
        return

    # Save last message
    save_last_message(user_id, msg_text)
    await clear_user_state(user_id)
    
    await callback.message.edit_text(f"✅ Рассылка завершена\n\n📨 Успешно: {success_count}\n❌ Ошибок: {error_count}", reply_markup=main_menu_keyboard())

@dp.callback_query_handler(lambda c: c.data == "schedule_task")
async def cb_schedule_task(callback: types.CallbackQuery):
    # Simplified: Schedule for 1 minute later for demo
    user_id = callback.from_user.id
    state = get_user_state(user_id)
    msg_text = state.get("message_text")
    groups = state.get("selected_groups", [])
    
    if not msg_text or not groups:
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    import uuid
    task_id = str(uuid.uuid4())
    task_data = {
        "task_id": task_id,
        "user_id": user_id,
        "message": msg_text,
        "groups": groups,
        "completed_repeats": 0,
        "total_repeats": 1,
        "status": "active"
    }
    save_task(task_id, task_data)
    
    # Schedule QStash
    # Note: QStash needs a public URL. Using APP_URL env.
    target_url = f"{APP_URL}/api/process"
    
    try:
        qstash.publish_json(
            url=target_url,
            body={"task_id": task_id},
            delay="1m" # Demo delay
        )
        await clear_user_state(user_id)
        await callback.message.edit_text(f"⏰ Таймер установлен. Задача {task_id[:8]} выполнена через 1 мин.", reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error(f"QStash error: {e}")
        await callback.message.edit_text(f"❌ Ошибка планировщика: {e}")
    
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "cancel")
async def cb_cancel(callback: types.CallbackQuery):
    await clear_user_state(callback.from_user.id)
    await callback.message.edit_text("Отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "templates")
async def cb_templates(callback: types.CallbackQuery):
    await callback.message.edit_text("Функционал шаблонов в разработке (демо).", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "tasks")
async def cb_tasks(callback: types.CallbackQuery):
    await callback.message.edit_text("Функционал таймеров в разработке (демо).", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "groups")
async def cb_groups(callback: types.CallbackQuery):
    await callback.message.edit_text("Список ваших групп доступен при создании рассылки.", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "settings")
async def cb_settings(callback: types.CallbackQuery):
    await callback.message.edit_text("Настройки в разработке.", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "back_broadcast")
async def cb_back_broadcast(callback: types.CallbackQuery):
    await callback.message.edit_text("📋 Выбор действия для новой рассылки:", reply_markup=broadcast_action_keyboard())
    await callback.answer()

@dp.message_handler()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    state = get_user_state(user_id)
    
    if state and state.get("step") == "waiting_for_message":
        text = message.text or message.caption
        if not text:
            await message.answer("Пожалуйста, отправьте текст.")
            return
        
        # Escape HTML just in case, though Telethon handles it mostly
        safe_text = html.escape(text) 
        
        await set_user_state(user_id, {"step": "selecting_groups", "message_text": safe_text, "selected_groups": []})
        
        # Fetch groups
        try:
            client = await get_telethon_client()
            dialogs = []
            async for dialog in client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    dialogs.append(dialog)
            await client.disconnect()
            
            group_data = [{"id": str(d.id), "title": d.name} for d in dialogs]
            redis.set(f"broadcast:groups:{user_id}", json.dumps(group_data))
            
            kb = groups_selection_keyboard([], dialogs)
            await message.answer("📋 Выберите группы для рассылки:", reply_markup=kb)
        except Exception as e:
            logger.error(f"Error fetching groups: {e}")
            await message.answer("Ошибка получения групп.")
            await clear_user_state(user_id)
    else:
        await message.answer("Неизвестная команда. Нажмите /start")

# --- FastAPI App ---
app = FastAPI(title="Telegram Broadcast Bot")

@app.get("/")
async def root():
    return {"ok": True, "service": "telegram-broadcast-bot"}

@app.get("/health")
async def health_check():
    try:
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
    if x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret token")
    
    try:
        body = await request.json()
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
            # Reschedule if repeats left (logic simplified)
            task["status"] = "active"
            # Re-publish to QStash if needed for next repeat
            pass
            
        save_task(task_id, task)
        release_task_lock(task_id)
        
        return JSONResponse(content={"ok": True, "sent": success_count, "errors": error_count})
        
    except Exception as e:
        logger.error(f"Process error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.on_event("startup")
async def on_startup():
    if APP_URL:
        webhook_url = f"{APP_URL}/api/webhook"
        await bot.set_webhook(webhook_url, secret_token=TELEGRAM_WEBHOOK_SECRET)
        logger.info(f"Webhook set to {webhook_url}")
