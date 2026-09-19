"""Storage module for Redis operations."""
from .redis_client import get_redis, redis
from .models import (
    Task, Template, ChatGroup, UserState,
    create_task, get_task, save_task, delete_task, get_user_tasks, get_due_tasks,
    create_template, get_template, get_user_templates, update_template, 
    set_template_group_ids, delete_template,
    create_chat_group, get_chat_group, get_user_chat_groups, update_chat_group,
    set_chat_group_chats, delete_chat_group,
    get_user_state, set_user_state, clear_user_state,
    save_last_message, get_last_message,
    acquire_task_lock, release_task_lock
)

__all__ = [
    "get_redis", "redis",
    "Task", "Template", "ChatGroup", "UserState",
    "create_task", "get_task", "save_task", "delete_task", "get_user_tasks", "get_due_tasks",
    "create_template", "get_template", "get_user_templates", "update_template",
    "set_template_group_ids", "delete_template",
    "create_chat_group", "get_chat_group", "get_user_chat_groups", "update_chat_group",
    "set_chat_group_chats", "delete_chat_group",
    "get_user_state", "set_user_state", "clear_user_state",
    "save_last_message", "get_last_message",
    "acquire_task_lock", "release_task_lock"
]
