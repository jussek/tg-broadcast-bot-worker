import os
import sys
import logging
import asyncio
from datetime import datetime, timedelta, timezone
import uuid
from aiohttp import web
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon import utils
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    PeerIdInvalidError,
    ChatAdminRequiredError,
    RPCError
)
from upstash_redis import Redis
from supabase import create_client

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def utc_now() -> str:
    """Return a PostgreSQL-compatible UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def utc_after_minutes(minutes: int) -> str:
    """Return a PostgreSQL-compatible UTC timestamp offset by minutes."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


# --- Получение переменных окружения ---
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
WORKER_SECRET = os.getenv("WORKER_SECRET")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# --- Проверка обязательных переменных ---
required_vars = {
    "TELEGRAM_API_ID": TELEGRAM_API_ID,
    "TELEGRAM_API_HASH": TELEGRAM_API_HASH,
    "TELEGRAM_SESSION_STRING": TELEGRAM_SESSION_STRING,
    "WORKER_SECRET": WORKER_SECRET,
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
}

missing_vars = [key for key, value in required_vars.items() if not value]

if missing_vars:
    logger.error(f"❌ Отсутствуют необходимые переменные окружения: {', '.join(missing_vars)}")
    logger.error("Проверьте настройки в панели Render (Environment Variables).")
    sys.exit(1)

# Преобразование API_ID в int
try:
    TELEGRAM_API_ID = int(TELEGRAM_API_ID)
except ValueError:
    logger.error("❌ TELEGRAM_API_ID должен быть числом!")
    sys.exit(1)

# --- Инициализация клиентов ---
if bool(UPSTASH_REDIS_REST_URL) != bool(UPSTASH_REDIS_REST_TOKEN):
    logger.error("Set both UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN, or neither.")
    sys.exit(1)

redis = (
    Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
    if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN
    else None
)

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

client = TelegramClient(
    StringSession(TELEGRAM_SESSION_STRING),
    api_id=TELEGRAM_API_ID,
    api_hash=TELEGRAM_API_HASH
)

# Глобальные флаги состояния
telegram_connected = False
telegram_authorized = False
scheduler_running = False


def acquire_task_lock(task_id: str, seconds: int = 90) -> bool:
    """Acquire Redis lock for task execution."""
    if redis is None:
        # A single persistent worker remains safe without Redis. Deployments
        # with multiple replicas must configure Redis for distributed locking.
        return True
    key = f"broadcast:lock:{task_id}"
    try:
        result = redis.set(key, "1", nx=True, ex=seconds)
        return bool(result)
    except Exception as e:
        logger.error(f"Error acquiring lock for task {task_id}: {e}")
        return False


def release_task_lock(task_id: str):
    """Release Redis lock for task."""
    if redis is None:
        return
    key = f"broadcast:lock:{task_id}"
    try:
        redis.delete(key)
    except Exception as e:
        logger.error(f"Error releasing lock for task {task_id}: {e}")


def get_due_broadcast_tasks():
    """Get active tasks that are due for execution from Supabase."""
    try:
        now = utc_now()
        response = supabase.table("broadcast_tasks")\
            .select("*")\
            .eq("status", "active")\
            .lte("next_run", now)\
            .execute()
        return response.data or []
    except Exception as e:
        logger.error(f"Error getting due tasks: {e}")
        return []


def update_broadcast_task(task_id: str, **kwargs):
    """Update broadcast task in Supabase."""
    try:
        kwargs["updated_at"] = utc_now()
        response = supabase.table("broadcast_tasks")\
            .update(kwargs)\
            .eq("id", task_id)\
            .execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error(f"Error updating task {task_id}: {e}")
        return None


def log_broadcast(task_id: str, user_id: int, message: str, groups: list, 
                  success_count: int, failed_count: int, errors: list):
    """Log broadcast attempt to Supabase."""
    try:
        log_id = str(uuid.uuid4())
        log_entry = {
            "id": log_id,
            "task_id": task_id,
            "user_id": user_id,
            "message": message[:1000],
            "groups": groups,
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors,
            "created_at": utc_now(),
        }
        supabase.table("broadcast_logs").insert(log_entry).execute()
        return log_entry
    except Exception as e:
        logger.error(f"Error logging broadcast: {e}")
        return None


async def send_message_to_chat(chat_id: str, message: str):
    """Send message to a single chat with error handling."""
    try:
        await client.send_message(chat_id, message)
        return {"success": True, "chat_id": chat_id, "error": None}
    except FloodWaitError as e:
        logger.warning(f"FloodWait for chat {chat_id}: wait {e.seconds} seconds")
        return {"success": False, "chat_id": chat_id, "error": f"FloodWait: {e.seconds}s"}
    except ChatWriteForbiddenError:
        logger.warning(f"ChatWriteForbiddenError for chat {chat_id}")
        return {"success": False, "chat_id": chat_id, "error": "ChatWriteForbidden"}
    except UserBannedInChannelError:
        logger.warning(f"UserBannedInChannelError for chat {chat_id}")
        return {"success": False, "chat_id": chat_id, "error": "UserBanned"}
    except PeerIdInvalidError:
        logger.warning(f"PeerIdInvalidError for chat {chat_id}")
        return {"success": False, "chat_id": chat_id, "error": "PeerIdInvalid"}
    except ChatAdminRequiredError:
        logger.warning(f"ChatAdminRequiredError for chat {chat_id}")
        return {"success": False, "chat_id": chat_id, "error": "AdminRequired"}
    except RPCError as e:
        logger.error(f"RPCError for chat {chat_id}: {e}")
        return {"success": False, "chat_id": chat_id, "error": f"RPCError: {e}"}
    except Exception as e:
        logger.error(f"Unexpected error for chat {chat_id}: {e}")
        return {"success": False, "chat_id": chat_id, "error": str(e)}


async def execute_broadcast_task(task):
    """Execute a single broadcast task."""
    task_id = task["id"]
    user_id = task["user_id"]
    message = task["message"]
    groups = task.get("groups", [])
    
    logger.info(f"📬 Starting broadcast task {task_id} for user {user_id}")
    
    # Acquire lock
    if not acquire_task_lock(task_id, seconds=120):
        logger.warning(f"Task {task_id} is already being executed (locked)")
        return
    
    try:
        # Remove duplicates
        unique_chat_ids = list(dict.fromkeys(groups))
        
        errors = []
        success_count = 0
        
        # Send messages
        for chat_id in unique_chat_ids:
            result = await send_message_to_chat(str(chat_id), message)
            if result["success"]:
                success_count += 1
                logger.info(f"✅ Message sent to chat {chat_id}")
            else:
                errors.append({
                    "chat_id": str(chat_id),
                    "error": result["error"]
                })
                logger.warning(f"❌ Failed to send to chat {chat_id}: {result['error']}")
        
        failed_count = len(errors)
        total_count = len(unique_chat_ids)
        
        # Log the broadcast
        log_broadcast(
            task_id=task_id,
            user_id=user_id,
            message=message,
            groups=groups,
            success_count=success_count,
            failed_count=failed_count,
            errors=errors
        )
        
        # Update task: increment completed_repeats
        completed_repeats = task.get("completed_repeats", 0) + 1
        total_repeats = task.get("total_repeats", 1)
        interval_minutes = task.get("interval_minutes", 60)
        
        if completed_repeats >= total_repeats:
            # Task completed
            new_status = "completed"
            next_run = None
            logger.info(f"✅ Task {task_id} completed ({completed_repeats}/{total_repeats})")
        else:
            # Schedule next run
            new_status = "active"
            next_run = utc_after_minutes(interval_minutes)
            logger.info(f"⏰ Task {task_id} scheduled for next run at {next_run}")
        
        update_broadcast_task(
            task_id=task_id,
            completed_repeats=completed_repeats,
            status=new_status,
            next_run=next_run
        )
        
        logger.info(f"Broadcast task {task_id} finished: {success_count}/{total_count} successful")
        
    except Exception as e:
        logger.error(f"Critical error executing task {task_id}: {e}")
        update_broadcast_task(task_id=task_id, last_error=str(e))
    finally:
        release_task_lock(task_id)


async def scheduler_loop():
    """Main scheduler loop - runs continuously on persistent worker."""
    global scheduler_running
    scheduler_running = True
    
    logger.info("🕐 Scheduler started")
    
    while scheduler_running:
        try:
            due_tasks = get_due_broadcast_tasks()
            
            if due_tasks:
                logger.info(f"Found {len(due_tasks)} due tasks")
                tasks = [execute_broadcast_task(task) for task in due_tasks]
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                logger.debug("No due tasks found")
            
            await asyncio.sleep(10)
            
        except asyncio.CancelledError:
            logger.info("Scheduler cancelled")
            break
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            await asyncio.sleep(5)


async def handle_send(request):
    """Handler for sending messages from Vercel."""
    worker_secret = request.headers.get("X-Worker-Secret")
    if not worker_secret or worker_secret != WORKER_SECRET:
        return web.json_response({"error": "Unauthorized", "ok": False, "success": 0, "failed": 0, "total": 0}, status=401)
    
    try:
        data = await request.json()
        chat_ids = data.get("chat_ids", [])
        message = data.get("message", "")
        
        if not isinstance(chat_ids, list) or not chat_ids:
            return web.json_response({"error": "chat_ids is required", "ok": False, "success": 0, "failed": 0, "total": 0}, status=400)
        if not isinstance(message, str) or not message:
            return web.json_response({"error": "message is required", "ok": False, "success": 0, "failed": 0, "total": 0}, status=400)
        
        if len(message) > 4096:
            return web.json_response({"error": "Message too long (max 4096 chars)", "ok": False, "success": 0, "failed": 0, "total": 0}, status=400)
        
        unique_chat_ids = list(dict.fromkeys(chat_ids))
        errors = []
        success_count = 0
        
        for chat_id in unique_chat_ids:
            result = await send_message_to_chat(chat_id, message)
            if result["success"]:
                success_count += 1
                logger.info(f"✅ Message sent to chat {chat_id}")
            else:
                errors.append({"chat_id": str(chat_id), "error": result["error"]})
                logger.error(f"Failed to send to {chat_id}: {result['error']}")
        
        failed_count = len(errors)
        total_count = len(unique_chat_ids)
        
        result = {
            "ok": True,
            "success": success_count,
            "failed": failed_count,
            "total": total_count,
            "errors": errors
        }
        
        logger.info(f"Broadcast finished: {success_count}/{total_count} successful")
        return web.json_response(result)
        
    except Exception as e:
        logger.error(f"Critical error in handle_send: {e}")
        return web.json_response({"error": str(e), "ok": False, "success": 0, "failed": 0, "total": 0, "errors": []}, status=500)


async def handle_get_chats(request):
    """Get chats list for Vercel."""
    worker_secret = request.headers.get("X-Worker-Secret")
    if not worker_secret or worker_secret != WORKER_SECRET:
        return web.json_response({"error": "Unauthorized", "ok": False, "chats": []}, status=401)
    
    try:
        dialogs = await client.get_dialogs()
        chats = [chat for dialog in dialogs if (chat := serialize_broadcast_dialog(dialog))]
        chats = []
        for dialog in dialogs:
            chat = dialog.chat
            is_broadcast = getattr(chat, "broadcast", False)
            is_megagroup = getattr(chat, "megagroup", False)

            # A broadcast worker must not expose private dialogs as destinations.
            # ``utils.get_peer_id`` creates Telegram's canonical marked ID
            # (for example, -100... for channels), preserving the peer type
            # when the ID is later passed back to Telethon.
            if not (is_broadcast or is_megagroup):
                continue
            
            if is_broadcast and not is_megagroup:
                chat_type = "channel"
            elif is_megagroup:
                chat_type = "group"
            else:
                chat_type = "private"
            
            title = getattr(chat, "title", None) or getattr(chat, "username", "Unknown")
            username = getattr(chat, "username", None)
            
            chats.append({
                "id": str(utils.get_peer_id(chat)),
                "title": title,
                "type": chat_type,
                "username": username,
            })
        
        logger.info(f"Got {len(chats)} chats")
        return web.json_response({"ok": True, "chats": chats})
    except Exception as e:
        logger.error(f"Error getting chats: {e}")
        return web.json_response({"error": str(e), "ok": False, "chats": []}, status=500)


def serialize_broadcast_dialog(dialog):
    """Return an API chat record for a broadcast destination, if applicable.

    Telethon's public ``Dialog`` API exposes the underlying entity as
    ``entity``.  Some older versions/examples use ``chat`` instead, so accept
    it as a compatibility fallback without assuming it exists.
    """
    chat = getattr(dialog, "entity", None) or getattr(dialog, "chat", None)
    if chat is None:
        logger.warning("Skipping dialog without a Telegram entity")
        return None

    is_broadcast = getattr(chat, "broadcast", False)
    is_megagroup = getattr(chat, "megagroup", False)
    is_group = bool(getattr(dialog, "is_group", False) or is_megagroup)
    is_channel = bool(getattr(dialog, "is_channel", False) or is_broadcast)
    if not (is_group or is_channel):
        return None

    return {
        # ``utils.get_peer_id`` creates Telegram's canonical marked ID (for
        # example, -100... for channels), preserving the peer type for sends.
        "id": str(utils.get_peer_id(chat)),
        "title": getattr(chat, "title", None) or getattr(chat, "username", "Unknown"),
        "type": "group" if is_group else "channel",
        "username": getattr(chat, "username", None),
    }


async def handle_health(request):
    """Health check endpoint with detailed status."""
    return web.json_response({
        "ok": telegram_connected and telegram_authorized and scheduler_running,
        "service": "telegram-worker",
        "telegram_connected": telegram_connected,
        "telegram_authorized": telegram_authorized,
        "scheduler_running": scheduler_running,
        "redis_configured": redis is not None
    })


async def on_startup(app):
    """Start Telegram client on startup."""
    global telegram_connected, telegram_authorized
    
    logger.info("🚀 Connecting Telegram client...")
    # Do not use ``client.start()`` here: when the saved StringSession is
    # invalid, it tries to run an interactive phone/password login. A hosted
    # worker has no stdin and then either hangs or fails with EOFError.
    await client.connect()
    
    telegram_authorized = await client.is_user_authorized()
    if not telegram_authorized:
        await client.disconnect()
        raise RuntimeError(
            "TELEGRAM_SESSION_STRING is invalid or expired. "
            "Generate a new session with generate_session.py."
        )
    
    telegram_connected = True
    logger.info("✅ Telegram client connected and authorized!")
    
    try:
        if redis is None:
            logger.warning("⚠️ Redis is not configured; task locking is limited to this worker instance.")
        else:
            redis.ping()
            logger.info("✅ Redis connected!")
    except Exception as e:
        logger.warning(f"⚠️ Redis connection error: {e}")
    
    try:
        supabase.table("users").select("count").limit(1).execute()
        logger.info("✅ Supabase connected!")
    except Exception as e:
        logger.warning(f"⚠️ Supabase connection error: {e}")
    
    logger.info("🕐 Starting scheduler loop...")
    app["scheduler_task"] = asyncio.create_task(scheduler_loop())


async def on_shutdown(app):
    """Stop client on shutdown."""
    global scheduler_running
    scheduler_running = False
    
    if "scheduler_task" in app:
        app["scheduler_task"].cancel()
        try:
            await app["scheduler_task"]
        except asyncio.CancelledError:
            pass
    
    logger.info("🛑 Stopping Telegram client...")
    await client.disconnect()
    logger.info("🛑 Worker stopped")


# --- Application setup ---
app = web.Application()
app.router.add_post('/send', handle_send)
app.router.add_get('/chats', handle_get_chats)
app.router.add_get('/health', handle_health)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8080))
    logger.info(f"🌐 Starting server on port {port}...")
    web.run_app(app, host='0.0.0.0', port=port)
