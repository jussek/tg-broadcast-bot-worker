"""Supabase storage module for Telegram Broadcast Bot.

This module provides persistent storage for users, templates, group sets,
broadcast tasks, and delivery logs using Supabase PostgreSQL.
"""
import os
import time
import uuid
from typing import Optional

from supabase import create_client, Client


# =========================================================
# SUPABASE CLIENT
# =========================================================

_supabase_client: Optional[Client] = None


def get_supabase_client() -> Client:
    """Get or create Supabase client (lazy initialization)."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    
    if not supabase_url or not supabase_key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY) are required."
        )
    
    _supabase_client = create_client(supabase_url, supabase_key)
    return _supabase_client


# =========================================================
# USERS TABLE
# =========================================================


def get_or_create_user(user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> dict:
    """Get existing user or create new one."""
    db = get_supabase_client()
    try:
        response = db.table("users").select("*").eq("user_id", user_id).execute()
        if response.data:
            return response.data[0]
    except Exception:
        pass

    user_data = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "created_at": time.time(),
        "is_active": True,
    }
    response = db.table("users").insert(user_data).execute()
    return response.data[0] if response.data else user_data


def update_user(user_id: int, **kwargs) -> Optional[dict]:
    """Update user fields."""
    db = get_supabase_client()
    response = db.table("users").update(kwargs).eq("user_id", user_id).execute()
    return response.data[0] if response.data else None


def get_user(user_id: int) -> Optional[dict]:
    """Get user by ID."""
    db = get_supabase_client()
    response = db.table("users").select("*").eq("user_id", user_id).execute()
    return response.data[0] if response.data else None


# =========================================================
# TEMPLATES TABLE
# =========================================================


def create_template(user_id: int, name: str, message: str, groups: Optional[list] = None) -> dict:
    """Create a new template."""
    db = get_supabase_client()
    template_id = str(uuid.uuid4())
    template = {
        "id": template_id,
        "user_id": user_id,
        "name": name.strip(),
        "message": message,
        "groups": groups or [],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    db.table("templates").insert(template).execute()
    return template


def get_template(template_id: str) -> Optional[dict]:
    """Get template by ID."""
    db = get_supabase_client()
    response = db.table("templates").select("*").eq("id", template_id).execute()
    return response.data[0] if response.data else None


def get_user_templates(user_id: int) -> list[dict]:
    """Get all templates for a user."""
    db = get_supabase_client()
    response = db.table("templates").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    return response.data or []


def update_template(user_id: int, template_id: str, name: Optional[str] = None, message: Optional[str] = None, groups: Optional[list] = None) -> Optional[dict]:
    """Update template fields."""
    db = get_supabase_client()
    updates = {"updated_at": time.time()}
    if name is not None:
        updates["name"] = name.strip()
    if message is not None:
        updates["message"] = message
    if groups is not None:
        updates["groups"] = groups

    response = db.table("templates").update(updates).eq("id", template_id).eq("user_id", user_id).execute()
    return response.data[0] if response.data else None


def delete_template(user_id: int, template_id: str) -> bool:
    """Delete template."""
    db = get_supabase_client()
    response = db.table("templates").delete().eq("id", template_id).eq("user_id", user_id).execute()
    return len(response.data or []) > 0


# =========================================================
# GROUP SETS TABLE (объединения чатов)
# =========================================================


def create_group_set(user_id: int, name: str, groups: Optional[list] = None) -> dict:
    """Create a new group set (union of chats)."""
    db = get_supabase_client()
    set_id = str(uuid.uuid4())
    group_set = {
        "id": set_id,
        "user_id": user_id,
        "name": name.strip(),
        "groups": [int(x) for x in (groups or [])],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    db.table("group_sets").insert(group_set).execute()
    return group_set


def get_group_set(set_id: str) -> Optional[dict]:
    """Get group set by ID."""
    db = get_supabase_client()
    response = db.table("group_sets").select("*").eq("id", set_id).execute()
    return response.data[0] if response.data else None


def get_user_group_sets(user_id: int) -> list[dict]:
    """Get all group sets for a user."""
    db = get_supabase_client()
    response = db.table("group_sets").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    return response.data or []


def update_group_set(user_id: int, set_id: str, name: Optional[str] = None, groups: Optional[list] = None) -> Optional[dict]:
    """Update group set fields."""
    db = get_supabase_client()
    updates = {"updated_at": time.time()}
    if name is not None:
        updates["name"] = name.strip()
    if groups is not None:
        updates["groups"] = [int(x) for x in groups]

    response = db.table("group_sets").update(updates).eq("id", set_id).eq("user_id", user_id).execute()
    return response.data[0] if response.data else None


def delete_group_set(user_id: int, set_id: str) -> bool:
    """Delete group set."""
    db = get_supabase_client()
    response = db.table("group_sets").delete().eq("id", set_id).eq("user_id", user_id).execute()
    return len(response.data or []) > 0


# =========================================================
# BROADCAST TASKS TABLE (задачи рассылок)
# =========================================================


def create_broadcast_task(user_id: int, message: str, groups: list, interval_minutes: int, repeats: int) -> dict:
    """Create a new scheduled broadcast task."""
    db = get_supabase_client()
    task_id = str(uuid.uuid4())
    now = time.time()
    task = {
        "id": task_id,
        "user_id": user_id,
        "message": message,
        "groups": [int(x) for x in groups],
        "interval_minutes": int(interval_minutes),
        "total_repeats": int(repeats),
        "completed_repeats": 0,
        "status": "active",
        "next_run": now + int(interval_minutes) * 60,
        "created_at": now,
        "updated_at": now,
    }
    db.table("broadcast_tasks").insert(task).execute()
    return task


def get_broadcast_task(task_id: str) -> Optional[dict]:
    """Get task by ID."""
    db = get_supabase_client()
    response = db.table("broadcast_tasks").select("*").eq("id", task_id).execute()
    return response.data[0] if response.data else None


def get_user_broadcast_tasks(user_id: int, status: Optional[str] = None) -> list[dict]:
    """Get all tasks for a user, optionally filtered by status."""
    db = get_supabase_client()
    query = db.table("broadcast_tasks").select("*").eq("user_id", user_id)
    if status:
        query = query.eq("status", status)
    response = query.order("created_at", desc=True).execute()
    return response.data or []


def get_due_broadcast_tasks() -> list[dict]:
    """Get all active tasks that are due for execution."""
    db = get_supabase_client()
    now = time.time()
    response = db.table("broadcast_tasks").select("*").eq("status", "active").lte("next_run", now).execute()
    return response.data or []


def update_broadcast_task(task_id: str, **kwargs) -> Optional[dict]:
    """Update task fields."""
    db = get_supabase_client()
    kwargs["updated_at"] = time.time()
    response = db.table("broadcast_tasks").update(kwargs).eq("id", task_id).execute()
    return response.data[0] if response.data else None


def delete_broadcast_task(user_id: int, task_id: str) -> bool:
    """Delete a broadcast task."""
    db = get_supabase_client()
    response = db.table("broadcast_tasks").delete().eq("id", task_id).eq("user_id", user_id).execute()
    return len(response.data or []) > 0


# =========================================================
# BROADCAST LOGS TABLE (история отправок)
# =========================================================


def log_broadcast(task_id: Optional[str], user_id: int, message: str, groups: list, success_count: int, failed_count: int, errors: Optional[list] = None) -> dict:
    """Log a broadcast attempt."""
    db = get_supabase_client()
    log_id = str(uuid.uuid4())
    log_entry = {
        "id": log_id,
        "task_id": task_id,
        "user_id": user_id,
        "message": message[:1000],
        "groups": groups,
        "success_count": success_count,
        "failed_count": failed_count,
        "errors": errors or [],
        "created_at": time.time(),
    }
    db.table("broadcast_logs").insert(log_entry).execute()
    return log_entry


def get_user_broadcast_logs(user_id: int, limit: int = 50) -> list[dict]:
    """Get recent broadcast logs for a user."""
    db = get_supabase_client()
    response = db.table("broadcast_logs").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute()
    return response.data or []


def get_task_broadcast_logs(task_id: str, limit: int = 50) -> list[dict]:
    """Get broadcast logs for a specific task."""
    db = get_supabase_client()
    response = db.table("broadcast_logs").select("*").eq("task_id", task_id).order("created_at", desc=True).limit(limit).execute()
    return response.data or []


# =========================================================
# CHAT MEMBERSHIPS TABLE (какие чаты у пользователей)
# =========================================================


def upsert_chat_membership(user_id: int, chat_id: int, chat_title: str, chat_type: str) -> dict:
    """Add or update a chat membership for a user."""
    db = get_supabase_client()
    membership = {
        "user_id": user_id,
        "chat_id": chat_id,
        "chat_title": chat_title,
        "chat_type": chat_type,
        "last_seen": time.time(),
    }
    response = db.table("chat_memberships").upsert(membership, on_conflict="user_id,chat_id").execute()
    return response.data[0] if response.data else membership


def get_user_chats(user_id: int) -> list[dict]:
    """Get all chats for a user."""
    db = get_supabase_client()
    response = db.table("chat_memberships").select("*").eq("user_id", user_id).order("chat_title").execute()
    return response.data or []


def remove_chat_membership(user_id: int, chat_id: int) -> bool:
    """Remove a chat membership."""
    db = get_supabase_client()
    response = db.table("chat_memberships").delete().eq("user_id", user_id).eq("chat_id", chat_id).execute()
    return len(response.data or []) > 0


def sync_user_chats(user_id: int, chats: list[dict]) -> int:
    """Sync user's chat list. Returns count of synced chats."""
    for chat in chats:
        upsert_chat_membership(
            user_id=user_id,
            chat_id=int(chat["id"]),
            chat_title=chat.get("title", "Без названия"),
            chat_type=chat.get("type", "group"),
        )
    return len(chats)
