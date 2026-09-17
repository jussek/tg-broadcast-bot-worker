"""FastAPI API entrypoint for Telegram Broadcast Bot.

This module provides the main API endpoints for the Vercel deployment.
It handles:
- Health checks
- User management
- Template management
- Group set management
- Broadcast task management
- Worker communication
"""
import os
import logging
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

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
from worker_client import send_message as worker_send_message, get_chats as worker_get_chats


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
    name: str
    message: str
    groups: Optional[list] = None


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    message: Optional[str] = None
    groups: Optional[list] = None


class GroupSetCreate(BaseModel):
    name: str
    groups: Optional[list] = None


class GroupSetUpdate(BaseModel):
    name: Optional[str] = None
    groups: Optional[list] = None


class BroadcastTaskCreate(BaseModel):
    message: str
    groups: list
    interval_minutes: int
    repeats: int


class MessageSend(BaseModel):
    chat_ids: list
    message: str


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
        return {"ok": True, "result": result}
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
