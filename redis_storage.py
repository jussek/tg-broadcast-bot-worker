import json
import os
import time
import uuid

from upstash_redis import Redis


_redis = None

SET_STATE_FIELDS = {
    "selected",
    "template_group_ids",
    "legacy_template_chat_ids",
    "chat_picker_selected_ids",
}


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        url = os.getenv("UPSTASH_REDIS_REST_URL")
        token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
        if not url or not token:
            raise RuntimeError(
                "Не найдены UPSTASH_REDIS_REST_URL или UPSTASH_REDIS_REST_TOKEN"
            )
        _redis = Redis(url=url, token=token)
    return _redis


def _as_str(value):
    if isinstance(value, bytes):
        return value.decode()
    return value


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


def chat_group_key(group_id: str):
    return f"broadcast:chatgroup:{group_id}"


def user_chat_groups_key(user_id: int):
    return f"broadcast:user:{user_id}:chatgroups"


def user_state_key(user_id: int):
    return f"broadcast:state:{user_id}"


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
        if key in {"template_group_ids"}:
            decoded[key] = {str(x) for x in values}
        else:
            decoded[key] = {int(x) for x in values}
    return decoded


def get_user_state(user_id: int):
    value = get_redis().get(user_state_key(user_id))
    data = _loads(value)
    if not data:
        return None
    return _decode_state(data)


def set_user_state(user_id: int, state: dict):
    get_redis().set(
        user_state_key(user_id),
        json.dumps(_encode_state(state), ensure_ascii=False),
    )
    return state


def clear_user_state(user_id: int):
    get_redis().delete(user_state_key(user_id))


def create_chat_group(user_id: int, name: str, chat_ids=None):
    group_id = str(uuid.uuid4())
    item = {
        "id": group_id,
        "user_id": int(user_id),
        "name": name.strip(),
        "chat_ids": [int(x) for x in (chat_ids or [])],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    redis = get_redis()
    redis.set(chat_group_key(group_id), json.dumps(item, ensure_ascii=False))
    redis.sadd(user_chat_groups_key(user_id), group_id)
    return item


def get_chat_group(group_id: str):
    value = get_redis().get(chat_group_key(_as_str(group_id)))
    if not value:
        return None
    return _loads(value)


def get_user_chat_groups(user_id: int):
    ids = get_redis().smembers(user_chat_groups_key(user_id))
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


def update_chat_group(user_id: int, group_id: str, name: str, chat_ids=None):
    item = get_chat_group(group_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        return None
    item["name"] = name.strip()
    item["chat_ids"] = [int(x) for x in (chat_ids or [])]
    item["updated_at"] = time.time()
    get_redis().set(chat_group_key(group_id), json.dumps(item, ensure_ascii=False))
    return item


def set_chat_group_chats(user_id: int, group_id: str, chat_ids):
    item = get_chat_group(group_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        return None
    item["chat_ids"] = [int(x) for x in (chat_ids or [])]
    item["updated_at"] = time.time()
    get_redis().set(chat_group_key(group_id), json.dumps(item, ensure_ascii=False))
    return item


def delete_chat_group(user_id: int, group_id: str):
    item = get_chat_group(group_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        return False
    redis = get_redis()
    redis.delete(chat_group_key(group_id))
    redis.srem(user_chat_groups_key(user_id), group_id)
    return True


def create_template(user_id: int, name: str, message: str, groups=None, group_ids=None):
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
    redis = get_redis()
    redis.set(template_key(template_id), json.dumps(template, ensure_ascii=False))
    redis.sadd(user_templates_key(user_id), template_id)
    return template


def get_template(template_id: str):
    value = get_redis().get(template_key(_as_str(template_id)))
    if not value:
        return None
    return _loads(value)


def get_user_templates(user_id: int):
    ids = get_redis().smembers(user_templates_key(user_id))
    if not ids:
        return []
    result = []
    for template_id in ids:
        template = get_template(_as_str(template_id))
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
    get_redis().set(template_key(template_id), json.dumps(template, ensure_ascii=False))
    return template


def set_template_group_ids(user_id: int, template_id: str, group_ids):
    template = get_template(template_id)
    if not template or int(template.get("user_id", -1)) != int(user_id):
        return None
    template["group_ids"] = [str(x) for x in (group_ids or [])]
    template["updated_at"] = time.time()
    get_redis().set(template_key(template_id), json.dumps(template, ensure_ascii=False))
    return template


def set_template_groups(user_id: int, template_id: str, groups):
    template = get_template(template_id)
    if not template or int(template.get("user_id", -1)) != int(user_id):
        return None
    template["groups"] = [int(x) for x in (groups or [])]
    template["updated_at"] = time.time()
    get_redis().set(template_key(template_id), json.dumps(template, ensure_ascii=False))
    return template


def delete_template(user_id: int, template_id: str):
    template = get_template(template_id)
    if not template or int(template.get("user_id", -1)) != int(user_id):
        return False
    redis = get_redis()
    redis.delete(template_key(template_id))
    redis.srem(user_templates_key(user_id), template_id)
    return True


def save_last_message(user_id: int, message: str):
    get_redis().set(last_message_key(user_id), message)


def get_last_message(user_id: int):
    value = get_redis().get(last_message_key(user_id))
    return _as_str(value) if value is not None else None


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
    get_redis().sadd(user_tasks_key(user_id), task_id)
    return task


def save_task(task: dict):
    get_redis().set(task_key(task["id"]), json.dumps(task, ensure_ascii=False))


def get_task(task_id: str):
    value = get_redis().get(task_key(_as_str(task_id)))
    if not value:
        return None
    return _loads(value)


def get_user_tasks(user_id: int):
    ids = get_redis().smembers(user_tasks_key(user_id))
    if not ids:
        return []
    result = []
    for task_id in ids:
        task = get_task(_as_str(task_id))
        if task:
            result.append(task)
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return result


def get_all_tasks():
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


def get_due_tasks():
    now = time.time()
    return [
        task
        for task in get_all_tasks()
        if task.get("status") == "active" and float(task.get("next_run") or 0) <= now
    ]


def delete_task(task_id: str):
    task = get_task(task_id)
    if not task:
        return False
    redis = get_redis()
    redis.delete(task_key(task_id))
    redis.srem(user_tasks_key(task["user_id"]), task_id)
    return True


def acquire_task_lock(task_id: str, seconds: int = 90):
    result = get_redis().set(
        f"broadcast:lock:{task_id}",
        "1",
        nx=True,
        ex=seconds,
    )
    return bool(result)


def release_task_lock(task_id: str):
    get_redis().delete(f"broadcast:lock:{task_id}")
