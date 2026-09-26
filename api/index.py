"""Telegram broadcast bot — FastAPI + aiogram on Vercel serverless.

Architecture (serverless-safe):
  Telegram -> POST /api/webhook -> Update -> Dispatcher (single, module-level)
           -> callback/message handlers -> service layer -> Redis (state/cache)
                                                        -> Telethon (MTProto)
  QStash   -> POST /api/process  -> broadcast runner (locks + idempotency)

Important rules enforced here:
  * setWebhook is NEVER called during app startup (it caused Telegram flood
    control crashes on every cold start).  It is exposed only as an explicit,
    secret-protected operation: POST /api/setup-webhook.
  * The aiogram Bot session is closed after every webhook invocation to avoid
    "Unclosed client session / Unclosed connector" warnings in serverless.
  * All user/broadcast state lives in Redis, not in process memory.
"""

import asyncio
import html
import json
import logging
import math
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# Vercel imports this module as ``index`` with the repo root NOT on sys.path
# (only the function directory is).  Make sibling packages importable.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerIdInvalidError,
    RPCError,
    UserBannedInChannelError,
)
from telethon.sessions import StringSession

from qstash import QStash

# Configure logging before anything else can use it during import.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration (env only — never hardcode secrets) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")
APP_URL = os.getenv("APP_URL", "https://tg-broadcast-bot-worker.vercel.app").rstrip("/")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
WEBHOOK_SETUP_SECRET = os.getenv("WEBHOOK_SETUP_SECRET")
QSTASH_VERIFICATION_KEY = os.getenv("QSTASH_VERIFICATION_KEY")

IS_VERCEL_ENV = os.getenv("VERCEL", "").lower() in {"1", "true", "yes"}

if not IS_VERCEL_ENV and not all([BOT_TOKEN, API_ID, API_HASH, TELEGRAM_SESSION_STRING]):
    logger.warning(
        "Missing required environment variables (BOT_TOKEN/API_ID/API_HASH/"
        "TELEGRAM_SESSION_STRING). Endpoints that need them will report errors."
    )


class MissingEnvError(RuntimeError):
    """Raised when a required environment variable is not configured."""


def _require_env(value: Optional[str], name: str) -> str:
    if not value:
        raise MissingEnvError(f"{name} is not configured")
    return value


# ============================================================================
# Storage layer — single Redis implementation (storage/redis_client + models)
# ============================================================================
from storage.redis_client import get_redis  # noqa: E402
from storage.models import (  # noqa: E402
    _loads,
    acquire_task_lock,
    clear_user_state as _models_clear_user_state,
    get_last_message as _models_get_last_message,
    get_task,
    get_user_state as _models_get_user_state,
    release_task_lock,
    save_last_message as _models_save_last_message,
    save_task,
)


def _build_fsm_storage() -> BaseStorage:
    """Redis-backed FSM storage when Upstash credentials exist; else memory.

    MemoryStorage alone is unsafe on Vercel (process state disappears between
    invocations), so it is only used as a local/test fallback.  Construction
    must never crash the whole serverless function: any failure while wiring
    up Redis FSM degrades gracefully to MemoryStorage instead of returning
    HTTP 500 for every request (import-time exceptions take down the deploy).
    """
    if os.getenv("UPSTASH_REDIS_REST_URL") and os.getenv("UPSTASH_REDIS_REST_TOKEN"):
        try:
            from storage.redis_fsm_storage import RedisFSMStorage

            return RedisFSMStorage()
        except Exception:
            # Non-fatal: log loudly but keep the app importable/running.
            logger.exception("Redis FSM storage unavailable; falling back to MemoryStorage")
    else:
        logger.warning("Redis credentials absent; FSM falls back to MemoryStorage (dev only)")
    return MemoryStorage()


# --- Redis helpers (user broadcast state, tasks, templates, group lists) ---
def _safe_loads(raw, default=None):
    """Parse a Redis JSON value; corrupted data yields ``default`` (never crash)."""
    if raw is None:
        return default
    try:
        return _loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.exception("Corrupted JSON payload in Redis; treating as missing")
        return default


def get_user_state(user_id: int) -> Optional[Dict[str, Any]]:
    try:
        return _models_get_user_state(user_id)
    except MissingEnvError:
        raise
    except Exception:
        logger.exception("Failed to read state for user %s", user_id)
        return None


def set_user_state(user_id: int, state: Dict[str, Any]):
    from storage.models import set_user_state as models_set_user_state

    models_set_user_state(user_id, state)


def clear_user_state(user_id: int):
    _models_clear_user_state(user_id)


def save_last_message(user_id: int, message_text: str):
    _models_save_last_message(user_id, message_text)


def get_last_message(user_id: int) -> Optional[str]:
    try:
        value = _models_get_last_message(user_id)
    except Exception:
        logger.exception("Failed to read last message for user %s", user_id)
        return None
    if isinstance(value, bytes):
        value = value.decode()
    return value


def get_task_key(task_id: str) -> str:
    return f"broadcast:task:{task_id}"


def get_user_tasks(user_id: int) -> List[Dict[str, Any]]:
    ids = get_redis().smembers(f"broadcast:user:{user_id}:tasks") or []
    tasks = []
    for task_id in ids:
        task = get_task(task_id.decode() if isinstance(task_id, bytes) else str(task_id))
        if task:
            tasks.append(task)
    return sorted(tasks, key=lambda task: task.get("created_at", 0), reverse=True)


# Template helpers (per-user JSON list kept in Redis)
def get_templates_key(user_id: int) -> str:
    return f"broadcast:templates:{user_id}"


def get_user_templates(user_id: int) -> list:
    return _safe_loads(get_redis().get(get_templates_key(user_id)), default=[])


def save_user_templates(user_id: int, templates: list):
    get_redis().set(get_templates_key(user_id), json.dumps(templates, ensure_ascii=False))


def get_group_lists_key(user_id: int) -> str:
    return f"broadcast:group-lists:{user_id}"


def get_group_lists(user_id: int) -> list:
    return _safe_loads(get_redis().get(get_group_lists_key(user_id)), default=[])


def save_group_lists(user_id: int, group_lists: list):
    get_redis().set(get_group_lists_key(user_id), json.dumps(group_lists, ensure_ascii=False))


# Group cache helpers — distinguish "no cache" from "empty cache".
def groups_cache_key(user_id: int) -> str:
    return f"broadcast:groups:{user_id}"


def load_groups_cache(user_id: int) -> Optional[List[Dict[str, str]]]:
    """Return cached groups list, or ``None`` when the cache does not exist."""
    raw = get_redis().get(groups_cache_key(user_id))
    if raw is None:
        return None
    parsed = _safe_loads(raw, default=None)
    if parsed is None:
        logger.warning("Group cache for user %s is corrupted; ignoring", user_id)
        return None
    if not isinstance(parsed, list):
        return None
    return [g for g in parsed if isinstance(g, dict) and g.get("id")]


def save_groups_cache(user_id: int, groups: List[Dict[str, str]], ttl: int = 86400):
    get_redis().set(groups_cache_key(user_id), json.dumps(groups, ensure_ascii=False), ex=ttl)


# ============================================================================
# Service layer — Telethon (user MTProto API)
# ============================================================================
class SessionNotAuthorizedError(RuntimeError):
    """Telethon session exists but is not authorized for a user account."""


def get_telethon_client_factory():
    """Construct a (not yet connected) TelegramClient for this invocation.

    Env vars are re-read on every call so tests and credential rotations take
    effect without re-importing the module.  Split out from connecting so the
    lifecycle (connect / authorize / disconnect) is independently testable.
    """
    api_id = int(_require_env(os.getenv("API_ID") or API_ID, "API_ID"))
    api_hash = _require_env(os.getenv("API_HASH") or API_HASH, "API_HASH")
    session = _require_env(
        os.getenv("TELEGRAM_SESSION_STRING") or TELEGRAM_SESSION_STRING,
        "TELEGRAM_SESSION_STRING",
    )
    return TelegramClient(StringSession(session), api_id, api_hash)


async def connect_authorized_client(client: TelegramClient) -> TelegramClient:
    """Connect a client and verify it belongs to an authorized account."""
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise SessionNotAuthorizedError(
                "Telegram user session is not authorized. "
                "Пересоздайте TELEGRAM_SESSION_STRING на авторизованном устройстве."
            )
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            logger.exception("Failed to disconnect Telethon client after error")
        raise
    return client


async def get_telethon_client() -> TelegramClient:
    """Create a connected, authorized Telethon client for this invocation."""
    return await connect_authorized_client(get_telethon_client_factory())


async def fetch_user_dialogs(client: TelegramClient) -> List[Dict[str, str]]:
    """Iterate ALL dialogs (no artificial limit); normalize groups/channels."""
    dialogs: List[Dict[str, str]] = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            dialogs.append({"id": str(dialog.id), "title": dialog.title or dialog.name or str(dialog.id)})
    return dialogs


async def broadcast_message(groups: List[str], text: str) -> Dict[str, Any]:
    """Send ``text`` to every group id; one failure never stops the rest."""
    client = await get_telethon_client()
    success = 0
    failures: List[Dict[str, str]] = []
    try:
        # A chat can occur more than once after combining a saved list with a
        # manual selection (and old tasks may already contain duplicates).
        # Telegram has no request-level idempotency key for these sends, so
        # normalize and de-duplicate ids before making any network calls.
        unique_groups = list(dict.fromkeys(str(gid) for gid in groups))
        for gid in unique_groups:
            try:
                entity = await client.get_entity(int(gid))
                await client.send_message(entity, text, parse_mode="html")
                success += 1
            except (ChatWriteForbiddenError, UserBannedInChannelError):
                logger.exception("No write access to %s", gid)
                failures.append({"id": str(gid), "error": "Нет прав на отправку (бан/запрет)"})
            except ChannelPrivateError:
                logger.exception("Channel %s is private/inaccessible", gid)
                failures.append({"id": str(gid), "error": "Канал приватный или недоступен"})
            except PeerIdInvalidError:
                logger.exception("Peer id invalid for %s", gid)
                failures.append({"id": str(gid), "error": "Не удалось найти чат по ID"})
            except FloodWaitError as exc:
                logger.exception("FloodWait while sending to %s (wait %ss)", gid, exc.seconds)
                failures.append({"id": str(gid), "error": f"FloodWait: пауза {exc.seconds} сек."})
                break  # further sends would fail too; QStash retry will resume
            except (ValueError, TypeError):
                logger.exception("Malformed group id %r", gid)
                failures.append({"id": str(gid), "error": "Некорректный ID группы"})
            except RPCError:
                logger.exception("Telegram RPC error while sending to %s", gid)
                failures.append({"id": str(gid), "error": "Ошибка Telegram API"})
            await asyncio.sleep(0.3)  # gentle pacing between sends
    finally:
        try:
            await client.disconnect()
        except Exception:
            logger.exception("Failed to disconnect Telethon client")
    return {"success": success, "failures": failures}


# ============================================================================
# Service layer — QStash scheduler
# ============================================================================
_qstash_client: Optional[QStash] = None


def get_qstash() -> QStash:
    global _qstash_client
    if _qstash_client is None:
        _require_env(os.getenv("QSTASH_TOKEN"), "QSTASH_TOKEN")
        _qstash_client = QStash(token=os.environ["QSTASH_TOKEN"])
    return _qstash_client


def schedule_process(task_id: str, delay_minutes: int, expected_repeat: int = 0):
    """Publish a delayed delivery of the task to /api/process via QStash."""
    get_qstash().message.publish_json(
        url=f"{APP_URL}/api/process",
        body={"task_id": task_id, "expected_repeat": int(expected_repeat)},
        delay=f"{int(delay_minutes)}m",
    )


def schedule_process_in_seconds(task_id: str, delay_seconds: float, expected_repeat: int):
    """Publish a delivery using the remaining wall-clock delay.

    QStash delays are relative.  This variant is used when QStash calls the
    endpoint before the persisted ``next_run`` timestamp (for example after a
    clock skew or a manually replayed request), so rounding to whole minutes
    cannot make a timer run early.
    """
    seconds = max(1, math.ceil(delay_seconds))
    get_qstash().message.publish_json(
        url=f"{APP_URL}/api/process",
        body={"task_id": task_id, "expected_repeat": int(expected_repeat)},
        delay=f"{seconds}s",
    )


# ============================================================================
# aiogram Bot / Dispatcher (single instance, single handler registry)
# ============================================================================
def create_bot() -> Bot:
    token = _require_env(BOT_TOKEN, "BOT_TOKEN")
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


dp = Dispatcher(storage=_build_fsm_storage())

ACTIVE_TASK_STATUSES = ("active", "pending")

# Max seconds to wait for Telethon to fetch the dialog list before giving up.
TELETHON_FETCH_TIMEOUT = float(os.environ.get("TELETHON_FETCH_TIMEOUT", "25"))


async def safe_answer(callback: types.CallbackQuery, text: Optional[str] = None, **kwargs):
    """Answer a callback exactly once; never let answering crash a handler."""
    try:
        if text is None:
            await callback.answer()
        else:
            await callback.answer(text, **kwargs)
    except Exception:
        # A stale/expired callback must not break the flow — but real bugs
        # (AttributeError etc.) should still be visible in logs with traceback.
        logger.exception("Unable to answer callback %r (stale?)", callback.data)


async def safe_edit(callback: types.CallbackQuery, text: str, reply_markup=None) -> bool:
    """Edit callback message; tolerate 'message is not modified' and stale msgs."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramRetryAfter as exc:
        logger.info("Telegram edit flood wait: retry after %ss", exc.retry_after)
        await safe_answer(callback, "Слишком много изменений подряд, попробуйте через секунду", show_alert=True)
        return False
    except Exception as exc:
        message = str(exc).lower()
        if "not modified" in message or "message can't be edited" in message:
            return True  # nothing to do — content already identical
        logger.exception("Failed to edit message for callback %r", callback.data)
        await safe_answer(callback, "Сообщение устарело, откройте меню заново", show_alert=True)
        return False


# ============================================================================
# Keyboards
# ============================================================================
def main_menu_keyboard():
    kb = [
        [InlineKeyboardButton(text="📨 Новая рассылка", callback_data="new_broadcast")],
        [InlineKeyboardButton(text="📚 Мои шаблоны", callback_data="templates")],
        [InlineKeyboardButton(text="🔁 Последнее сообщение", callback_data="last_message")],
        [InlineKeyboardButton(text="📊 Мои таймеры", callback_data="tasks")],
        [InlineKeyboardButton(text="📋 Мои группы", callback_data="groups")],
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


def groups_selection_keyboard(selected_ids: List[str], all_groups: List[Dict[str, str]], page: int = 0):
    page_size = 20
    page_count = max(1, (len(all_groups) + page_size - 1) // page_size)
    page = max(0, min(int(page), page_count - 1))
    kb = []
    for g in all_groups[page * page_size:(page + 1) * page_size]:
        gid = str(g["id"])
        symbol = "☑️" if gid in selected_ids else "☐"
        title = g.get("title") or gid
        kb.append([InlineKeyboardButton(text=f"{symbol} {title[:56]}", callback_data=f"toggle_group:{gid}")])

    if page_count > 1:
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(text="◀️", callback_data=f"groups_page:{page - 1}"))
        navigation.append(InlineKeyboardButton(text=f"Страница {page + 1}/{page_count}", callback_data="ignore"))
        if page < page_count - 1:
            navigation.append(InlineKeyboardButton(text="▶️", callback_data=f"groups_page:{page + 1}"))
        kb.append(navigation)
    kb.append([InlineKeyboardButton(text="☑️ Выбрать все", callback_data="select_all_groups")])
    kb.append([InlineKeyboardButton(text="➡️ Продолжить", callback_data="continue_to_send")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def group_lists_keyboard(group_lists: list, prefix: str, back_callback: str = "back_menu"):
    kb = [
        [InlineKeyboardButton(text=f"📋 {item['name']} ({len(item['groups'])})", callback_data=f"{prefix}:{item['id']}")]
        for item in group_lists
    ]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def group_source_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Выбрать каналы вручную", callback_data="select_live_groups")],
        [InlineKeyboardButton(text="📋 Использовать сохранённый список", callback_data="select_group_list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="new_broadcast")],
    ])


def send_decision_keyboard():
    kb = [
        [InlineKeyboardButton(text="⚡ Отправить сейчас", callback_data="send_now")],
        [InlineKeyboardButton(text="⏰ Задать таймер", callback_data="schedule_task")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def parse_positive_integer(value: Optional[str]) -> Optional[int]:
    """Return a positive integer entered by a user, or ``None`` if invalid."""
    try:
        number = int((value or "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


async def create_and_schedule_task(user_id: int, state: Dict[str, Any]) -> str:
    """Persist a broadcast task and publish its first QStash delivery."""
    interval_minutes = int(state["interval_minutes"])
    total_repeats = int(state["total_repeats"])
    task_id = str(uuid.uuid4())
    task_data = {
        "id": task_id,
        "user_id": int(user_id),
        "message": state["message_text"],
        "groups": list(dict.fromkeys(str(gid) for gid in state["selected_groups"])),
        "interval_minutes": interval_minutes,
        "completed_repeats": 0,
        "total_repeats": total_repeats,
        "status": "active",
        "created_at": time.time(),
        "next_run": time.time() + interval_minutes * 60,
    }
    save_task(task_data)
    get_redis().sadd(f"broadcast:user:{user_id}:tasks", task_id)

    try:
        schedule_process(task_id, interval_minutes, expected_repeat=0)
    except Exception:
        task_data["status"] = "error"
        save_task(task_data)
        raise
    return task_id


# ============================================================================
# Groups selection flow (shared business logic; handlers own callback.answer)
# ============================================================================
async def show_groups_picker(callback: types.CallbackQuery, user_id: int) -> None:
    """Render the live-groups keyboard, refreshing the Telethon cache as needed."""
    state = get_user_state(user_id) or {}
    cached = load_groups_cache(user_id)

    if cached is None:
        try:
            client = await get_telethon_client()
        except SessionNotAuthorizedError as exc:
            logger.exception("Telethon session unauthorized for user %s", user_id)
            await safe_answer(callback, f"⛔ {exc}", show_alert=True)
            return
        except MissingEnvError as exc:
            await safe_answer(callback, f"⛔ {exc}. Настройте переменные окружения.", show_alert=True)
            return
        except Exception as exc:
            logger.exception("Telethon connection failed for user %s", user_id)
            await safe_answer(callback, f"⛔ Не удалось подключиться к Telegram: {exc}", show_alert=True)
            return

        try:
            groups = await asyncio.wait_for(fetch_user_dialogs(client), timeout=TELETHON_FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            logger.exception("Telethon dialog fetch timed out for user %s", user_id)
            await safe_answer(callback, "⛔ Не удалось получить список групп (таймаут). Попробуйте ещё раз.",
                              show_alert=True)
            return
        finally:
            try:
                await client.disconnect()
            except Exception:
                logger.exception("Telethon disconnect failed")
        # Never overwrite a good cache with an empty result.
        if groups:
            save_groups_cache(user_id, groups)
        else:
            groups = []
    else:
        groups = cached

    if not groups:
        await safe_answer(
            callback,
            "Telegram вернул 0 групп/каналов. Проверьте, что аккаунт состоит в группах.",
            show_alert=True,
        )
        return

    selected = [str(x) for x in state.get("selected_groups", [])]
    page = int(state.get("group_page", 0) or 0)
    ok = await safe_edit(callback, "📋 Выберите группы для рассылки:", groups_selection_keyboard(selected, groups, page))
    if ok:
        await safe_answer(callback)


@dp.callback_query(F.data == "select_live_groups")
async def cb_select_live_groups(callback: types.CallbackQuery):
    try:
        await show_groups_picker(callback, callback.from_user.id)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("toggle_group:"))
async def cb_toggle_group(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        gid = callback.data.split(":", 1)[1]
        state = get_user_state(user_id) or {}
        selected = [str(x) for x in state.get("selected_groups", [])]

        if gid in selected:
            selected.remove(gid)
        else:
            selected.append(gid)

        groups = load_groups_cache(user_id)
        if groups is None:
            # Cache missing/expired — do NOT persist a selection the user
            # cannot see; just tell them to refresh the picker.
            await safe_answer(callback, "Список групп устарел, нажмите «Выбрать каналы вручную»", show_alert=True)
            return

        state["selected_groups"] = selected
        set_user_state(user_id, state)

        page = int(state.get("group_page", 0) or 0)
        await safe_edit(callback, "📋 Выберите группы для рассылки:",
                        groups_selection_keyboard(selected, groups, page))
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "select_all_groups")
async def cb_select_all_groups(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        groups = load_groups_cache(user_id)
        if groups is None:
            await safe_answer(callback, "Список групп устарел, обновите его", show_alert=True)
            return
        all_ids = [str(g["id"]) for g in groups]
        state = get_user_state(user_id) or {}
        state["selected_groups"] = all_ids
        set_user_state(user_id, state)
        page = int(state.get("group_page", 0) or 0)
        await safe_edit(callback, "📋 Выберите группы для рассылки:",
                        groups_selection_keyboard(all_ids, groups, page))
        await safe_answer(callback, f"Выбрано групп: {len(all_ids)}")
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("groups_page:"))
async def cb_groups_page(callback: types.CallbackQuery):
    try:
        try:
            page = int(callback.data.split(":", 1)[1])
        except ValueError:
            await safe_answer(callback)
            return
        user_id = callback.from_user.id
        groups = load_groups_cache(user_id)
        if groups is None:
            await safe_answer(callback, "Список групп устарел, обновите его", show_alert=True)
            return
        page = max(0, min(page, max(0, (len(groups) - 1) // 20)))
        state = get_user_state(user_id) or {}
        state["group_page"] = page
        set_user_state(user_id, state)
        await safe_edit(callback, "📋 Выберите группы для рассылки:",
                        groups_selection_keyboard([str(x) for x in state.get("selected_groups", [])], groups, page))
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "ignore")
async def cb_ignore(callback: types.CallbackQuery):
    await safe_answer(callback)


# ============================================================================
# Menu / command handlers
# ============================================================================
@dp.message(Command("start", "s"))
async def cmd_start(message: types.Message):
    try:
        clear_user_state(message.from_user.id)
    except Exception:
        logger.exception("Unable to clear state for user %s", message.from_user.id)
    await message.answer("🤖 Панель рассылки\n\nВыбери действие:", reply_markup=main_menu_keyboard())


@dp.callback_query(F.data == "back_menu")
async def cb_back_menu(callback: types.CallbackQuery):
    try:
        clear_user_state(callback.from_user.id)
        await safe_edit(callback, "🤖 Панель рассылки\n\nВыбери действие:", main_menu_keyboard())
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "new_broadcast")
async def cb_new_broadcast(callback: types.CallbackQuery):
    try:
        await safe_edit(callback, "📋 Выбор действия для новой рассылки:", broadcast_action_keyboard())
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "write_message")
async def cb_write_message(callback: types.CallbackQuery):
    try:
        set_user_state(callback.from_user.id, {"step": "waiting_for_message"})
        await safe_edit(callback, "📝 Введите текст сообщения для рассылки:", cancel_keyboard())
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "use_last_message")
async def cb_use_last_message(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        last_msg = get_last_message(user_id)
        if not last_msg:
            await safe_answer(callback, "⚠️ Последнее сообщение не найдено.", show_alert=True)
            return
        set_user_state(user_id, {"step": "selecting_groups", "message_text": last_msg, "selected_groups": []})
        await safe_edit(callback, "Выберите источник каналов:", group_source_keyboard())
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "last_message")
async def cb_last_message(callback: types.CallbackQuery):
    """Open the last-message flow from the main menu."""
    await cb_use_last_message(callback)


@dp.callback_query(F.data == "select_template")
async def cb_select_template(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        templates = get_user_templates(user_id)
        if not templates:
            await safe_answer(callback, "У вас нет шаблонов — переходим к написанию сообщения", show_alert=True)
            set_user_state(user_id, {"step": "waiting_for_message"})
            await safe_edit(callback, "📝 Введите текст сообщения для рассылки:", cancel_keyboard())
            return
        kb = [[InlineKeyboardButton(text=t["name"], callback_data=f"use_template:{t['id']}")] for t in templates]
        kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="new_broadcast")])
        await safe_edit(callback, "📚 Выберите шаблон:", InlineKeyboardMarkup(inline_keyboard=kb))
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("use_template:"))
async def cb_use_template(callback: types.CallbackQuery):
    try:
        template_id = callback.data.split(":", 1)[1]
        user_id = callback.from_user.id
        template = next((t for t in get_user_templates(user_id) if t.get("id") == template_id), None)
        if not template:
            await safe_answer(callback, "Шаблон не найден.", show_alert=True)
            return
        set_user_state(user_id, {"step": "selecting_groups", "message_text": template["message"], "selected_groups": []})
        await safe_edit(callback, "Выберите источник каналов:", group_source_keyboard())
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "select_group_list")
async def cb_select_group_list(callback: types.CallbackQuery):
    try:
        group_lists = get_group_lists(callback.from_user.id)
        if not group_lists:
            await safe_answer(callback, "Сохранённых списков пока нет.", show_alert=True)
            return
        await safe_edit(callback, "📋 Выберите список каналов для рассылки:",
                        group_lists_keyboard(group_lists, "use_group_list", "new_broadcast"))
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("use_group_list:"))
async def cb_use_group_list(callback: types.CallbackQuery):
    try:
        list_id = callback.data.split(":", 1)[1]
        group_list = next((item for item in get_group_lists(callback.from_user.id) if item["id"] == list_id), None)
        state = get_user_state(callback.from_user.id)
        if not group_list or not state or not state.get("message_text"):
            await safe_answer(callback, "Список или сообщение не найдены.", show_alert=True)
            return
        state["selected_groups"] = group_list["groups"]
        set_user_state(callback.from_user.id, state)
        await safe_edit(
            callback,
            f"📋 Используется список «{group_list['name']}» ({len(group_list['groups'])} каналов).\n\nКак отправить сообщение?",
            send_decision_keyboard(),
        )
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "continue_to_send")
async def cb_continue_to_send(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        state = get_user_state(user_id) or {}
        if not state.get("selected_groups"):
            await safe_answer(callback, "Выберите хотя бы одну группу.", show_alert=True)
            return

        if state.get("step") == "selecting_group_list_channels":
            state["step"] = "waiting_for_group_list_name"
            set_user_state(user_id, state)
            await safe_edit(callback, "Введите название списка каналов:", cancel_keyboard())
            await safe_answer(callback)
            return

        await safe_edit(callback, "Как отправить сообщение?", send_decision_keyboard())
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "send_now")
async def cb_send_now(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    try:
        state = get_user_state(user_id) or {}
        msg_text = state.get("message_text")
        groups = state.get("selected_groups", [])
        if not msg_text or not groups:
            await safe_answer(callback, "Ошибка данных: нет сообщения или групп.", show_alert=True)
            return

        await safe_edit(callback, "⏳ Отправка...")
        await safe_answer(callback)

        result = await broadcast_message(groups, msg_text)
        save_last_message(user_id, msg_text)
        clear_user_state(user_id)

        lines = [f"✅ Рассылка завершена\n\n📨 Успешно: {result['success']}\n❌ Ошибок: {len(result['failures'])}"]
        for failure in result["failures"][:10]:
            lines.append(f"• {failure['id']}: {failure['error']}")
        await safe_edit(callback, "\n".join(lines), main_menu_keyboard())
    except MissingEnvError as exc:
        logger.error("Broadcast blocked: %s", exc)
        await safe_edit(callback, f"⛔ {exc}. Настройте переменные окружения.")
    except SessionNotAuthorizedError as exc:
        logger.exception("Broadcast blocked: session unauthorized")
        await safe_edit(callback, f"⛔ {exc}")
    except Exception as exc:
        logger.exception("Send-now failed for user %s", user_id)
        await safe_edit(callback, f"❌ Ошибка отправки: {exc}")


@dp.callback_query(F.data == "schedule_task")
async def cb_schedule_task(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        state = get_user_state(user_id) or {}
        if not state.get("message_text") or not state.get("selected_groups"):
            await safe_answer(callback, "Ошибка данных: нет сообщения или групп.", show_alert=True)
            return
        state["step"] = "waiting_for_interval"
        set_user_state(user_id, state)
        await safe_edit(
            callback,
            "⏰ Введите интервал в минутах между отправками (целое число больше 0):",
            cancel_keyboard(),
        )
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "cancel")
async def cb_cancel(callback: types.CallbackQuery):
    try:
        clear_user_state(callback.from_user.id)
        await safe_edit(callback, "Отменено.", main_menu_keyboard())
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


# ============================================================================
# Templates
# ============================================================================
@dp.callback_query(F.data == "templates")
async def cb_templates(callback: types.CallbackQuery):
    try:
        await render_templates(callback)
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


async def render_templates(callback: types.CallbackQuery):
    """Render the templates screen (no callback.answer — callers own it)."""
    templates = get_user_templates(callback.from_user.id)
    kb = [[InlineKeyboardButton(text="➕ Создать шаблон", callback_data="create_template")]]
    kb.extend([InlineKeyboardButton(text=f"📝 {item['name']}", callback_data=f"manage_template:{item['id']}")]
              for item in templates)
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")])
    await safe_edit(callback, "📚 Шаблоны сообщений:", InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data == "create_template")
async def cb_create_template(callback: types.CallbackQuery):
    try:
        set_user_state(callback.from_user.id, {"step": "waiting_for_template_name"})
        await safe_edit(callback, "Введите название шаблона:", cancel_keyboard())
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("manage_template:"))
async def cb_manage_template(callback: types.CallbackQuery):
    try:
        template_id = callback.data.split(":", 1)[1]
        template = next((item for item in get_user_templates(callback.from_user.id) if item["id"] == template_id), None)
        if not template:
            await safe_answer(callback, "Шаблон не найден.", show_alert=True)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_template:{template_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="templates")],
        ])
        body = html.unescape(template["message"])
        await safe_edit(callback, f"📝 <b>{html.escape(template['name'])}</b>\n\n{html.escape(body)}", kb)
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("delete_template:"))
async def cb_delete_template(callback: types.CallbackQuery):
    try:
        template_id = callback.data.split(":", 1)[1]
        templates = [item for item in get_user_templates(callback.from_user.id) if item["id"] != template_id]
        save_user_templates(callback.from_user.id, templates)
        await render_templates(callback)
        await safe_answer(callback, "🗑 Шаблон удалён.")
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


# ============================================================================
# Timers (tasks)
# ============================================================================
def _task_status_label(status: str) -> str:
    return {
        "active": "🟢 Активен",
        "pending": "🟡 В очереди",
        "completed": "✅ Завершён",
        "cancelled": "🚫 Отменён",
        "error": "❌ Ошибка",
        "failed": "❌ Ошибка",
    }.get(status, f"⚪ {status or 'нет статуса'}")


def _format_task_details(task: Dict[str, Any]) -> str:
    task_id = task.get("id") or task.get("task_id") or "?"
    lines = [
        f"⏰ <b>Таймер {str(task_id)[:8]}</b>",
        f"Статус: {_task_status_label(task.get('status'))}",
        f"Интервал: {task.get('interval_minutes', 1)} мин.",
        f"Повторы: {task.get('completed_repeats', 0)}/{task.get('total_repeats', 1)}",
        f"Каналов: {len(task.get('groups', []))}",
    ]
    if task.get("status") in ACTIVE_TASK_STATUSES and task.get("next_run"):
        next_run = float(task["next_run"])
        remaining = max(0, math.ceil(next_run - time.time()))
        minutes, seconds = divmod(remaining, 60)
        lines.append(f"До запуска: {minutes} мин. {seconds:02d} сек.")
        lines.append(f"Следующий запуск: {time.strftime('%d.%m.%Y %H:%M:%S UTC', time.gmtime(next_run))}")
    message_preview = (task.get("message") or "").strip().replace("\n", " ")
    if message_preview:
        if len(message_preview) > 120:
            message_preview = message_preview[:120] + "…"
        lines.append(f"Сообщение: <i>{html.escape(message_preview)}</i>")
    return "\n".join(lines)


async def render_tasks(callback: types.CallbackQuery):
    """Render the timers screen showing ONLY active timers (no callback.answer — callers own it)."""
    all_tasks = get_user_tasks(callback.from_user.id)
    tasks = [t for t in all_tasks if t.get("status") in ACTIVE_TASK_STATUSES]
    header_lines = ["📊 <b>Мои таймеры</b>"]
    if not tasks:
        header_lines.append("\nАктивных таймеров нет. Создайте новый через «📨 Новая рассылка» → «⏰ Задать таймер».")
    else:
        header_lines.append(f"\nАктивных таймеров: {len(tasks)}\n")
        for i, task in enumerate(tasks, 1):
            tid = str(task.get("id") or task.get("task_id") or "?")[:8]
            status = {"active": "🟢", "pending": "🟡"}.get(task.get("status"), "⚪")
            header_lines.append(
                f"{i}. {status} {tid} — {task.get('completed_repeats', 0)}/{task.get('total_repeats', 1)} повторов, "
                f"интервал {task.get('interval_minutes', 1)} мин."
            )
    kb = []
    for task in tasks:
        task_id = task.get("id") or task.get("task_id")
        if not task_id:
            continue
        row = [InlineKeyboardButton(text=f"ℹ️ {str(task_id)[:8]}", callback_data=f"task_details:{task_id}")]
        row.append(InlineKeyboardButton(text="🚫 Отменить", callback_data=f"cancel_task:{task_id}"))
        kb.append(row)
    kb.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="tasks")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")])
    await safe_edit(callback, "\n".join(header_lines), InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data == "tasks")
async def cb_tasks(callback: types.CallbackQuery):
    try:
        await render_tasks(callback)
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("task_details:"))
async def cb_task_details(callback: types.CallbackQuery):
    try:
        task_id = callback.data.split(":", 1)[1]
        task = get_task(task_id)
        if not task or int(task.get("user_id", -1)) != int(callback.from_user.id):
            await safe_answer(callback, "Таймер не найден.", show_alert=True)
            return
        kb = []
        if task.get("status") in ACTIVE_TASK_STATUSES:
            kb.append([InlineKeyboardButton(text="🚫 Отменить таймер", callback_data=f"cancel_task:{task_id}")])
        kb.append([InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="tasks")])
        await safe_edit(callback, _format_task_details(task), InlineKeyboardMarkup(inline_keyboard=kb))
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("cancel_task:"))
async def cb_cancel_task(callback: types.CallbackQuery):
    try:
        task_id = callback.data.split(":", 1)[1]
        task = get_task(task_id)
        if not task or int(task.get("user_id", -1)) != int(callback.from_user.id):
            await safe_answer(callback, "Таймер не найден.", show_alert=True)
            return
        if task.get("status") not in ACTIVE_TASK_STATUSES:
            await safe_answer(callback, "Этот таймер уже не активен.", show_alert=True)
            return
        task["status"] = "cancelled"
        task["cancelled_at"] = time.time()
        save_task(task)
        # Re-render the timer list (render function does not answer), then
        # toast the result — exactly one callback.answer().
        await render_tasks(callback)
        await safe_answer(callback, "✅ Таймер отменён, рассылка остановлена.")
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


# ============================================================================
# Saved group lists
# ============================================================================
@dp.callback_query(F.data == "groups")
async def cb_groups(callback: types.CallbackQuery):
    try:
        group_lists = get_group_lists(callback.from_user.id)
        kb = [[InlineKeyboardButton(text="➕ Создать список", callback_data="create_group_list")]]
        kb.extend([[InlineKeyboardButton(text=f"📋 {item['name']} ({len(item['groups'])})",
                                         callback_data=f"manage_group_list:{item['id']}")]
                   for item in group_lists])
        kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")])
        await safe_edit(callback, "📋 Списки каналов для рассылки:", InlineKeyboardMarkup(inline_keyboard=kb))
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "create_group_list")
async def cb_create_group_list(callback: types.CallbackQuery):
    try:
        set_user_state(callback.from_user.id, {"step": "selecting_group_list_channels", "selected_groups": []})
        await show_groups_picker(callback, callback.from_user.id)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("manage_group_list:"))
async def cb_manage_group_list(callback: types.CallbackQuery):
    try:
        list_id = callback.data.split(":", 1)[1]
        group_list = next((item for item in get_group_lists(callback.from_user.id) if item["id"] == list_id), None)
        if not group_list:
            await safe_answer(callback, "Список не найден.", show_alert=True)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить список", callback_data=f"delete_group_list:{list_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="groups")],
        ])
        await safe_edit(callback,
                        f"📋 <b>{html.escape(group_list['name'])}</b>\nКаналов в списке: {len(group_list['groups'])}", kb)
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("delete_group_list:"))
async def cb_delete_group_list(callback: types.CallbackQuery):
    try:
        list_id = callback.data.split(":", 1)[1]
        save_group_lists(callback.from_user.id,
                         [item for item in get_group_lists(callback.from_user.id) if item["id"] != list_id])
        await cb_groups(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "back_broadcast")
async def cb_back_broadcast(callback: types.CallbackQuery):
    try:
        await safe_edit(callback, "📋 Выбор действия для новой рассылки:", broadcast_action_keyboard())
        await safe_answer(callback)
    except Exception:
        logger.exception("Callback failed: %s", callback.data)
        await safe_answer(callback, "❌ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


# ============================================================================
# Text-input flows (state machine stored in Redis)
# ============================================================================
@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    state = get_user_state(user_id)

    if state and state.get("step") == "waiting_for_template_name":
        name = (message.text or "").strip()
        if not name:
            await message.answer("Введите непустое название шаблона.", reply_markup=cancel_keyboard())
            return
        state["template_name"] = name
        state["step"] = "waiting_for_template_message"
        set_user_state(user_id, state)
        await message.answer("Введите текст шаблона:", reply_markup=cancel_keyboard())
        return

    if state and state.get("step") == "waiting_for_template_message":
        text = message.text or message.caption
        if not text:
            await message.answer("Отправьте текст шаблона.", reply_markup=cancel_keyboard())
            return
        templates = get_user_templates(user_id)
        templates.append({"id": str(uuid.uuid4()), "name": state["template_name"], "message": html.escape(text)})
        save_user_templates(user_id, templates)
        clear_user_state(user_id)
        await message.answer("✅ Шаблон сохранён.", reply_markup=main_menu_keyboard())
        return

    if state and state.get("step") == "waiting_for_group_list_name":
        name = (message.text or "").strip()
        if not name:
            await message.answer("Введите непустое название списка.", reply_markup=cancel_keyboard())
            return
        group_lists = get_group_lists(user_id)
        group_lists.append({"id": str(uuid.uuid4()), "name": name, "groups": state["selected_groups"]})
        save_group_lists(user_id, group_lists)
        clear_user_state(user_id)
        await message.answer(f"✅ Список «{name}» сохранён.", reply_markup=main_menu_keyboard())
        return

    if state and state.get("step") == "waiting_for_interval":
        interval_minutes = parse_positive_integer(message.text)
        if interval_minutes is None:
            await message.answer("Введите целое число минут больше 0.", reply_markup=cancel_keyboard())
            return
        state["interval_minutes"] = interval_minutes
        state["step"] = "waiting_for_repeats"
        set_user_state(user_id, state)
        await message.answer("🔁 Введите количество отправок (целое число больше 0):", reply_markup=cancel_keyboard())
        return

    if state and state.get("step") == "waiting_for_repeats":
        total_repeats = parse_positive_integer(message.text)
        if total_repeats is None:
            await message.answer("Введите целое число повторов больше 0.", reply_markup=cancel_keyboard())
            return
        state["total_repeats"] = total_repeats
        try:
            task_id = await create_and_schedule_task(user_id, state)
        except MissingEnvError as exc:
            logger.error("Scheduler blocked: %s", exc)
            await message.answer(f"⛔ {exc}. Планировщик недоступен.", reply_markup=cancel_keyboard())
            return
        except Exception:
            logger.exception("QStash scheduling failed for user %s", user_id)
            await message.answer("❌ Ошибка планировщика QStash. Попробуйте позже.", reply_markup=cancel_keyboard())
            return
        clear_user_state(user_id)
        await message.answer(
            f"⏰ Таймер установлен. Задача {task_id[:8]} начнётся через "
            f"{state['interval_minutes']} мин. и выполнится {total_repeats} раз.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if state and state.get("step") == "waiting_for_message":
        text = message.text or message.caption
        if not text:
            await message.answer("Пожалуйста, отправьте текст.")
            return
        safe_text = html.escape(text)
        set_user_state(user_id, {"step": "selecting_groups", "message_text": safe_text, "selected_groups": []})
        await message.answer("Выберите источник каналов:", reply_markup=group_source_keyboard())
        return

    await message.answer("Неизвестная команда. Нажмите /start")


# ============================================================================
# FastAPI application
# ============================================================================
# Keep the constructor assignment simple: Vercel discovers FastAPI entry points
# statically and expects a module-level ``app = FastAPI()`` declaration.
app = FastAPI(title="Telegram Broadcast Bot")


@app.get("/")
async def root():
    return {"ok": True, "service": "telegram-broadcast-bot"}


@app.get("/health")
async def health_check():
    """Pure env-config check — performs NO network calls (serverless-safe)."""
    return {
        "status": "ok",
        "bot_configured": bool(BOT_TOKEN),
        "redis_configured": bool(os.getenv("UPSTASH_REDIS_REST_URL") and os.getenv("UPSTASH_REDIS_REST_TOKEN")),
        "telegram_configured": bool(API_ID and API_HASH and TELEGRAM_SESSION_STRING),
        "qstash_configured": bool(os.getenv("QSTASH_TOKEN")),
        "webhook_secret_configured": bool(TELEGRAM_WEBHOOK_SECRET),
    }


def _check_qstash_signature(request: Request, signature: Optional[str]) -> bool:
    """Best-effort QStash signed-delivery verification.

    Returns True when verification is not configured (legacy deployments),
    otherwise validates the current verified-delivery format.
    """
    if not QSTASH_VERIFICATION_KEY:
        return True
    if not signature:
        return False
    try:
        import base64
        import hmac
        import hashlib

        parts = signature.split(",")
        timestamp = next(p.split("=", 1)[1] for p in parts if p.startswith("t="))
        signatures = [p.split("=", 1)[1] for p in parts if p.startswith("v1=")]
        signed_body = f"{timestamp}.{request['url'].path}".encode()
        for candidate in signatures:
            expected = base64.b64encode(
                hmac.new(base64.b64decode(QSTASH_VERIFICATION_KEY), signed_body, hashlib.sha256).digest()
            ).decode()
            if hmac.compare_digest(expected, candidate):
                return True
        return False
    except Exception:
        logger.exception("QStash signature verification failed")
        return False


@app.post("/api/webhook")
async def webhook_handler(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None, alias="X-Telegram-Bot-Api-Secret-Token"),
):
    """Receive Telegram updates (message / callback_query / edited_message)."""
    if TELEGRAM_WEBHOOK_SECRET and x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret token")

    try:
        body = await request.json()
    except Exception:
        logger.exception("Webhook received non-JSON body")
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Safe diagnostics — never log secrets.
    update_type = next((k for k in ("message", "edited_message", "callback_query", "inline_query") if k in body), "unknown")
    callback_data = ((body.get("callback_query") or {}).get("data"))
    user_id = (
        ((body.get("callback_query") or {}).get("from") or {}).get("id")
        or ((body.get("message") or {}).get("from") or {}).get("id")
    )
    logger.info(
        "Incoming Telegram update id=%s type=%s data=%r user_id=%s",
        body.get("update_id"), update_type, callback_data, user_id,
    )

    bot = None
    try:
        update = Update(**body)
        bot = create_bot()
        await dp.feed_update(bot, update)
        return JSONResponse(content={"ok": True})
    except Exception:
        logger.exception("Webhook processing error (update_id=%s)", body.get("update_id"))
        # Always 200-ish for handled-but-failed updates? No — surface real
        # errors so Telegram retries; malformed updates were rejected above.
        raise HTTPException(status_code=500, detail="Update processing failed")
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:
                logger.exception("Failed to close bot session")


def _webhook_url() -> str:
    return f"{APP_URL}/api/webhook"


async def _call_set_webhook() -> Dict[str, Any]:
    bot = create_bot()
    try:
        await bot.set_webhook(
            _webhook_url(),
            secret_token=TELEGRAM_WEBHOOK_SECRET or None,
            allowed_updates=["message", "edited_message", "callback_query"],
            drop_pending_updates=False,
        )
        info = await bot.get_webhook_info()
    finally:
        await bot.session.close()
    return {
        "url": info.url,
        "pending_update_count": info.pending_update_count,
        "last_error_date": info.last_error_date,
        "last_error_message": info.last_error_message,
        "allowed_updates": info.allowed_updates,
        "max_connections": info.max_connections,
    }


@app.post("/api/setup-webhook")
async def setup_webhook(x_setup_secret: Optional[str] = Header(None, alias="X-Setup-Secret")):
    """Explicitly (re)install the Telegram webhook. Never runs automatically."""
    if not WEBHOOK_SETUP_SECRET:
        raise HTTPException(status_code=501, detail="WEBHOOK_SETUP_SECRET is not configured on the server")
    if not x_setup_secret or x_setup_secret != WEBHOOK_SETUP_SECRET:
        raise HTTPException(status_code=403, detail="Missing or invalid X-Setup-Secret header")
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN is not configured")

    try:
        info = await _call_set_webhook()
    except TelegramRetryAfter as exc:
        logger.warning("setWebhook flood-controlled; retry after %ss", exc.retry_after)
        raise HTTPException(status_code=429, detail=f"Telegram flood control: retry after {exc.retry_after}s")
    except Exception:
        logger.exception("setWebhook failed")
        raise HTTPException(status_code=502, detail="Telegram setWebhook failed")
    return {"status": "ok", "webhook_url": _webhook_url(), "webhook_info": info}


@app.get("/api/webhook-info")
async def webhook_info(x_setup_secret: Optional[str] = Header(None, alias="X-Setup-Secret")):
    """Safe webhook diagnostics (no secrets are ever returned)."""
    if not WEBHOOK_SETUP_SECRET:
        raise HTTPException(status_code=501, detail="WEBHOOK_SETUP_SECRET is not configured on the server")
    if not x_setup_secret or x_setup_secret != WEBHOOK_SETUP_SECRET:
        raise HTTPException(status_code=403, detail="Missing or invalid X-Setup-Secret header")
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN is not configured")
    try:
        return {"status": "ok", "webhook_info": await _call_set_webhook_info()}
    except Exception:
        logger.exception("getWebhookInfo failed")
        raise HTTPException(status_code=502, detail="Telegram getWebhookInfo failed")


async def _call_set_webhook_info() -> Dict[str, Any]:
    bot = create_bot()
    try:
        info = await bot.get_webhook_info()
    finally:
        await bot.session.close()
    return {
        "url": info.url,
        "pending_update_count": info.pending_update_count,
        "last_error_date": info.last_error_date,
        "last_error_message": info.last_error_message,
        "allowed_updates": info.allowed_updates,
        "max_connections": info.max_connections,
    }


@app.post("/api/admin/cancel-task")
async def admin_cancel_task(
    request: Request,
    x_setup_secret: Optional[str] = Header(None, alias="X-Setup-Secret"),
):
    """Secret-protected maintenance endpoint: force-cancel one or more tasks.

    Body: {"task_ids": ["09f58875", ...]}  (full or prefix of task id).
    Marks tasks cancelled in Redis and removes them from the user's index so
    they disappear from "Мои таймеры" immediately.
    """
    if not WEBHOOK_SETUP_SECRET or x_setup_secret != WEBHOOK_SETUP_SECRET:
        raise HTTPException(status_code=403, detail="Missing or invalid X-Setup-Secret header")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    raw_ids = body.get("task_ids") or ([body["task_id"]] if body.get("task_id") else [])
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(status_code=400, detail="Provide \"task_ids\": [...] or \"task_id\"")

    redis = get_redis()
    all_keys = redis.keys("broadcast:task:*") or []
    results = {}
    for raw in raw_ids:
        raw = str(raw).strip()
        matched = [k for k in all_keys if k.split("broadcast:task:", 1)[1].startswith(raw)]
        if not matched:
            results[raw] = "not_found"
            continue
        for key in matched:
            task_id = key.split("broadcast:task:", 1)[1]
            task = _safe_loads(redis.get(key), default=None)
            if not task:
                continue
            task["status"] = "cancelled"
            redis.set(key, json.dumps(task, ensure_ascii=False))
            user_id = task.get("user_id")
            if user_id is not None:
                redis.srem(f"broadcast:user:{user_id}:tasks", task_id)
        results[task_id] = "cancelled"
    return {"status": "ok", "results": results}


@app.post("/api/delete-webhook")
async def delete_webhook(x_setup_secret: Optional[str] = Header(None, alias="X-Setup-Secret")):
    if not WEBHOOK_SETUP_SECRET or x_setup_secret != WEBHOOK_SETUP_SECRET:
        raise HTTPException(status_code=403, detail="Missing or invalid X-Setup-Secret header")
    bot = None
    try:
        bot = create_bot()
        await bot.delete_webhook(drop_pending_updates=False)
        return {"status": "ok"}
    except Exception:
        logger.exception("deleteWebhook failed")
        raise HTTPException(status_code=502, detail="Telegram deleteWebhook failed")
    finally:
        if bot is not None:
            await bot.session.close()


# Startup intentionally does NOTHING over the network: no setWebhook, no
# Telethon connect, no Redis migrations.  Webhook installation is a separate
# controlled operation (/api/setup-webhook).  This keeps cold starts cheap and
# immune to Telegram flood control.


@app.post("/api/process")
async def process_task(request: Request):
    """QStash-triggered task execution with lock-based duplicate protection."""
    signature = request.headers.get("Signature")
    if QSTASH_VERIFICATION_KEY and not _check_qstash_signature(request, signature):
        raise HTTPException(status_code=401, detail="Invalid QStash signature")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    task_id = body.get("task_id")
    if not task_id:
        raise HTTPException(status_code=400, detail="No task_id")
    expected_repeat = body.get("expected_repeat")
    if expected_repeat is not None:
        try:
            expected_repeat = int(expected_repeat)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid expected_repeat")

    # Idempotency across repeated deliveries: QStash exposes a stable
    # Message-Id header for every invocation (retries carry the same id), so
    # we dedupe on it first.  Additionally we lock per (task, repeat) so even
    # legacy publishers without a Message-Id cannot send the same repeat twice.
    message_id = request.headers.get("Message-Id") or request.headers.get("message-id")
    msg_key = None
    if message_id:
        msg_key = f"broadcast:qstash_message:{message_id}"
        try:
            seen_before = bool(get_redis().get(msg_key))
        except Exception:
            logger.exception("Message-Id dedupe check failed; continuing")
            seen_before = False
        if seen_before:
            logger.info("Duplicate QStash delivery (Message-Id %s) ignored", message_id)
            return JSONResponse(content={"ok": True, "status": "duplicate_ignored"})

    pre_task = get_task(task_id)
    if not pre_task:
        return JSONResponse(content={"ok": False, "error": "Task not found"})
    repeat_no = int(pre_task.get("completed_repeats", 0))
    dedupe_key = f"broadcast:processed:{task_id}:{repeat_no}"
    try:
        first_delivery = get_redis().set(dedupe_key, "1", nx=True, ex=7 * 24 * 3600)
    except Exception:
        logger.exception("Dedupe check failed; continuing with lock only")
        first_delivery = True
    if not first_delivery:
        logger.info("Duplicate QStash delivery for task %s repeat %s ignored", task_id, repeat_no)
        return JSONResponse(content={"ok": True, "status": "duplicate_ignored"})

    def _mark_message_seen():
        """Record Message-Id as processed (only after the run is committed)."""
        if msg_key:
            try:
                get_redis().set(msg_key, "1", ex=7 * 24 * 3600)
            except Exception:
                logger.exception("Failed to mark Message-Id %s as seen", message_id)

    if not acquire_task_lock(task_id, seconds=120):
        # The dedupe marker was reserved above, but this delivery did not do
        # any work.  Remove it so a retry can process the repeat after the
        # in-flight worker (or an expired stale lock) is gone.
        try:
            get_redis().delete(dedupe_key)
        except Exception:
            logger.exception("Failed to release dedupe marker for locked task %s", task_id)
        logger.info("Task %s already processing", task_id)
        return JSONResponse(content={"ok": True, "status": "already_processing"})

    try:
        task = get_task(task_id)
        if not task or task.get("status") not in ACTIVE_TASK_STATUSES:
            _mark_message_seen()
            return JSONResponse(content={"ok": True, "status": "inactive_or_missing"})

        current_repeat = int(task.get("completed_repeats", 0))
        if expected_repeat is not None and expected_repeat != current_repeat:
            # This is an obsolete delivery from an earlier schedule.  Without
            # this generation check, a delayed duplicate of repeat N could be
            # mistaken for repeat N+1 and send the same message again.
            get_redis().delete(dedupe_key)
            _mark_message_seen()
            return JSONResponse(content={"ok": True, "status": "stale_delivery"})

        # Never rely solely on the delivery provider's clock.  A replayed or
        # prematurely delivered request must not shorten the timer selected by
        # the user.  Requeue for the exact remaining number of seconds without
        # consuming a repeat.
        now = time.time()
        next_run = float(task.get("next_run") or 0)
        if next_run > now + 1:
            schedule_process_in_seconds(task_id, next_run - now, current_repeat)
            get_redis().delete(dedupe_key)
            _mark_message_seen()
            return JSONResponse(content={"ok": True, "status": "not_due_yet"})

        result = await broadcast_message(task.get("groups", []), task.get("message", ""))
        success_count, failures = result["success"], result["failures"]

        task["completed_repeats"] = int(task.get("completed_repeats", 0)) + 1
        if task["completed_repeats"] >= int(task.get("total_repeats", 1)):
            task["status"] = "completed"
        else:
            interval_minutes = int(task.get("interval_minutes", 1))
            task["status"] = "active"
            task["next_run"] = time.time() + interval_minutes * 60
            try:
                schedule_process(
                    task_id,
                    interval_minutes,
                    expected_repeat=task["completed_repeats"],
                )
            except Exception:
                logger.exception("Unable to reschedule task %s", task_id)
                task["status"] = "error"
        save_task(task)
        # The run is committed (repeat counter persisted / task completed), so
        # this Message-Id must never execute again — record it now.  On an
        # unexpected error below we deliberately do NOT mark it, letting
        # QStash retry safely via the per-repeat dedupe key rollback.
        _mark_message_seen()

        # Notify owner about per-send failures (best effort).
        if failures and BOT_TOKEN:
            try:
                bot = create_bot()
                try:
                    report = "\n".join(f"• {f['id']}: {f['error']}" for f in failures[:10])
                    await bot.send_message(task["user_id"], f"⚠️ Часть рассылки не отправлена:\n{report}")
                finally:
                    await bot.session.close()
            except Exception:
                logger.exception("Failed to notify user %s about send errors", task.get("user_id"))

        return JSONResponse(content={"ok": True, "sent": success_count, "errors": len(failures)})
    except MissingEnvError as exc:
        logger.error("Process blocked: %s", exc)
        return JSONResponse(content={"ok": False, "error": str(exc)}, status_code=503)
    except SessionNotAuthorizedError as exc:
        logger.exception("Process blocked: Telethon session unauthorized")
        task = get_task(task_id)
        if task and task.get("status") in ACTIVE_TASK_STATUSES:
            task["status"] = "error"
            save_task(task)
        return JSONResponse(content={"ok": False, "error": str(exc)}, status_code=503)
    except Exception:
        logger.exception("Process error for task %s", task_id)
        # The run never committed: roll back the per-repeat dedupe key so a
        # QStash retry (or a new delivery) can attempt this repeat again.
        try:
            get_redis().delete(dedupe_key)
        except Exception:
            logger.exception("Failed to roll back dedupe key for task %s", task_id)
        raise HTTPException(status_code=500, detail="Task processing failed")
    finally:
        release_task_lock(task_id)
