import os
import json
import time
import uuid

from upstash_redis import Redis


# =========================================================
# REDIS
# =========================================================

redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"]
)


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
    redis.set(group_set_key(set_id), json.dumps(item, ensure_ascii=False))
    redis.sadd(user_group_sets_key(user_id), set_id)
    return item


def get_group_set(set_id: str):
    value = redis.get(group_set_key(set_id))
    if not value:
        return None
    return json.loads(value) if isinstance(value, str) else value


def get_user_group_sets(user_id: int):
    ids = redis.smembers(user_group_sets_key(user_id))
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
    redis.set(group_set_key(set_id), json.dumps(item, ensure_ascii=False))
    return item


def set_group_set_groups(user_id: int, set_id: str, groups):
    item = get_group_set(set_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        return None
    item["groups"] = [int(x) for x in (groups or [])]
    item["updated_at"] = time.time()
    redis.set(group_set_key(set_id), json.dumps(item, ensure_ascii=False))
    return item


def delete_group_set(user_id: int, set_id: str):
    item = get_group_set(set_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        return False
    redis.delete(group_set_key(set_id))
    redis.srem(user_group_sets_key(user_id), set_id)
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
        "created_at": time.time()
    }
    redis.set(template_key(template_id), json.dumps(template, ensure_ascii=False))
    redis.sadd(user_templates_key(user_id), template_id)
    return template


def get_template(template_id: str):
    value = redis.get(template_key(template_id))
    if not value:
        return None
    return json.loads(value) if isinstance(value, str) else value


def get_user_templates(user_id: int):
    ids = redis.smembers(user_templates_key(user_id))
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
    redis.set(template_key(template_id), json.dumps(template, ensure_ascii=False))
    return template



def set_template_groups(user_id: int, template_id: str, groups):
    template = get_template(template_id)
    if not template or int(template.get("user_id", -1)) != int(user_id):
        return None

    template["groups"] = [int(x) for x in (groups or [])]
    template["updated_at"] = time.time()

    redis.set(
        template_key(template_id),
        json.dumps(template, ensure_ascii=False)
    )

    return template

def delete_template(user_id: int, template_id: str):
    template = get_template(template_id)
    if not template or int(template.get("user_id", -1)) != int(user_id):
        return False
    redis.delete(template_key(template_id))
    redis.srem(user_templates_key(user_id), template_id)
    return True


# =========================================================
# LAST MESSAGE
# =========================================================

def save_last_message(
    user_id: int,
    message: str
):

    redis.set(
        last_message_key(user_id),
        message
    )


def get_last_message(
    user_id: int
):

    return redis.get(
        last_message_key(user_id)
    )


# =========================================================
# CREATE TASK
# =========================================================

def create_task(
    user_id: int,
    message: str,
    groups: list,
    interval_minutes: int,
    repeats: int
):

    task_id = str(
        uuid.uuid4()
    )

    now = time.time()

    task = {

        "id": task_id,

        "user_id": int(user_id),

        "message": message,

        "groups": [
            int(x)
            for x in groups
        ],

        "interval_minutes": int(
            interval_minutes
        ),

        "total_repeats": int(
            repeats
        ),

        "completed_repeats": 0,

        "status": "active",

        "created_at": now,

        "next_run": (
            now
            + int(interval_minutes) * 60
        )
    }

    save_task(task)

    redis.sadd(
        user_tasks_key(user_id),
        task_id
    )

    return task


# =========================================================
# SAVE TASK
# =========================================================

def save_task(task: dict):

    redis.set(
        task_key(task["id"]),
        json.dumps(
            task,
            ensure_ascii=False
        )
    )


# =========================================================
# GET TASK
# =========================================================

def get_task(
    task_id: str
):

    value = redis.get(
        task_key(task_id)
    )

    if not value:
        return None

    if isinstance(value, str):

        return json.loads(value)

    return value


# =========================================================
# GET USER TASKS
# =========================================================

def get_user_tasks(
    user_id: int
):

    ids = redis.smembers(
        user_tasks_key(user_id)
    )

    if not ids:
        return []

    result = []

    for task_id in ids:

        task = get_task(
            task_id
        )

        if task:

            result.append(task)

    result.sort(
        key=lambda x: x.get(
            "created_at",
            0
        ),
        reverse=True
    )

    return result


# =========================================================
# GET ALL TASKS
# =========================================================

def get_all_tasks():

    keys = redis.keys(
        "broadcast:task:*"
    )

    result = []

    for key in keys:

        value = redis.get(key)

        if not value:
            continue

        if isinstance(value, str):

            result.append(
                json.loads(value)
            )

        else:

            result.append(value)

    return result


# =========================================================
# GET DUE TASKS
# =========================================================

def get_due_tasks():

    now = time.time()

    tasks = get_all_tasks()

    return [
        task
        for task in tasks
        if (
            task.get("status") == "active"
            and task.get("next_run", 0) <= now
        )
    ]


# =========================================================
# DELETE TASK
# =========================================================

def delete_task(
    task_id: str
):

    task = get_task(
        task_id
    )

    if not task:
        return False

    redis.delete(
        task_key(task_id)
    )

    redis.srem(
        user_tasks_key(
            task["user_id"]
        ),
        task_id
    )

    return True


# =========================================================
# LOCK
# =========================================================

def acquire_task_lock(
    task_id: str,
    seconds: int = 90
):

    key = (
        f"broadcast:lock:{task_id}"
    )

    result = redis.set(
        key,
        "1",
        nx=True,
        ex=seconds
    )

    return bool(result)


def release_task_lock(
    task_id: str
):

    redis.delete(
        f"broadcast:lock:{task_id}"
    )

# =========================================================
# PERSISTENT USER UI STATE
# =========================================================

def user_state_key(user_id: int):
    return f"broadcast:user:{user_id}:state"


def save_user_state(user_id: int, state: dict):
    redis.set(user_state_key(user_id), json.dumps(state, ensure_ascii=False))


def get_user_state(user_id: int):
    value = redis.get(user_state_key(user_id))
    if not value:
        return None
    return json.loads(value) if isinstance(value, str) else value


def delete_user_state(user_id: int):
    redis.delete(user_state_key(user_id))
