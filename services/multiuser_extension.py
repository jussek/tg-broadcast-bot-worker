"""Multi-user product layer installed on top of the existing bot application.

The existing index.py remains the stable broadcast UI/worker.  This extension
adds:

* a per-user Telegram account screen;
* a one-time HTTPS account connection flow (phone -> code -> optional 2FA);
* ownership guards so broadcasts/timers cannot be created without the owner's
  personal Telegram session;
* live group discovery through that exact user's Telegram session;
* disconnect handling that cancels the owner's active timers.

It is installed by api/app.py after api.index has registered its existing
handlers/routes. Existing handler functions resolve module globals at runtime,
so replacing a few helpers here upgrades them without duplicating 70KB of bot
code.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import parse_qs

from fastapi.responses import HTMLResponse, JSONResponse
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
)

from services import telegram_delivery
from services.telegram_login import (
    LoginConfigurationError,
    send_login_code,
    submit_2fa_password,
    submit_login_code,
)
from storage.telegram_accounts import (
    SessionEncryptionError,
    allow_send_code,
    clear_group_cache,
    create_connect_challenge,
    delete_account,
    delete_connect_challenge,
    get_public_account,
    is_connected,
    load_connect_challenge,
    save_account,
    save_connect_challenge,
)

logger = logging.getLogger("broadcast.multiuser")


def install_multiuser(legacy) -> None:
    if getattr(legacy, "_MULTIUSER_EXTENSION_INSTALLED", False):
        return
    legacy._MULTIUSER_EXTENSION_INSTALLED = True

    original_main_menu = legacy.main_menu_keyboard
    original_create_immediate = legacy.create_immediate_run
    original_create_timer = legacy.create_timer_task

    def main_menu_keyboard():
        kb = [
            [legacy.InlineKeyboardButton(text="📨 Новая рассылка", callback_data="new_broadcast")],
            [legacy.InlineKeyboardButton(text="📚 Мои шаблоны", callback_data="templates")],
            [legacy.InlineKeyboardButton(text="🔁 Последнее сообщение", callback_data="last_message")],
            [legacy.InlineKeyboardButton(text="📊 Мои таймеры", callback_data="tasks")],
            [legacy.InlineKeyboardButton(text="📋 Мои группы", callback_data="groups")],
            [legacy.InlineKeyboardButton(text="👤 Telegram-аккаунт", callback_data="telegram_account")],
        ]
        return legacy.InlineKeyboardMarkup(inline_keyboard=kb)

    def require_personal_account(user_id: int) -> None:
        try:
            connected = is_connected(user_id)
        except SessionEncryptionError as exc:
            raise legacy.SessionNotAuthorizedError(str(exc)) from exc
        if not connected:
            raise legacy.SessionNotAuthorizedError(
                "Telegram-аккаунт не подключён. Сначала откройте «👤 Telegram-аккаунт» "
                "и подключите свой аккаунт."
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
            except Exception as exc:  # display a useful account-specific error
                logger.warning(
                    "Unable to connect personal Telegram account for user %s: %s",
                    user_id,
                    type(exc).__name__,
                )
                await legacy.safe_answer(
                    callback,
                    "⛔ Telegram-аккаунт не подключён или сессия недействительна. "
                    "Откройте «👤 Telegram-аккаунт».",
                    show_alert=True,
                )
                return

            try:
                groups = await telegram_delivery.fetch_dialogs_with_timeout(client)
            except asyncio.TimeoutError:
                logger.exception("Personal dialog fetch timed out for user %s", user_id)
                await legacy.safe_answer(
                    callback,
                    "⛔ Не удалось получить список групп (таймаут). Попробуйте ещё раз.",
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
                "Нет доступных групп/каналов для отправки. Read-only каналы скрываются автоматически.",
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

    # Existing handlers look these names up in api.index globals when invoked.
    legacy.main_menu_keyboard = main_menu_keyboard
    legacy.create_immediate_run = create_immediate_run
    legacy.create_timer_task = create_timer_task
    legacy.show_groups_picker = show_groups_picker

    async def render_account(callback, *, force_new_link: bool = False) -> None:
        user_id = callback.from_user.id
        account = None
        try:
            account = get_public_account(user_id)
        except SessionEncryptionError:
            logger.exception("Session encryption configuration is invalid")

        if account and not force_new_link:
            username = account.get("username")
            display = f"@{html_lib.escape(username)}" if username else html_lib.escape(
                account.get("first_name") or "Telegram user"
            )
            telegram_id = account.get("telegram_user_id") or "?"
            phone = account.get("phone_masked") or "скрыт"
            text = (
                "👤 <b>Telegram-аккаунт</b>\n\n"
                "✅ Подключён\n"
                f"Аккаунт: {display}\n"
                f"Telegram ID: <code>{telegram_id}</code>\n"
                f"Телефон: {html_lib.escape(str(phone))}\n\n"
                "Рассылки и списки групп работают только через этот аккаунт."
            )
            kb = legacy.InlineKeyboardMarkup(inline_keyboard=[
                [legacy.InlineKeyboardButton(text="🔄 Подключить другой", callback_data="telegram_reconnect")],
                [legacy.InlineKeyboardButton(text="🚫 Отключить аккаунт", callback_data="telegram_disconnect")],
                [legacy.InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")],
            ])
        else:
            try:
                token = create_connect_challenge(user_id)
            except Exception as exc:
                logger.exception("Unable to create Telegram connect challenge")
                await legacy.safe_edit(
                    callback,
                    f"⛔ Не удалось подготовить подключение: {html_lib.escape(str(exc))}",
                    legacy.main_menu_keyboard(),
                )
                await legacy.safe_answer(callback)
                return
            url = f"{legacy.APP_URL}/connect/{token}"
            text = (
                "👤 <b>Telegram-аккаунт</b>\n\n"
                "Для рассылок каждый пользователь подключает <b>свой</b> Telegram.\n"
                "Код входа вводится на защищённой HTTPS-странице, а не отправляется боту.\n\n"
                "Ссылка одноразовая и действует 15 минут."
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
            await render_account(callback, force_new_link=True)
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
        user_id = callback.from_user.id
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

    @legacy.app.get("/connect/{token}", response_class=HTMLResponse)
    async def connect_page(token: str):
        challenge = _safe_challenge(token)
        if not challenge:
            return _expired_page()
        return _connect_response(token, challenge)

    @legacy.app.post("/connect/{token}", response_class=HTMLResponse)
    async def connect_submit(token: str, request):
        challenge = _safe_challenge(token)
        if not challenge:
            return _expired_page()
        try:
            fields = await _read_form(request)
            stage = challenge.get("stage") or "phone"
            user_id = int(challenge["user_id"])

            if stage == "phone":
                phone = (fields.get("phone") or "").strip().replace(" ", "")
                if not phone:
                    return _connect_response(token, challenge, "Введите номер телефона")
                if not phone.startswith("+"):
                    phone = "+" + phone
                if not allow_send_code(user_id):
                    return _connect_response(
                        token,
                        challenge,
                        "Код уже запрашивался. Подождите около минуты и попробуйте снова.",
                    )
                login = await send_login_code(phone)
                challenge.update(login)
                challenge["stage"] = "code"
                save_connect_challenge(token, challenge)
                return _connect_response(token, challenge)

            if stage == "code":
                code = "".join(ch for ch in (fields.get("code") or "") if ch.isdigit())
                if not code:
                    return _connect_response(token, challenge, "Введите код из Telegram")
                result = await submit_login_code(
                    temp_session=challenge["temp_session"],
                    phone=challenge["phone"],
                    phone_code_hash=challenge["phone_code_hash"],
                    code=code,
                )
                if result.status == "password_required":
                    challenge["temp_session"] = result.session_string
                    challenge["stage"] = "password"
                    save_connect_challenge(token, challenge)
                    return _connect_response(token, challenge)
                return await _finish_authorization(legacy, token, challenge, result)

            if stage == "password":
                password = fields.get("password") or ""
                if not password:
                    return _connect_response(token, challenge, "Введите пароль двухэтапной аутентификации")
                result = await submit_2fa_password(
                    temp_session=challenge["temp_session"],
                    password=password,
                )
                return await _finish_authorization(legacy, token, challenge, result)

            return _expired_page()

        except PhoneNumberInvalidError:
            return _connect_response(token, challenge, "Telegram не принимает этот номер телефона")
        except PhoneCodeInvalidError:
            return _connect_response(token, challenge, "Неверный код. Попробуйте ещё раз")
        except PhoneCodeExpiredError:
            challenge["stage"] = "phone"
            for key in ("temp_session", "phone", "phone_code_hash"):
                challenge.pop(key, None)
            save_connect_challenge(token, challenge)
            return _connect_response(token, challenge, "Код истёк. Запросите новый")
        except PasswordHashInvalidError:
            return _connect_response(token, challenge, "Неверный пароль двухэтапной аутентификации")
        except FloodWaitError as exc:
            return _connect_response(
                token,
                challenge,
                f"Telegram временно ограничил запросы. Повторите через {int(exc.seconds)} сек.",
            )
        except (LoginConfigurationError, SessionEncryptionError) as exc:
            logger.exception("Telegram login configuration error")
            return _connect_response(token, challenge, f"Ошибка конфигурации сервера: {exc}")
        except Exception:
            logger.exception("Telegram account connection failed")
            return _connect_response(
                token,
                challenge,
                "Не удалось завершить подключение. Попробуйте ещё раз.",
            )

    @legacy.app.get("/health/multiuser")
    async def multiuser_health():
        encryption_ready = True
        try:
            # Creating a challenge exercises Redis + encryption without exposing
            # any secret/session material. Delete it immediately.
            token = create_connect_challenge(0)
            delete_connect_challenge(token)
        except Exception:
            encryption_ready = False
        return JSONResponse(content={
            "status": "ok" if encryption_ready else "degraded",
            "multiuser": True,
            "api_credentials_configured": bool(os.getenv("API_ID") and os.getenv("API_HASH")),
            "session_encryption_ready": encryption_ready,
            "shared_session_used_for_user_tasks": False,
        })


def _safe_challenge(token: str) -> Optional[Dict[str, Any]]:
    if not token or len(token) < 20:
        return None
    try:
        return load_connect_challenge(token)
    except Exception:
        logger.exception("Unable to load connect challenge")
        return None


async def _read_form(request) -> Dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


async def _finish_authorization(legacy, token: str, challenge: Dict[str, Any], result):
    if result.status != "authorized" or not result.session_string or not result.account:
        raise RuntimeError("Telegram authorization did not return an account")
    user_id = int(challenge["user_id"])
    account = result.account
    save_account(
        user_id,
        result.session_string,
        telegram_user_id=account.get("telegram_user_id"),
        username=account.get("username"),
        first_name=account.get("first_name"),
        phone=account.get("phone"),
    )
    delete_connect_challenge(token)
    clear_group_cache(user_id)

    try:
        bot = legacy.create_bot()
        try:
            await bot.send_message(
                user_id,
                "✅ Telegram-аккаунт подключён. Теперь списки групп и рассылки будут работать от вашего аккаунта.",
                reply_markup=legacy.main_menu_keyboard(),
            )
        finally:
            await bot.session.close()
    except Exception:
        logger.exception("Unable to send account-connected notification to %s", user_id)

    return _html_response(
        _shell(
            "Telegram подключён",
            "<div class='ok'>✅ Аккаунт успешно подключён</div>"
            "<p>Можно закрыть эту страницу и вернуться в бота.</p>",
        )
    )


def _expired_page():
    return _html_response(
        _shell(
            "Ссылка недействительна",
            "<div class='error'>Ссылка истекла или уже была использована.</div>"
            "<p>Вернитесь в бота → «👤 Telegram-аккаунт» и создайте новую ссылку.</p>",
        ),
        status_code=410,
    )


def _connect_response(token: str, challenge: Dict[str, Any], error: Optional[str] = None):
    stage = challenge.get("stage") or "phone"
    err = (
        f"<div class='error'>{html_lib.escape(str(error))}</div>" if error else ""
    )
    action = f"/connect/{html_lib.escape(token, quote=True)}"

    if stage == "phone":
        body = f"""
        {err}
        <p>Введите номер Telegram в международном формате.</p>
        <form method="post" action="{action}">
          <label>Номер телефона</label>
          <input name="phone" type="tel" placeholder="+371..." autocomplete="tel" required>
          <button type="submit">Получить код</button>
        </form>
        <p class="note">Код и пароль вводятся только на этой HTTPS-странице. Бот их не запрашивает сообщением.</p>
        """
    elif stage == "code":
        body = f"""
        {err}
        <p>Telegram отправил код входа на ваш аккаунт. Введите его ниже.</p>
        <form method="post" action="{action}">
          <label>Код Telegram</label>
          <input name="code" inputmode="numeric" autocomplete="one-time-code" required>
          <button type="submit">Подтвердить</button>
        </form>
        <p class="note">Ссылка и временная сессия автоматически истекут.</p>
        """
    elif stage == "password":
        body = f"""
        {err}
        <p>На аккаунте включена двухэтапная аутентификация.</p>
        <form method="post" action="{action}">
          <label>Пароль Telegram</label>
          <input name="password" type="password" autocomplete="current-password" required>
          <button type="submit">Подключить аккаунт</button>
        </form>
        <p class="note">Пароль используется только для завершения авторизации и не сохраняется.</p>
        """
    else:
        return _expired_page()

    return _html_response(_shell("Подключение Telegram", body))


def _html_response(content: str, status_code: int = 200):
    return HTMLResponse(
        content=content,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        },
    )


def _shell(title: str, inner: str) -> str:
    safe_title = html_lib.escape(title)
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title>
<style>
:root{{color-scheme:light dark}}*{{box-sizing:border-box}}body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f3f5f7;color:#17202a}}main{{max-width:520px;margin:8vh auto;padding:28px;background:white;border-radius:18px;box-shadow:0 12px 35px rgba(0,0,0,.09)}}h1{{font-size:24px;margin:0 0 18px}}p{{line-height:1.5}}label{{display:block;font-weight:650;margin:18px 0 7px}}input{{width:100%;font-size:18px;padding:13px 14px;border:1px solid #ccd2d8;border-radius:10px;background:white;color:#17202a}}button{{width:100%;margin-top:18px;padding:13px 16px;border:0;border-radius:10px;font-size:17px;font-weight:700;background:#2481cc;color:white;cursor:pointer}}.note{{font-size:13px;color:#69727d;margin-top:20px}}.error{{background:#ffe8e8;color:#9d1d1d;padding:12px 14px;border-radius:10px;margin-bottom:15px}}.ok{{background:#e6f7eb;color:#176b34;padding:14px;border-radius:10px;font-weight:700}}@media(prefers-color-scheme:dark){{body{{background:#121518;color:#eef2f5}}main{{background:#1d2227}}input{{background:#15191d;color:#eef2f5;border-color:#414950}}.note{{color:#a9b1b8}}}}
</style>
</head>
<body><main><h1>{safe_title}</h1>{inner}</main></body>
</html>"""
