"""Multi-user Telegram account layer using phone + one-time Telegram code.

The connection link is the ownership capability. The user enters a phone number,
Telegram sends a one-time login code, and the second request completes sign-in.
The OTP itself is never persisted. Only the temporary StringSession and
phone_code_hash are stored inside the already-encrypted short-lived challenge.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from services import telegram_delivery
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
    """Install multi-user UI/routes and ownership guards on api.index."""
    if getattr(legacy, "_MULTIUSER_PHONE_CODE_INSTALLED", False):
        return
    legacy._MULTIUSER_PHONE_CODE_INSTALLED = True

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
                "и выполните вход по номеру и коду Telegram."
            )

    def create_immediate_run(*, user_id: int, message: str, groups):
        require_personal_account(user_id)
        return original_create_immediate(user_id=user_id, message=message, groups=groups)

    def create_timer_task(user_id: int, message: str, groups, interval_minutes: int, total_repeats):
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
                await legacy.safe_answer(callback, "⛔ Таймаут при получении групп Telegram.", show_alert=True)
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

    legacy.main_menu_keyboard = main_menu_keyboard
    legacy.create_immediate_run = create_immediate_run
    legacy.create_timer_task = create_timer_task
    legacy.show_groups_picker = show_groups_picker

    async def render_account(callback, *, reconnect: bool = False) -> None:
        user_id = int(callback.from_user.id)
        account = get_public_account(user_id)
        if account and not reconnect:
            username = account.get("username")
            label = f"@{html.escape(username)}" if username else html.escape(account.get("first_name") or "Telegram user")
            tg_id = account.get("telegram_user_id") or "?"
            phone = html.escape(str(account.get("phone_masked") or "скрыт"))
            text = (
                "👤 <b>Telegram-аккаунт</b>\n\n"
                "✅ Подключён\n"
                f"Аккаунт: {label}\n"
                f"Telegram ID: <code>{tg_id}</code>\n"
                f"Телефон: {phone}\n\n"
                "Рассылки выполняются от этого аккаунта."
            )
            kb = legacy.InlineKeyboardMarkup(inline_keyboard=[
                [legacy.InlineKeyboardButton(text="🔄 Подключить другой", callback_data="telegram_reconnect")],
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
                "Нажмите кнопку ниже и войдите по номеру телефона и одноразовому коду Telegram.\n\n"
                "Код используется только для текущей попытки входа и не сохраняется. "
                "Ссылка действует 15 минут."
            )
            kb = legacy.InlineKeyboardMarkup(inline_keyboard=[
                [legacy.InlineKeyboardButton(text="🔐 Войти по коду", url=url)],
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
        return HTMLResponse(_connect_page_html(token, challenge), headers=_no_store_headers())

    @legacy.app.post("/connect/{token}/send-code")
    async def send_code(token: str, request: Request):
        challenge = _load_valid_challenge(token)
        if not challenge:
            return JSONResponse({"ok": False, "error": "Ссылка истекла"}, status_code=410)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Некорректный запрос"}, status_code=400)

        phone = _normalize_phone(str(payload.get("phone") or ""))
        if not phone:
            return JSONResponse({"ok": False, "error": "Введите номер в международном формате, например +371..."}, status_code=400)
        owner_id = int(challenge["user_id"])
        if not allow_send_code(owner_id):
            return JSONResponse({"ok": False, "error": "Подождите около 45 секунд перед повторной отправкой кода"}, status_code=429)

        client = _telegram_client()
        try:
            await asyncio.wait_for(client.connect(), timeout=20)
            sent = await asyncio.wait_for(client.send_code_request(phone), timeout=25)
            challenge.update({
                "stage": "code",
                "phone": phone,
                "phone_code_hash": sent.phone_code_hash,
                "temp_session": client.session.save(),
            })
            save_connect_challenge(token, challenge)
            return JSONResponse({"ok": True, "stage": "code", "phone": _mask_phone_for_page(phone)})
        except PhoneNumberInvalidError:
            return JSONResponse({"ok": False, "error": "Telegram не принимает этот номер телефона"}, status_code=400)
        except FloodWaitError as exc:
            return JSONResponse({"ok": False, "error": f"Telegram просит подождать {int(exc.seconds)} сек."}, status_code=429)
        except Exception:
            logger.exception("Failed to send Telegram login code for owner %s", owner_id)
            return JSONResponse({"ok": False, "error": "Не удалось отправить код Telegram"}, status_code=500)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @legacy.app.post("/connect/{token}/verify-code")
    async def verify_code(token: str, request: Request):
        challenge = _load_valid_challenge(token)
        if not challenge or challenge.get("stage") != "code":
            return JSONResponse({"ok": False, "error": "Сессия входа истекла. Начните подключение заново."}, status_code=410)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Некорректный запрос"}, status_code=400)

        code = re.sub(r"\D", "", str(payload.get("code") or ""))
        if not (4 <= len(code) <= 8):
            return JSONResponse({"ok": False, "error": "Введите код из Telegram"}, status_code=400)

        owner_id = int(challenge["user_id"])
        client = _telegram_client(challenge.get("temp_session"))
        try:
            await asyncio.wait_for(client.connect(), timeout=20)
            await asyncio.wait_for(
                client.sign_in(
                    phone=challenge.get("phone"),
                    code=code,
                    phone_code_hash=challenge.get("phone_code_hash"),
                ),
                timeout=25,
            )
            me = await asyncio.wait_for(client.get_me(), timeout=15)
            actual_user_id = int(getattr(me, "id", 0) or 0)
            if not actual_user_id:
                raise RuntimeError("Telegram returned no user")

            save_account(
                owner_id,
                client.session.save(),
                telegram_user_id=actual_user_id,
                username=getattr(me, "username", None),
                first_name=getattr(me, "first_name", None),
                phone=getattr(me, "phone", None) or challenge.get("phone"),
            )
            delete_connect_challenge(token)
            clear_group_cache(owner_id)
            await _notify_success(legacy, owner_id, me)
            return JSONResponse({
                "ok": True,
                "connected": True,
                "telegram_user_id": actual_user_id,
                "username": getattr(me, "username", None),
                "first_name": getattr(me, "first_name", None),
            })
        except PhoneCodeInvalidError:
            return JSONResponse({"ok": False, "error": "Неверный код Telegram"}, status_code=400)
        except PhoneCodeExpiredError:
            delete_connect_challenge(token)
            return JSONResponse({"ok": False, "error": "Код истёк. Создайте новую ссылку в боте."}, status_code=410)
        except SessionPasswordNeededError:
            return JSONResponse({
                "ok": False,
                "error": "На аккаунте включён облачный пароль Telegram (2FA). Вход по 2FA пока не включён."
            }, status_code=409)
        except FloodWaitError as exc:
            return JSONResponse({"ok": False, "error": f"Telegram просит подождать {int(exc.seconds)} сек."}, status_code=429)
        except Exception:
            logger.exception("Telegram code sign-in failed for owner %s", owner_id)
            return JSONResponse({"ok": False, "error": "Не удалось завершить вход"}, status_code=500)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @legacy.app.get("/health/multiuser")
    async def multiuser_health():
        encryption_ready = True
        redis_ready = True
        try:
            probe = create_connect_challenge(0)
            delete_connect_challenge(probe)
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
            "login_method": "telegram_phone_code",
        })


def _load_valid_challenge(token: str) -> Optional[Dict[str, Any]]:
    if not token or len(token) < 20:
        return None
    try:
        return load_connect_challenge(token)
    except Exception:
        logger.exception("Unable to load connect challenge")
        return None


def _telegram_client(session_string: Optional[str] = None) -> TelegramClient:
    api_id = (os.getenv("API_ID") or "").strip()
    api_hash = (os.getenv("API_HASH") or "").strip()
    if not api_id or not api_hash:
        raise RuntimeError("API_ID/API_HASH are not configured")
    return TelegramClient(StringSession(session_string or ""), int(api_id), api_hash)


def _normalize_phone(value: str) -> Optional[str]:
    raw = value.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if not raw.startswith("+"):
        raw = "+" + raw
    digits = raw[1:]
    if not digits.isdigit() or not (7 <= len(digits) <= 15):
        return None
    return "+" + digits


def _mask_phone_for_page(phone: str) -> str:
    if len(phone) <= 6:
        return "••••"
    return phone[:4] + "•" * max(4, len(phone) - 7) + phone[-3:]


async def _notify_success(legacy, user_id: int, me) -> None:
    try:
        bot = legacy.create_bot()
        try:
            username = getattr(me, "username", None)
            name = f"@{username}" if username else (getattr(me, "first_name", None) or str(getattr(me, "id", "")))
            await bot.send_message(
                user_id,
                f"✅ Telegram-аккаунт {html.escape(name)} подключён. Теперь можно выбирать его группы и запускать рассылки.",
                reply_markup=legacy.main_menu_keyboard(),
            )
        finally:
            await bot.session.close()
    except Exception:
        logger.exception("Unable to notify connected user %s", user_id)


def _connect_page_html(token: str, challenge: Dict[str, Any]) -> str:
    stage = challenge.get("stage") or "phone"
    safe_token = html.escape(token, quote=True)
    if stage == "code":
        phone = html.escape(_mask_phone_for_page(str(challenge.get("phone") or "")))
        body = f"""
<div id='panel'>
  <div class='ok'>Код отправлен на {phone}</div>
  <label>Код из Telegram</label>
  <input id='code' inputmode='numeric' autocomplete='one-time-code' placeholder='12345' maxlength='8'>
  <button onclick='verifyCode()'>Подключить аккаунт</button>
  <p class='note'>Код не сохраняется. Если код истёк, вернитесь в бот и создайте новую ссылку.</p>
</div>
"""
    else:
        body = """
<div id='panel'>
  <label>Номер Telegram</label>
  <input id='phone' type='tel' autocomplete='tel' placeholder='+371...' maxlength='20'>
  <button onclick='sendCode()'>Получить код</button>
  <p class='note'>Telegram пришлёт одноразовый код в приложение или другим доступным способом.</p>
</div>
"""

    script = f"""
<script>
const token = '{safe_token}';
function setBusy(v) {{ document.querySelectorAll('button').forEach(b => b.disabled = v); }}
function showError(msg) {{
  let e=document.getElementById('error'); e.textContent=msg||''; e.style.display=msg?'block':'none';
}}
async function post(path, data) {{
  setBusy(true); showError('');
  try {{
    const r=await fetch('/connect/'+token+'/'+path, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(data), cache:'no-store'}});
    const j=await r.json();
    if(!j.ok) throw new Error(j.error||'Ошибка');
    return j;
  }} finally {{ setBusy(false); }}
}}
async function sendCode() {{
  try {{ await post('send-code', {{phone:document.getElementById('phone').value}}); location.reload(); }}
  catch(e) {{ showError(e.message); }}
}}
async function verifyCode() {{
  try {{
    const j=await post('verify-code', {{code:document.getElementById('code').value}});
    const who=j.username ? '@'+j.username : (j.first_name||('ID '+j.telegram_user_id));
    document.getElementById('panel').innerHTML='<div class="ok">✅ '+who+' подключён</div><p>Можно закрыть страницу и вернуться в бота.</p>';
  }} catch(e) {{ showError(e.message); }}
}}
</script>
"""
    return _page_head("Подключение Telegram") + "<div id='error' class='error' style='display:none'></div>" + body + script + _page_tail()


def _expired_page():
    return HTMLResponse(
        _page_head("Ссылка недействительна")
        + "<div class='error'>Ссылка истекла или уже была использована.</div>"
        + "<p>Вернитесь в бота → «👤 Telegram-аккаунт» и создайте новую ссылку.</p>"
        + _page_tail(),
        status_code=410,
        headers=_no_store_headers(),
    )


def _no_store_headers() -> Dict[str, str]:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
    }


def _page_head(title: str) -> str:
    safe_title = html.escape(title)
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{safe_title}</title><style>
:root{{color-scheme:light dark}}*{{box-sizing:border-box}}body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f3f5f7;color:#17202a}}main{{max-width:520px;margin:8vh auto;padding:28px;background:white;border-radius:18px;box-shadow:0 12px 35px rgba(0,0,0,.09)}}h1{{font-size:24px;margin:0 0 18px}}p{{line-height:1.5}}label{{display:block;font-weight:700;margin:16px 0 7px}}input{{width:100%;font-size:18px;padding:13px 14px;border:1px solid #b8c0c8;border-radius:11px;background:transparent;color:inherit}}button{{width:100%;border:0;margin:18px 0;padding:14px 16px;border-radius:11px;font-size:17px;font-weight:750;background:#2481cc;color:white;cursor:pointer}}button:disabled{{opacity:.55}}.note{{font-size:13px;color:#69727d}}.error{{background:#ffe8e8;color:#9d1d1d;padding:14px;border-radius:10px;margin-bottom:14px}}.ok{{background:#e6f7eb;color:#176b34;padding:14px;border-radius:10px;font-weight:700}}@media(prefers-color-scheme:dark){{body{{background:#121518;color:#eef2f5}}main{{background:#1d2227}}.note{{color:#a9b1b8}}}}
</style></head><body><main><h1>{safe_title}</h1>"""


def _page_tail() -> str:
    return "</main></body></html>"
