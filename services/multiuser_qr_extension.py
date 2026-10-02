"""Safe multi-user Telegram account layer.

Personal Telegram accounts are connected with Telegram's QR login token flow.
The application never asks users for a Telegram login code or 2FA password.
Each connected StringSession is encrypted at rest and keyed by the bot user id.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from typing import Any, Dict, Optional

from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from services import telegram_delivery
from storage.telegram_accounts import (
    SessionEncryptionError,
    clear_group_cache,
    create_connect_challenge,
    delete_account,
    delete_connect_challenge,
    get_public_account,
    is_connected,
    load_connect_challenge,
    save_account,
)

logger = logging.getLogger("broadcast.multiuser")

QR_WAIT_SECONDS = 28


def install_multiuser(legacy) -> None:
    """Install multi-user UI/routes and ownership guards on api.index."""
    if getattr(legacy, "_MULTIUSER_QR_INSTALLED", False):
        return
    legacy._MULTIUSER_QR_INSTALLED = True

    original_create_immediate = legacy.create_immediate_run
    original_create_timer = legacy.create_timer_task

    def main_menu_keyboard():
        return legacy.InlineKeyboardMarkup(inline_keyboard=[
            [legacy.InlineKeyboardButton(text="📨 Новая рассылка", callback_data="new_broadcast")],
            [legacy.InlineKeyboardButton(text="📚 Мои шаблоны", callback_data="templates")],
            [legacy.InlineKeyboardButton(text="🔁 Последнее сообщение", callback_data="last_message")],
            [legacy.InlineKeyboardButton(text="📊 Мои таймеры", callback_data="tasks")],
            [legacy.InlineKeyboardButton(text="📋 Мои группы", callback_data="groups")],
            [legacy.InlineKeyboardButton(text="👤 Telegram-аккаунт", callback_data="telegram_account")],
        ])

    def require_personal_account(user_id: int) -> None:
        try:
            connected = is_connected(user_id)
        except SessionEncryptionError as exc:
            raise legacy.SessionNotAuthorizedError(str(exc)) from exc
        if not connected:
            raise legacy.SessionNotAuthorizedError(
                "Telegram-аккаунт не подключён. Откройте «👤 Telegram-аккаунт» "
                "и подтвердите вход через Telegram."
            )

    def create_immediate_run(*, user_id: int, message: str, groups):
        require_personal_account(user_id)
        return original_create_immediate(user_id=user_id, message=message, groups=groups)

    def create_timer_task(
        user_id: int,
        message: str,
        groups,
        interval_minutes: int,
        total_repeats,
    ):
        require_personal_account(user_id)
        return original_create_timer(
            user_id=user_id,
            message=message,
            groups=groups,
            interval_minutes=interval_minutes,
            total_repeats=total_repeats,
        )

    async def show_groups_picker(callback, user_id: int) -> None:
        state = legacy.get_user_state(user_id) or {}
        cached = legacy.load_groups_cache(user_id)

        if cached is None:
            try:
                client = await telegram_delivery.get_telethon_client(user_id)
            except Exception:
                logger.exception("Personal Telegram connection failed for user %s", user_id)
                await legacy.safe_answer(
                    callback,
                    "⛔ Telegram-аккаунт не подключён или авторизация истекла. "
                    "Откройте «👤 Telegram-аккаунт».",
                    show_alert=True,
                )
                return

            try:
                groups = await telegram_delivery.fetch_dialogs_with_timeout(client)
            except asyncio.TimeoutError:
                logger.exception("Dialog fetch timed out for user %s", user_id)
                await legacy.safe_answer(
                    callback,
                    "⛔ Не удалось получить список групп: таймаут Telegram.",
                    show_alert=True,
                )
                return
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    logger.exception("Telethon disconnect failed")

            if groups:
                legacy.save_groups_cache(user_id, groups)
            else:
                groups = []
        else:
            groups = cached

        if not groups:
            await legacy.safe_answer(
                callback,
                "Нет доступных групп или каналов для отправки. Каналы только для чтения скрыты.",
                show_alert=True,
            )
            return

        selected = [str(x) for x in state.get("selected_groups", [])]
        page = int(state.get("group_page", 0) or 0)
        ok = await legacy.safe_edit(
            callback,
            "📋 Выберите группы для рассылки:",
            legacy.groups_selection_keyboard(selected, groups, page),
        )
        if ok:
            await legacy.safe_answer(callback)

    # Existing handlers resolve these globals when they execute, so this
    # upgrades the stable UI without copying api/index.py.
    legacy.main_menu_keyboard = main_menu_keyboard
    legacy.create_immediate_run = create_immediate_run
    legacy.create_timer_task = create_timer_task
    legacy.show_groups_picker = show_groups_picker

    async def render_account(callback, *, reconnect: bool = False) -> None:
        user_id = int(callback.from_user.id)
        account = get_public_account(user_id)

        if account and not reconnect:
            username = account.get("username")
            label = (
                f"@{html.escape(username)}"
                if username
                else html.escape(account.get("first_name") or "Telegram user")
            )
            tg_id = account.get("telegram_user_id") or "?"
            phone = html.escape(str(account.get("phone_masked") or "скрыт"))
            text = (
                "👤 <b>Telegram-аккаунт</b>\n\n"
                "✅ Подключён\n"
                f"Аккаунт: {label}\n"
                f"Telegram ID: <code>{tg_id}</code>\n"
                f"Телефон: {phone}\n\n"
                "Ваши списки групп и рассылки выполняются только от этого аккаунта."
            )
            kb = legacy.InlineKeyboardMarkup(inline_keyboard=[
                [legacy.InlineKeyboardButton(text="🔄 Переподключить", callback_data="telegram_reconnect")],
                [legacy.InlineKeyboardButton(text="🚫 Отключить", callback_data="telegram_disconnect")],
                [legacy.InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")],
            ])
        else:
            try:
                token = create_connect_challenge(user_id)
            except Exception as exc:
                logger.exception("Unable to create connect challenge")
                await legacy.safe_edit(
                    callback,
                    f"⛔ Не удалось подготовить подключение: {html.escape(str(exc))}",
                    legacy.main_menu_keyboard(),
                )
                await legacy.safe_answer(callback)
                return

            url = f"{legacy.APP_URL}/connect/{token}"
            text = (
                "👤 <b>Telegram-аккаунт</b>\n\n"
                "Нажмите кнопку ниже. На защищённой странице будет создан короткоживущий "
                "Telegram QR-login token. Подтверждение выполняется в официальном Telegram.\n\n"
                "Бот <b>не просит и не принимает</b> код входа или пароль Telegram.\n"
                "Ссылка из этого сообщения действует 15 минут."
            )
            kb = legacy.InlineKeyboardMarkup(inline_keyboard=[
                [legacy.InlineKeyboardButton(text="🔐 Подключить Telegram", url=url)],
                [legacy.InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")],
            ])

        await legacy.safe_edit(callback, text, kb)
        await legacy.safe_answer(callback)

    @legacy.dp.callback_query(legacy.F.data == "telegram_account")
    async def cb_telegram_account(callback):
        try:
            await render_account(callback)
        except Exception:
            logger.exception("telegram_account callback failed")
            await legacy.safe_answer(callback, "❌ Не удалось открыть аккаунт", show_alert=True)

    @legacy.dp.callback_query(legacy.F.data == "telegram_reconnect")
    async def cb_telegram_reconnect(callback):
        try:
            await render_account(callback, reconnect=True)
        except Exception:
            logger.exception("telegram_reconnect callback failed")
            await legacy.safe_answer(callback, "❌ Не удалось создать ссылку", show_alert=True)

    @legacy.dp.callback_query(legacy.F.data == "telegram_disconnect")
    async def cb_telegram_disconnect(callback):
        kb = legacy.InlineKeyboardMarkup(inline_keyboard=[
            [legacy.InlineKeyboardButton(text="✅ Да, отключить", callback_data="telegram_disconnect_confirm")],
            [legacy.InlineKeyboardButton(text="⬅️ Назад", callback_data="telegram_account")],
        ])
        await legacy.safe_edit(
            callback,
            "Отключить Telegram-аккаунт? Активные таймеры этого пользователя будут остановлены.",
            kb,
        )
        await legacy.safe_answer(callback)

    @legacy.dp.callback_query(legacy.F.data == "telegram_disconnect_confirm")
    async def cb_telegram_disconnect_confirm(callback):
        user_id = int(callback.from_user.id)
        try:
            delete_account(user_id)
            clear_group_cache(user_id)
            for task in legacy.get_user_tasks(user_id):
                if task.get("status") in legacy.ACTIVE_TASK_STATUSES:
                    task["status"] = "cancelled"
                    task["next_run"] = None
                    task["last_error"] = "Telegram account disconnected"
                    legacy.save_task(task)
            await legacy.safe_edit(
                callback,
                "✅ Telegram-аккаунт отключён. Активные таймеры остановлены.",
                legacy.main_menu_keyboard(),
            )
            await legacy.safe_answer(callback)
        except Exception:
            logger.exception("Telegram disconnect failed for %s", user_id)
            await legacy.safe_answer(callback, "❌ Не удалось отключить аккаунт", show_alert=True)

    @legacy.app.get("/connect/{token}")
    async def connect_page(token: str):
        challenge = _load_valid_challenge(token)
        if not challenge:
            return _expired_page()
        return _qr_stream(legacy, token, challenge)

    @legacy.app.get("/health/multiuser")
    async def multiuser_health():
        encryption_ready = True
        redis_ready = True
        try:
            token = create_connect_challenge(0)
            delete_connect_challenge(token)
        except SessionEncryptionError:
            encryption_ready = False
        except Exception:
            redis_ready = False
        ready = encryption_ready and redis_ready
        return JSONResponse(content={
            "status": "ok" if ready else "degraded",
            "multiuser": True,
            "api_credentials_configured": bool(os.getenv("API_ID") and os.getenv("API_HASH")),
            "redis_ready": redis_ready,
            "session_encryption_ready": encryption_ready,
            "shared_session_used_for_user_tasks": False,
            "login_method": "telegram_qr_token",
        })


def _load_valid_challenge(token: str) -> Optional[Dict[str, Any]]:
    if not token or len(token) < 20:
        return None
    try:
        return load_connect_challenge(token)
    except Exception:
        logger.exception("Unable to load connect challenge")
        return None


def _telegram_client() -> TelegramClient:
    api_id = (os.getenv("API_ID") or "").strip()
    api_hash = (os.getenv("API_HASH") or "").strip()
    if not api_id or not api_hash:
        raise RuntimeError("API_ID/API_HASH are not configured")
    return TelegramClient(StringSession(), int(api_id), api_hash)


def _qr_stream(legacy, token: str, challenge: Dict[str, Any]):
    async def generate():
        client = _telegram_client()
        try:
            await asyncio.wait_for(client.connect(), timeout=20)
            qr = await asyncio.wait_for(client.qr_login(), timeout=20)
            login_url = html.escape(qr.url, quote=True)

            yield _page_head("Подключение Telegram")
            yield (
                "<div id='status'>"
                "<div class='ok'>Шаг 1 из 2</div>"
                "<p>Нажмите кнопку ниже и подтвердите новую сессию в Telegram.</p>"
                f"<p><a class='button' href='{login_url}'>Открыть Telegram</a></p>"
                "<p class='note'>После подтверждения вернитесь на эту страницу или просто в бота. "
                "Токен действует около 30 секунд.</p>"
                "<p class='note'>Мы не запрашиваем код входа и пароль Telegram.</p>"
                "</div>"
            )
            # Encourage proxies/browsers to flush the first chunk before the
            # request waits for Telegram's authorization update.
            yield "<!--" + ("." * 4096) + "-->"

            try:
                await qr.wait(timeout=QR_WAIT_SECONDS)
            except SessionPasswordNeededError:
                delete_connect_challenge(token)
                yield _replace_status(
                    "<div class='error'>На аккаунте включена двухэтапная аутентификация. "
                    "Эта безопасная версия бота не принимает пароль Telegram, поэтому такое "
                    "подключение пока не завершается удалённо.</div>"
                    "<p>Закройте страницу. Пароль нигде не вводите.</p>"
                )
                yield _page_tail()
                return
            except asyncio.TimeoutError:
                yield _replace_status(
                    "<div class='error'>QR-login token истёк.</div>"
                    "<p>Закройте страницу и нажмите «Подключить Telegram» в боте ещё раз.</p>"
                )
                yield _page_tail()
                return

            me = await asyncio.wait_for(client.get_me(), timeout=15)
            expected_user_id = int(challenge["user_id"])
            actual_user_id = int(getattr(me, "id", 0) or 0)
            if actual_user_id != expected_user_id:
                delete_connect_challenge(token)
                logger.warning(
                    "Rejected Telegram account mismatch bot_user=%s mtproto_user=%s",
                    expected_user_id,
                    actual_user_id,
                )
                yield _replace_status(
                    "<div class='error'>Подтверждён другой Telegram-аккаунт. "
                    "Нужно использовать тот же аккаунт, с которого открыт бот.</div>"
                )
                yield _page_tail()
                return

            save_account(
                expected_user_id,
                client.session.save(),
                telegram_user_id=actual_user_id,
                username=getattr(me, "username", None),
                first_name=getattr(me, "first_name", None),
                phone=getattr(me, "phone", None),
            )
            delete_connect_challenge(token)
            clear_group_cache(expected_user_id)

            yield _replace_status(
                "<div class='ok'>✅ Telegram успешно подключён.</div>"
                "<p>Теперь группы, списки и рассылки будут работать от вашего аккаунта. "
                "Можно закрыть эту страницу.</p>"
            )
            await _notify_success(legacy, expected_user_id)
            yield _page_tail()
        except Exception:
            logger.exception("QR account connection failed")
            yield _replace_status(
                "<div class='error'>Не удалось завершить подключение.</div>"
                "<p>Вернитесь в бота и создайте новую ссылку.</p>"
            )
            yield _page_tail()
        finally:
            try:
                await client.disconnect()
            except Exception:
                logger.exception("Telegram QR client disconnect failed")

    return StreamingResponse(
        generate(),
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; navigate-to tg:; base-uri 'none'; frame-ancestors 'none'",
        },
    )


async def _notify_success(legacy, user_id: int) -> None:
    try:
        bot = legacy.create_bot()
        try:
            await bot.send_message(
                user_id,
                "✅ Telegram-аккаунт подключён. Теперь вы видите свои группы и можете запускать свои рассылки.",
                reply_markup=legacy.main_menu_keyboard(),
            )
        finally:
            await bot.session.close()
    except Exception:
        logger.exception("Unable to notify connected user %s", user_id)


def _replace_status(fragment: str) -> str:
    # The HTML body only contains server-authored strings. No user input is
    # interpolated into JavaScript.
    safe_js = fragment.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    return f"<script>document.getElementById('status').innerHTML=`{safe_js}`;</script>"


def _expired_page():
    return HTMLResponse(
        content=(
            _page_head("Ссылка недействительна")
            + "<div class='error'>Ссылка истекла или уже была использована.</div>"
              "<p>Вернитесь в бота → «👤 Telegram-аккаунт» и создайте новую ссылку.</p>"
            + _page_tail()
        ),
        status_code=410,
        headers={"Cache-Control": "no-store"},
    )


def _page_head(title: str) -> str:
    safe_title = html.escape(title)
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{safe_title}</title><style>
:root{{color-scheme:light dark}}*{{box-sizing:border-box}}body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f3f5f7;color:#17202a}}main{{max-width:520px;margin:8vh auto;padding:28px;background:white;border-radius:18px;box-shadow:0 12px 35px rgba(0,0,0,.09)}}h1{{font-size:24px;margin:0 0 18px}}p{{line-height:1.5}}.button{{display:block;text-align:center;text-decoration:none;margin:18px 0;padding:14px 16px;border-radius:11px;font-size:17px;font-weight:750;background:#2481cc;color:white}}.note{{font-size:13px;color:#69727d}}.error{{background:#ffe8e8;color:#9d1d1d;padding:14px;border-radius:10px}}.ok{{background:#e6f7eb;color:#176b34;padding:14px;border-radius:10px;font-weight:700}}@media(prefers-color-scheme:dark){{body{{background:#121518;color:#eef2f5}}main{{background:#1d2227}}.note{{color:#a9b1b8}}}}
</style></head><body><main><h1>{safe_title}</h1>"""


def _page_tail() -> str:
    return "</main></body></html>"
