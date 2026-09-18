"""FastAPI API entrypoint for Telegram Broadcast Bot.

This module provides the main API endpoints for the Vercel deployment.
It handles:
- Health checks
- User management
- Template management
- Group set management
- Broadcast task management
- Worker communication
- Telegram Bot Webhook
"""
import os
import logging
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Request
from pydantic import BaseModel, Field

# Import Supabase storage functions (primary data store)
from storage.supabase_storage import (
    get_or_create_user,
    get_user,
    update_user,
    create_template,
    get_template,
    get_user_templates,
    update_template as update_template_db,
    delete_template,
    create_group_set,
    get_group_set,
    get_user_group_sets,
    update_group_set as update_group_set_db,
    delete_group_set,
    create_broadcast_task,
    get_broadcast_task,
    get_user_broadcast_tasks,
    update_broadcast_task,
    delete_broadcast_task,
    log_broadcast,
    get_user_broadcast_logs,
    get_user_chats,
    sync_user_chats,
)


# =========================================================
# LOGGING CONFIGURATION
# =========================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =========================================================
# FASTAPI APPLICATION
# =========================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown events."""
    logger.info("Starting Telegram Broadcast Bot API")
    yield
    logger.info("Shutting down Telegram Broadcast Bot API")


app = FastAPI(
    title="Telegram Broadcast Bot API",
    description="API for managing Telegram broadcast campaigns",
    version="1.0.0",
    lifespan=lifespan,
)


# =========================================================
# PYDANTIC MODELS
# =========================================================

class UserCreate(BaseModel):
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=4096)
    groups: Optional[list] = None


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    message: Optional[str] = None
    groups: Optional[list] = None


class GroupSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    groups: Optional[list] = None


class GroupSetUpdate(BaseModel):
    name: Optional[str] = None
    groups: Optional[list] = None


class BroadcastTaskCreate(BaseModel):
    message: str = Field(min_length=1, max_length=4096)
    groups: list = Field(min_length=1)
    interval_minutes: int = Field(ge=1)
    repeats: int = Field(ge=1)


class MessageSend(BaseModel):
    chat_ids: list = Field(min_length=1)
    message: str = Field(min_length=1, max_length=4096)


# Telegram Webhook models
class TelegramWebhookUpdate(BaseModel):
    update_id: int
    message: Optional[dict] = None
    callback_query: Optional[dict] = None


# =========================================================
# HEALTH ENDPOINTS
# =========================================================

@app.get("/")
async def root():
    """Root endpoint with service information."""
    return {
        "ok": True,
        "service": "telegram-broadcast-api",
        "version": "1.0.0"
    }


@app.get("/health")
async def health_check():
    """Health check endpoint for Vercel and monitoring."""
    return {
        "ok": True,
        "service": "telegram-broadcast-api"
    }


# =========================================================
# USER ENDPOINTS
# =========================================================

@app.post("/users")
async def create_user_endpoint(user_data: UserCreate):
    """Create or get existing user."""
    try:
        user = get_or_create_user(
            user_id=user_data.user_id,
            username=user_data.username,
            first_name=user_data.first_name
        )
        return {"ok": True, "user": user}
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users/{user_id}")
async def get_user_endpoint(user_id: int):
    """Get user by ID."""
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True, "user": user}


# =========================================================
# TEMPLATE ENDPOINTS
# =========================================================

@app.post("/users/{user_id}/templates")
async def create_template_endpoint(user_id: int, template_data: TemplateCreate):
    """Create a new template."""
    try:
        template = create_template(
            user_id=user_id,
            name=template_data.name,
            message=template_data.message,
            groups=template_data.groups
        )
        return {"ok": True, "template": template}
    except Exception as e:
        logger.error(f"Error creating template: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users/{user_id}/templates")
async def get_user_templates_endpoint(user_id: int):
    """Get all templates for a user."""
    templates = get_user_templates(user_id)
    return {"ok": True, "templates": templates}


@app.get("/templates/{template_id}")
async def get_template_endpoint(template_id: str):
    """Get template by ID."""
    template = get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"ok": True, "template": template}


@app.put("/templates/{template_id}")
async def update_template_endpoint(template_id: str, template_data: TemplateUpdate, x_user_id: int = Header(...)):
    """Update template."""
    try:
        template = update_template_db(
            user_id=x_user_id,
            template_id=template_id,
            name=template_data.name,
            message=template_data.message,
            groups=template_data.groups
        )
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        return {"ok": True, "template": template}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating template: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/templates/{template_id}")
async def delete_template_endpoint(template_id: str, x_user_id: int = Header(...)):
    """Delete template."""
    try:
        success = delete_template(user_id=x_user_id, template_id=template_id)
        if not success:
            raise HTTPException(status_code=404, detail="Template not found")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting template: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================
# GROUP SET ENDPOINTS
# =========================================================

@app.post("/users/{user_id}/group-sets")
async def create_group_set_endpoint(user_id: int, group_set_data: GroupSetCreate):
    """Create a new group set."""
    try:
        group_set = create_group_set(
            user_id=user_id,
            name=group_set_data.name,
            groups=group_set_data.groups
        )
        return {"ok": True, "group_set": group_set}
    except Exception as e:
        logger.error(f"Error creating group set: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users/{user_id}/group-sets")
async def get_user_group_sets_endpoint(user_id: int):
    """Get all group sets for a user."""
    group_sets = get_user_group_sets(user_id)
    return {"ok": True, "group_sets": group_sets}


@app.put("/group-sets/{set_id}")
async def update_group_set_endpoint(set_id: str, group_set_data: GroupSetUpdate, x_user_id: int = Header(...)):
    """Update group set."""
    try:
        group_set = update_group_set_db(
            user_id=x_user_id,
            set_id=set_id,
            name=group_set_data.name,
            groups=group_set_data.groups
        )
        if not group_set:
            raise HTTPException(status_code=404, detail="Group set not found")
        return {"ok": True, "group_set": group_set}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating group set: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/group-sets/{set_id}")
async def delete_group_set_endpoint(set_id: str, x_user_id: int = Header(...)):
    """Delete group set."""
    try:
        success = delete_group_set(user_id=x_user_id, set_id=set_id)
        if not success:
            raise HTTPException(status_code=404, detail="Group set not found")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting group set: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================
# BROADCAST TASK ENDPOINTS
# =========================================================

@app.post("/users/{user_id}/broadcast-tasks")
async def create_broadcast_task_endpoint(user_id: int, task_data: BroadcastTaskCreate):
    """Create a new broadcast task."""
    try:
        task = create_broadcast_task(
            user_id=user_id,
            message=task_data.message,
            groups=task_data.groups,
            interval_minutes=task_data.interval_minutes,
            repeats=task_data.repeats
        )
        return {"ok": True, "task": task}
    except Exception as e:
        logger.error(f"Error creating broadcast task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users/{user_id}/broadcast-tasks")
async def get_user_broadcast_tasks_endpoint(user_id: int, status: Optional[str] = None):
    """Get all broadcast tasks for a user."""
    tasks = get_user_broadcast_tasks(user_id, status=status)
    return {"ok": True, "tasks": tasks}


@app.delete("/broadcast-tasks/{task_id}")
async def delete_broadcast_task_endpoint(task_id: str, x_user_id: int = Header(...)):
    """Delete broadcast task."""
    try:
        success = delete_broadcast_task(user_id=x_user_id, task_id=task_id)
        if not success:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting broadcast task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================
# MESSAGE SENDING ENDPOINTS
# =========================================================

@app.post("/send")
async def send_message_endpoint(message_data: MessageSend, x_worker_secret: Optional[str] = Header(None)):
    """Send message to chats via Worker."""
    worker_secret = os.getenv("WORKER_SECRET")
    
    # Validate worker secret if provided
    if worker_secret and x_worker_secret != worker_secret:
        raise HTTPException(status_code=401, detail="Invalid worker secret")
    
    try:
        result = await worker_send_message(
            chat_ids=message_data.chat_ids,
            message=message_data.message
        )
        if not result.get("ok", False):
            raise HTTPException(status_code=502, detail=result.get("error", "Worker rejected the message"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/chats")
async def get_chats_endpoint(x_worker_secret: Optional[str] = Header(None)):
    """Get chats from Worker."""
    worker_secret = os.getenv("WORKER_SECRET")
    
    # Validate worker secret if provided
    if worker_secret and x_worker_secret != worker_secret:
        raise HTTPException(status_code=401, detail="Invalid worker secret")
    
    try:
        chats = await worker_get_chats()
        return {"ok": True, "chats": chats}
    except Exception as e:
        logger.error(f"Error getting chats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================
# LOGS ENDPOINTS
# =========================================================

@app.get("/users/{user_id}/logs")
async def get_user_logs_endpoint(user_id: int, limit: int = 50):
    """Get broadcast logs for a user."""
    logs = get_user_broadcast_logs(user_id, limit=limit)
    return {"ok": True, "logs": logs}


# =========================================================
# CHAT SYNC ENDPOINT
# =========================================================

@app.post("/users/{user_id}/sync-chats")
async def sync_user_chats_endpoint(user_id: int, request: Request):
    """Sync user's chat list from Worker to Supabase."""
    try:
        data = await request.json()
        chats = data.get("chats", [])
        
        if not chats:
            return {"ok": True, "count": 0}
        
        # Sync to Supabase
        count = sync_user_chats(user_id=user_id, chats=chats)
        return {"ok": True, "count": count, "chats": chats}
    except Exception as e:
        logger.error(f"Error syncing chats for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================
# TELEGRAM WEBHOOK ENDPOINT (aiogram integration)
# =========================================================

@app.api_route("/api", methods=["POST"])
async def telegram_webhook_api(request: Request):
    """Handle Telegram webhook updates via aiogram Dispatcher.
    
    This is the main webhook endpoint that Telegram calls.
    It receives updates from Telegram and passes them to the aiogram Dispatcher.
    """
    try:
        # Import bot and dispatcher lazily to avoid initialization issues
        from bot import get_bot, get_dispatcher
        
        bot_instance = get_bot()
        dispatcher = get_dispatcher()
        
        if not bot_instance or not dispatcher:
            logger.warning("Bot or Dispatcher not initialized (missing TELEGRAM_BOT_TOKEN)")
            return {"ok": False, "error": "Bot not initialized"}
        
        # Parse incoming update
        body = await request.json()
        logger.debug(f"Received Telegram update: {body.get('update_id')}")
        
        # Convert to aiogram Update object and feed to dispatcher
        from aiogram.types import Update
        update = Update(**body)
        
        # Process update through dispatcher
        await dispatcher.feed_update(bot_instance, update)
        
        return {"ok": True}
        
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return {"ok": False, "error": str(e)}


@app.api_route("/telegram-webhook", methods=["POST"])
async def telegram_webhook(request: Request):
    """Alternative webhook endpoint path."""
    return await telegram_webhook_api(request)


@app.post("/set-webhook")
async def set_webhook_endpoint():
    """Set Telegram webhook URL."""
    from bot import get_bot
    
    bot_instance = get_bot()
    if not bot_instance:
        raise HTTPException(status_code=500, detail="Bot not initialized")
    
    webhook_url = os.getenv("VERCEL_API_URL")
    if not webhook_url:
        raise HTTPException(status_code=400, detail="VERCEL_API_URL not set")
    
    webhook_url = f"{webhook_url}/telegram-webhook"
    
    try:
        await bot_instance.set_webhook(webhook_url)
        return {"ok": True, "webhook_url": webhook_url}
    except Exception as e:
        logger.error(f"Error setting webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/delete-webhook")
async def delete_webhook_endpoint():
    """Delete Telegram webhook."""
    from bot import get_bot
    
    bot_instance = get_bot()
    if not bot_instance:
        raise HTTPException(status_code=500, detail="Bot not initialized")
    
    try:
        await bot_instance.delete_webhook()
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error deleting webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================
# HELPER FUNCTIONS FOR WORKER COMMUNICATION
# =========================================================

async def worker_get_chats() -> list:
    """Get chats from Telegram Worker."""
    from worker_client import get_chats as wc_get_chats
    try:
        chats = await wc_get_chats()
        return chats if isinstance(chats, list) else []
    except Exception as e:
        logger.error(f"Error getting chats from worker: {e}")
        return []


async def worker_send_message(chat_ids: list, message: str) -> dict:
    """Send message via Telegram Worker."""
    from worker_client import send_message as wc_send_message
    try:
        result = await wc_send_message(chat_ids=chat_ids, message=message)
        return result
    except Exception as e:
        logger.error(f"Error sending message via worker: {e}")
        return {"ok": False, "error": str(e), "success": 0, "failed": len(chat_ids), "total": len(chat_ids)}
