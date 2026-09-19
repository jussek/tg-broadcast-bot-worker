"""Data models and storage operations."""
import json
import time
import uuid
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, asdict

from .redis_client import get_redis


# ============================================================================
# Constants
# ============================================================================

SET_STATE_FIELDS = {"selected", "template_group_ids", "legacy_template_chat_ids", "chat_picker_selected_ids"}


# ============================================================================
# Data Models (TypedDicts for flexibility)
# ============================================================================

class Task:
    """Scheduled broadcast task."""
    def __init__(self, id: str, user_id: int, message: str, groups: List[int],
                 interval_minutes: int, total_repeats: int, status: str = "active",
                 completed_repeats: int = 0, created_at: float = None, next_run: float = None):
        self.id = id
        self.user_id = user_id
        self.message = message
        self.groups = groups
        self.interval_minutes = interval_minutes
        self.total_repeats = total_repeats
        self.status = status
        self.completed_repeats = completed_repeats
        self.created_at = created_at or time.time()
        self.next_run = next_run or (time.time() + interval_minutes * 60)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "user_id": self.user_id, "message": self.message,
            "groups": self.groups, "interval_minutes": self.interval_minutes,
            "total_repeats": self.total_repeats, "status": self.status,
            "completed_repeats": self.completed_repeats,
            "created_at": self.created_at, "next_run": self.next_run
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(**data)


class Template:
    """Message template."""
    def __init__(self, id: str, user_id: int, name: str, message: str,
                 group_ids: List[str] = None, groups: List[int] = None,
                 created_at: float = None, updated_at: float = None):
        self.id = id
        self.user_id = user_id
        self.name = name
        self.message = message
        self.group_ids = group_ids or []
        self.groups = groups or []
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or time.time()

    def to_dict(self) -> dict:
        return asdict(self)


class ChatGroup:
    """Group of chat IDs for broadcasting."""
    def __init__(self, id: str, user_id: int, name: str, chat_ids: List[int],
                 created_at: float = None, updated_at: float = None):
        self.id = id
        self.user_id = user_id
        self.name = name
        self.chat_ids = chat_ids
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or time.time()

    def to_dict(self) -> dict:
        return asdict(self)


class UserState(dict):
    """User FSM state stored in Redis."""
    pass


# ============================================================================
# Key Generators
# ============================================================================

def _task_key(task_id: str) -> str:
    return f"broadcast:task:{task_id}"


def _user_tasks_key(user_id: int) -> str:
    return f"broadcast:user:{user_id}:tasks"


def _last_message_key(user_id: int) -> str:
    return f"broadcast:user:{user_id}:last_message"


def _template_key(template_id: str) -> str:
    return f"broadcast:template:{template_id}"


def _user_templates_key(user_id: int) -> str:
    return f"broadcast:user:{user_id}:templates"


def _chat_group_key(group_id: str) -> str:
    return f"broadcast:chatgroup:{group_id}"


def _user_chat_groups_key(user_id: int) -> str:
    return f"broadcast:user:{user_id}:chatgroups"


def _user_state_key(user_id: int) -> str:
    return f"broadcast:state:{user_id}"


def _task_lock_key(task_id: str) -> str:
    return f"broadcast:lock:{task_id}"


# ============================================================================
# Helpers
# ============================================================================

def _as_str(value) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _loads(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode()
    if isinstance(value, str):
        return json.loads(value)
    return value


def _encode_state(state: dict) -> dict:
    encoded = {}
    for key, value in state.items():
        if isinstance(value, set):
            encoded[key] = list(value)
        else:
            encoded[key] = value
    return encoded


def _decode_state(state: dict) -> dict:
    decoded = dict(state)
    for key in SET_STATE_FIELDS:
        if key not in decoded or decoded[key] is None:
            continue
        values = decoded[key]
        if key == "template_group_ids":
            decoded[key] = {str(x) for x in values}
        else:
            decoded[key] = {int(x) for x in values}
    return decoded


# ============================================================================
# Task Operations
# ============================================================================

def create_task(user_id: int, message: str, groups: list, interval_minutes: int, repeats: int) -> dict:
    task_id = str(uuid.uuid4())
    now = time.time()
    task = {
        "id": task_id,
        "user_id": int(user_id),
        "message": message,
        "groups": [int(x) for x in groups],
        "interval_minutes": int(interval_minutes),
        "total_repeats": int(repeats),
        "completed_repeats": 0,
        "status": "active",
        "created_at": now,
        "next_run": now + int(interval_minutes) * 60,
    }
    save_task(task)
    get_redis().sadd(_user_tasks_key(user_id), task_id)
    return task


def get_task(task_id: str) -> Optional[dict]:
    value = get_redis().get(_task_key(_as_str(task_id)))
    if not value:
        return None
    return _loads(value)


def save_task(task: dict):
    get_redis().set(_task_key(task["id"]), json.dumps(task, ensure_ascii=False))


def delete_task(task_id: str) -> bool:
    task = get_task(task_id)
    if not task:
        return False
    r = get_redis()
    r.delete(_task_key(task_id))
    r.srem(_user_tasks_key(task["user_id"]), task_id)
    return True


def get_user_tasks(user_id: int) -> List[dict]:
    ids = get_redis().smembers(_user_tasks_key(user_id))
    if not ids:
        return []
    result = []
    for task_id in ids:
        task = get_task(_as_str(task_id))
        if task:
            result.append(task)
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return result


def get_all_tasks() -> List[dict]:
    keys = get_redis().keys("broadcast:task:*")
    result = []
    for key in keys or []:
        value = get_redis().get(_as_str(key))
        if not value:
            continue
        task = _loads(value)
        if task:
            result.append(task)
    return result


def get_due_tasks() -> List[dict]:
    now = time.time()
    return [
        task for task in get_all_tasks()
        if task.get("status") == "active" and float(task.get("next_run") or 0) <= now
    ]


def acquire_task_lock(task_id: str, seconds: int = 90) -> bool:
    result = get_redis().set(_task_lock_key(task_id), "1", nx=True, ex=seconds)
    return bool(result)


def release_task_lock(task_id: str):
    get_redis().delete(_task_lock_key(task_id))


# ============================================================================
# Template Operations
# ============================================================================

def create_template(user_id: int, name: str, message: str, groups=None, group_ids=None) -> dict:
    template_id = str(uuid.uuid4())
    now = time.time()
    template = {
        "id": template_id,
        "user_id": int(user_id),
        "name": name.strip(),
        "message": message,
        "groups": [int(x) for x in (groups or [])],
        "group_ids": [str(x) for x in (group_ids or [])],
        "created_at": now,
        "updated_at": now,
    }
    r = get_redis()
    r.set(_template_key(template_id), json.dumps(template, ensure_ascii=False))
    r.sadd(_user_templates_key(user_id), template_id)
    return template


def get_template(template_id: str) -> Optional[dict]:
    value = get_redis().get(_template_key(_as_str(template_id)))
    if not value:
        return None
    return _loads(value)


def get_user_templates(user_id: int) -> List[dict]:
    ids = get_redis().smembers(_user_templates_key(user_id))
    if not ids:
        return []
    result = []
    for template_id in ids:
        template = get_template(_as_str(template_id))
        if template and int(template.get("user_id", -1)) == int(user_id):
            result.append(template)
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return result


def update_template(user_id: int, template_id: str, name: str, message: str) -> Optional[dict]:
    template = get_template(template_id)
    if not template or int(template.get("user_id", -1)) != int(user_id):
        return None
    template["name"] = name.strip()
    template["message"] = message
    template["updated_at"] = time.time()
    get_redis().set(_template_key(template_id), json.dumps(template, ensure_ascii=False))
    return template


def set_template_group_ids(user_id: int, template_id: str, group_ids) -> Optional[dict]:
    template = get_template(template_id)
    if not template or int(template.get("user_id", -1)) != int(user_id):
        return None
    template["group_ids"] = [str(x) for x in (group_ids or [])]
    template["updated_at"] = time.time()
    get_redis().set(_template_key(template_id), json.dumps(template, ensure_ascii=False))
    return template


def delete_template(user_id: int, template_id: str) -> bool:
    template = get_template(template_id)
    if not template or int(template.get("user_id", -1)) != int(user_id):
        return False
    r = get_redis()
    r.delete(_template_key(template_id))
    r.srem(_user_templates_key(user_id), template_id)
    return True


# ============================================================================
# Chat Group Operations
# ============================================================================

def create_chat_group(user_id: int, name: str, chat_ids=None) -> dict:
    group_id = str(uuid.uuid4())
    item = {
        "id": group_id,
        "user_id": int(user_id),
        "name": name.strip(),
        "chat_ids": [int(x) for x in (chat_ids or [])],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    r = get_redis()
    r.set(_chat_group_key(group_id), json.dumps(item, ensure_ascii=False))
    r.sadd(_user_chat_groups_key(user_id), group_id)
    return item


def get_chat_group(group_id: str) -> Optional[dict]:
    value = get_redis().get(_chat_group_key(_as_str(group_id)))
    if not value:
        return None
    return _loads(value)


def get_user_chat_groups(user_id: int) -> List[dict]:
    ids = get_redis().smembers(_user_chat_groups_key(user_id))
    if not ids:
        return []
    result = []
    for group_id in ids:
        item = get_chat_group(_as_str(group_id))
        if item and int(item.get("user_id", -1)) == int(user_id):
            item.setdefault("chat_ids", [])
            result.append(item)
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return result


def update_chat_group(user_id: int, group_id: str, name: str, chat_ids=None) -> Optional[dict]:
    item = get_chat_group(group_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        return None
    item["name"] = name.strip()
    item["chat_ids"] = [int(x) for x in (chat_ids or [])]
    item["updated_at"] = time.time()
    get_redis().set(_chat_group_key(group_id), json.dumps(item, ensure_ascii=False))
    return item


def set_chat_group_chats(user_id: int, group_id: str, chat_ids) -> Optional[dict]:
    item = get_chat_group(group_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        return None
    item["chat_ids"] = [int(x) for x in (chat_ids or [])]
    item["updated_at"] = time.time()
    get_redis().set(_chat_group_key(group_id), json.dumps(item, ensure_ascii=False))
    return item


def delete_chat_group(user_id: int, group_id: str) -> bool:
    item = get_chat_group(group_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        return False
    r = get_redis()
    r.delete(_chat_group_key(group_id))
    r.srem(_user_chat_groups_key(user_id), group_id)
    return True


# ============================================================================
# User State Operations
# ============================================================================

def get_user_state(user_id: int) -> Optional[dict]:
    value = get_redis().get(_user_state_key(user_id))
    data = _loads(value)
    if not data:
        return None
    return _decode_state(data)


def set_user_state(user_id: int, state: dict):
    get_redis().set(
        _user_state_key(user_id),
        json.dumps(_encode_state(state), ensure_ascii=False)
    )
    return state


def clear_user_state(user_id: int):
    get_redis().delete(_user_state_key(user_id))


# ============================================================================
# Last Message Operations
# ============================================================================

def save_last_message(user_id: int, message: str):
    get_redis().set(_last_message_key(user_id), message)


def get_last_message(user_id: int) -> Optional[str]:
    value = get_redis().get(_last_message_key(user_id))
    return _as_str(value) if value is not None else None
