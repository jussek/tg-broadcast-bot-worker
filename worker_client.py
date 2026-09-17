import json
import os
import time
import uuid
from typing import Optional

import aiohttp
from upstash_redis import Redis

WORKER_URL: Optional[str] = None
WORKER_SECRET: Optional[str] = None
TIMEOUT: Optional[aiohttp.ClientTimeout] = None
redis: Optional[Redis] = None


def _get_worker_url() -> str:
    """Get worker URL with lazy initialization."""
    global WORKER_URL
    if WORKER_URL is None:
        url = os.environ.get("TELEGRAM_WORKER_URL")
        if not url:
            raise RuntimeError(
                "TELEGRAM_WORKER_URL is required. Set it in environment variables."
            )
        WORKER_URL = url.rstrip("/")
    return WORKER_URL


def _get_worker_secret() -> str:
    """Get worker secret with lazy initialization."""
    global WORKER_SECRET
    if WORKER_SECRET is None:
        secret = os.environ.get("WORKER_SECRET")
        if not secret:
            raise RuntimeError(
                "WORKER_SECRET is required. Set it in environment variables."
            )
        WORKER_SECRET = secret
    return WORKER_SECRET


def _get_timeout() -> aiohttp.ClientTimeout:
    """Get timeout with lazy initialization."""
    global TIMEOUT
    if TIMEOUT is None:
        timeout_seconds = int(os.getenv("WORKER_TIMEOUT_SECONDS", "90"))
        TIMEOUT = aiohttp.ClientTimeout(total=timeout_seconds)
    return TIMEOUT


async def _request(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["X-Worker-Secret"] = _get_worker_secret()
    async with aiohttp.ClientSession(timeout=_get_timeout()) as session:
        async with session.request(method, f"{_get_worker_url()}{path}", headers=headers, **kwargs) as response:
            data = await response.json(content_type=None)
            if response.status >= 400 or not data.get("ok", False):
                raise RuntimeError(data.get("error", f"Worker HTTP {response.status}"))
            return data


async def get_chats():
    """Get chats from worker."""
    data = await _request("GET", "/chats")
    return data.get("chats", [])


async def send_message(chat_ids: list, message: str):
    """Send message via worker."""
    return await _request("POST", "/send", json={"chat_ids": chat_ids, "message": message})


def _get_redis():
    global redis
    if redis is None:
        if Redis is None:
            raise RuntimeError("upstash_redis is not installed")
        url = os.getenv("UPSTASH_REDIS_REST_URL")
        token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
        if not url or not token:
            raise RuntimeError("Missing UPSTASH_REDIS_REST_URL or UPSTASH_REDIS_REST_TOKEN")
        redis = Redis(url=url, token=token)
    return redis


# =========================================================
# KEYS
# =========================================================


def task_key(task_id: str):
    return f"broadcast:task:{task_id}"


def user_tasks_key(user_id: int):
    return f"broadcast:user:{user_id}:tasks"


def last_message_key(user_id: int):
    return f"broadcast:user:{user_id}:last_message"


def template_key(template_id: str):
    return f"broadcast:template:{template_id}"


def user_templates_key(user_id: int):
    return f"broadcast:user:{user_id}:templates"


def user_group_sets_key(user_id: int):
    return f"broadcast:user:{user_id}:group_sets"


def group_set_key(set_id: str):
    return f"broadcast:group_set:{set_id}"


# =========================================================
# GROUP SETS (groups of chats)
# =========================================================


def create_group_set(user_id: int, name: str, groups=None):
    set_id = str(uuid.uuid4())
    item = {
        "id": set_id,
        "user_id": int(user_id),
        "name": name.strip(),
        "groups": [int(x) for x in (groups or [])],
        "created_at": time.time(),
    }
    client = _get_redis()
    client.set(group_set_key(set_id), json.dumps(item, ensure_ascii=False))
    client.sadd(user_group_sets_key(user_id), set_id)
    return item


def get_group_set(set_id: str):
    value = _get_redis().get(group_set_key(set_id))
    if not value:
        return None
    return json.loads(value) if isinstance(value, str) else value


def get_user_group_sets(user_id: int):
    ids = _get_redis().smembers(user_group_sets_key(user_id))
    if not ids:
        return []
    result = []
    for set_id in ids:
        if isinstance(set_id, bytes):
            set_id = set_id.decode()
        item = get_group_set(set_id)
        if item and int(item.get("user_id", -1)) == int(user_id):
            result.append(item)
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return result


def update_group_set(user_id: int, set_id: str, name: str, groups=None):
    item = get_group_set(set_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        return None
    item["name"] = name.strip()
    item["groups"] = [int(x) for x in (groups or [])]
    item["updated_at"] = time.time()
    _get_redis().set(group_set_key(set_id), json.dumps(item, ensure_ascii=False))
    return item


def set_group_set_groups(user_id: int, set_id: str, groups):
    item = get_group_set(set_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        return None
    item["groups"] = [int(x) for x in (groups or [])]
    item["updated_at"] = time.time()
    _get_redis().set(group_set_key(set_id), json.dumps(item, ensure_ascii=False))
    return item


def delete_group_set(user_id: int, set_id: str):
    item = get_group_set(set_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        return False
    _get_redis().delete(group_set_key(set_id))
    _get_redis().srem(user_group_sets_key(user_id), set_id)
    return True


# =========================================================
# TEMPLATES
# =========================================================


def create_template(user_id: int, name: str, message: str, groups=None):
    template_id = str(uuid.uuid4())
    template = {
        "id": template_id,
        "user_id": int(user_id),
        "name": name.strip(),
        "message": message,
        "groups": [int(x) for x in (groups or [])],
        "created_at": time.time(),
    }
    client = _get_redis()
    client.set(template_key(template_id), json.dumps(template, ensure_ascii=False))
    client.sadd(user_templates_key(user_id), template_id)
    return template


def get_template(template_id: str):
    value = _get_redis().get(template_key(template_id))
    if not value:
        return None
    return json.loads(value) if isinstance(value, str) else value


def get_user_templates(user_id: int):
    ids = _get_redis().smembers(user_templates_key(user_id))
    if not ids:
        return []
    result = []
    for template_id in ids:
        if isinstance(template_id, bytes):
            template_id = template_id.decode()
        template = get_template(template_id)
        if template and int(template.get("user_id", -1)) == int(user_id):
            result.append(template)
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return result


def update_template(user_id: int, template_id: str, name: str, message: str):
    template = get_template(template_id)
    if not template or int(template.get("user_id", -1)) != int(user_id):
        return None
    template["name"] = name.strip()
    template["message"] = message
    template["updated_at"] = time.time()
    _get_redis().set(template_key(template_id), json.dumps(template, ensure_ascii=False))
    return template


def set_template_groups(user_id: int, template_id: str, groups):
    template = get_template(template_id)
    if not template or int(template.get("user_id", -1)) != int(user_id):
        return None
    template["groups"] = [int(x) for x in (groups or [])]
    template["updated_at"] = time.time()
    _get_redis().set(template_key(template_id), json.dumps(template, ensure_ascii=False))
    return template


def delete_template(user_id: int, template_id: str):
    template = get_template(template_id)
    if not template or int(template.get("user_id", -1)) != int(user_id):
        return False
    _get_redis().delete(template_key(template_id))
    _get_redis().srem(user_templates_key(user_id), template_id)
    return True


# =========================================================
# LAST MESSAGE
# =========================================================


def save_last_message(user_id: int, message: str):
    _get_redis().set(last_message_key(user_id), message)


def get_last_message(user_id: int):
    return _get_redis().get(last_message_key(user_id))


# =========================================================
# CREATE TASK
# =========================================================


def create_task(user_id: int, message: str, groups: list, interval_minutes: int, repeats: int):
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
    _get_redis().sadd(user_tasks_key(user_id), task_id)
    return task


# =========================================================
# SAVE TASK
# =========================================================


def save_task(task: dict):
    _get_redis().set(task_key(task["id"]), json.dumps(task, ensure_ascii=False))


# =========================================================
# GET TASK
# =========================================================


def get_task(task_id: str):
    value = _get_redis().get(task_key(task_id))
    if not value:
        return None
    return json.loads(value) if isinstance(value, str) else value


# =========================================================
# GET USER TASKS
# =========================================================


def get_user_tasks(user_id: int):
    ids = _get_redis().smembers(user_tasks_key(user_id))
    if not ids:
        return []
    result = []
    for task_id in ids:
        task = get_task(task_id)
        if task:
            result.append(task)
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return result


# =========================================================
# GET ALL TASKS
# =========================================================


def get_all_tasks():
    keys = _get_redis().keys("broadcast:task:*")
    result = []
    for key in keys:
        value = _get_redis().get(key)
        if not value:
            continue
        result.append(json.loads(value) if isinstance(value, str) else value)
    return result


# =========================================================
# GET DUE TASKS
# =========================================================


def get_due_tasks():
    now = time.time()
    tasks = get_all_tasks()
    return [
        task for task in tasks
        if task.get("status") == "active" and task.get("next_run", 0) <= now
    ]


# =========================================================
# DELETE TASK
# =========================================================


def delete_task(task_id: str):
    task = get_task(task_id)
    if not task:
        return False
    _get_redis().delete(task_key(task_id))
    _get_redis().srem(user_tasks_key(task["user_id"]), task_id)
    return True


# =========================================================
# LOCK
# =========================================================


def acquire_task_lock(task_id: str, seconds: int = 90):
    key = f"broadcast:lock:{task_id}"
    result = _get_redis().set(key, "1", nx=True, ex=seconds)
    return bool(result)


def release_task_lock(task_id: str):
    _get_redis().delete(f"broadcast:lock:{task_id}")


# =========================================================
# PERSISTENT USER UI STATE
# =========================================================


def user_state_key(user_id: int):
    return f"broadcast:user:{user_id}:state"


def save_user_state(user_id: int, state: dict):
    _get_redis().set(user_state_key(user_id), json.dumps(state, ensure_ascii=False))


def get_user_state(user_id: int):
    value = _get_redis().get(user_state_key(user_id))
    if not value:
        return None
    return json.loads(value) if isinstance(value, str) else value


def delete_user_state(user_id: int):
    _get_redis().delete(user_state_key(user_id))
