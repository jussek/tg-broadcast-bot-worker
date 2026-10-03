"""Add Telegram cloud-password (2FA) support to the phone-code login flow.

This extension deliberately replaces the legacy POST /connect/{token}/verify-code
route instead of adding a parallel login route. That guarantees that both fresh
and already-open connect pages hit the same 2FA-aware handler.

The 2FA password is accepted only by a same-origin HTTPS POST and is used
immediately with Telethon. It is never written to Redis, task state, logs, or
the encrypted connect challenge. Only the temporary StringSession and a small
attempt counter are persisted while the one-time connect link is valid.
"""

from __future__ import annotations

import asyncio
import html
from typing import Any, Dict

from fastapi import Request
from fastapi.responses import JSONResponse
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from services import multiuser_qr_extension as login_layer
from storage.telegram_accounts import (
    clear_group_cache,
    delete_connect_challenge,
    save_account,
    save_connect_challenge,
)

MAX_2FA_ATTEMPTS = 5
MAX_PASSWORD_LENGTH = 512


def _remove_post_route(app, path: str) -> int:
    """Remove matching POST routes and return how many were removed."""
    before = len(app.router.routes)
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", None) == path
            and "POST" in (getattr(route, "methods", None) or set())
        )
    ]
    return before - len(app.router.routes)


def install_twofa(legacy) -> None:
    """Replace the legacy code verifier with a single native 2FA-aware flow."""
    if getattr(legacy, "_TELEGRAM_2FA_INSTALLED", False):
        return
    legacy._TELEGRAM_2FA_INSTALLED = True

    # The base multi-user extension already registered /verify-code. Remove it
    # before registering the 2FA-aware handler at the exact same URL. This is
    # critical: an old page that is already open in the browser may still POST
    # to /verify-code, and it must never reach the obsolete 409 handler.
    removed = _remove_post_route(legacy.app, "/connect/{token}/verify-code")
    if removed != 1:
        login_layer.logger.warning(
            "Expected one legacy Telegram verify-code route, removed %s", removed
        )

    # Clean up route names from the previous parallel implementation if this
    # module is ever loaded in a warm/reloaded process.
    _remove_post_route(legacy.app, "/connect/{token}/verify-login")
    _remove_post_route(legacy.app, "/connect/{token}/verify-password")

    # connect_page() in the base extension resolves this module global at
    # request time, so replacing it upgrades the existing GET route without
    # registering another GET /connect/{token} endpoint.
    original_page_renderer = login_layer._connect_page_html
    login_layer._connect_page_html = _build_page_renderer(original_page_renderer)

    @legacy.app.post("/connect/{token}/verify-code")
    async def verify_code(token: str, request: Request):
        challenge = login_layer._load_valid_challenge(token)
        if not challenge or challenge.get("stage") != "code":
            return JSONResponse(
                {"ok": False, "error": "Сессия входа истекла. Начните подключение заново."},
                status_code=410,
            )

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Некорректный запрос"}, status_code=400)

        code = login_layer.re.sub(r"\D", "", str(payload.get("code") or ""))
        if not (4 <= len(code) <= 8):
            return JSONResponse({"ok": False, "error": "Введите код из Telegram"}, status_code=400)

        owner_id = int(challenge["user_id"])
        client = login_layer._telegram_client(challenge.get("temp_session"))
        try:
            await asyncio.wait_for(client.connect(), timeout=20)
            try:
                await asyncio.wait_for(
                    client.sign_in(
                        phone=challenge.get("phone"),
                        code=code,
                        phone_code_hash=challenge.get("phone_code_hash"),
                    ),
                    timeout=25,
                )
            except SessionPasswordNeededError:
                challenge["stage"] = "password"
                challenge["temp_session"] = client.session.save()
                challenge["password_attempts"] = 0
                # Important: neither the one-time code nor the 2FA password is saved.
                save_connect_challenge(token, challenge)
                return JSONResponse({"ok": True, "requires_2fa": True, "stage": "password"})

            return await _finalize_login(legacy, token, challenge, client)
        except PhoneCodeInvalidError:
            return JSONResponse({"ok": False, "error": "Неверный код Telegram"}, status_code=400)
        except PhoneCodeExpiredError:
            delete_connect_challenge(token)
            return JSONResponse(
                {"ok": False, "error": "Код истёк. Создайте новую ссылку в боте."},
                status_code=410,
            )
        except FloodWaitError as exc:
            return JSONResponse(
                {"ok": False, "error": f"Telegram просит подождать {int(exc.seconds)} сек."},
                status_code=429,
            )
        except Exception:
            login_layer.logger.exception("Telegram code sign-in failed for owner %s", owner_id)
            return JSONResponse({"ok": False, "error": "Не удалось завершить вход"}, status_code=500)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @legacy.app.post("/connect/{token}/verify-password")
    async def verify_password(token: str, request: Request):
        challenge = login_layer._load_valid_challenge(token)
        if not challenge or challenge.get("stage") != "password":
            return JSONResponse(
                {"ok": False, "error": "Этап 2FA истёк. Начните подключение заново."},
                status_code=410,
            )

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Некорректный запрос"}, status_code=400)

        password = payload.get("password")
        if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH:
            return JSONResponse({"ok": False, "error": "Введите облачный пароль Telegram"}, status_code=400)

        owner_id = int(challenge["user_id"])
        attempts = int(challenge.get("password_attempts") or 0)
        if attempts >= MAX_2FA_ATTEMPTS:
            delete_connect_challenge(token)
            return JSONResponse(
                {"ok": False, "error": "Слишком много попыток. Создайте новую ссылку в боте."},
                status_code=429,
            )

        client = login_layer._telegram_client(challenge.get("temp_session"))
        try:
            await asyncio.wait_for(client.connect(), timeout=20)
            await asyncio.wait_for(client.sign_in(password=password), timeout=25)
            return await _finalize_login(legacy, token, challenge, client)
        except PasswordHashInvalidError:
            attempts += 1
            if attempts >= MAX_2FA_ATTEMPTS:
                delete_connect_challenge(token)
                return JSONResponse(
                    {"ok": False, "error": "Неверный пароль. Лимит попыток исчерпан; создайте новую ссылку."},
                    status_code=429,
                )
            challenge["password_attempts"] = attempts
            # Persist only the counter/session metadata. Never persist password.
            save_connect_challenge(token, challenge)
            remaining = MAX_2FA_ATTEMPTS - attempts
            return JSONResponse(
                {"ok": False, "error": f"Неверный облачный пароль Telegram. Осталось попыток: {remaining}"},
                status_code=401,
            )
        except FloodWaitError as exc:
            return JSONResponse(
                {"ok": False, "error": f"Telegram просит подождать {int(exc.seconds)} сек."},
                status_code=429,
            )
        except Exception:
            # Never include the password or request body in logs.
            login_layer.logger.exception("Telegram 2FA sign-in failed for owner %s", owner_id)
            return JSONResponse({"ok": False, "error": "Не удалось проверить облачный пароль"}, status_code=500)
        finally:
            # Drop the last in-memory reference as soon as the request is done.
            password = None
            try:
                await client.disconnect()
            except Exception:
                pass


async def _finalize_login(legacy, token: str, challenge: Dict[str, Any], client):
    me = await asyncio.wait_for(client.get_me(), timeout=15)
    actual_user_id = int(getattr(me, "id", 0) or 0)
    if not actual_user_id:
        raise RuntimeError("Telegram returned no user")

    owner_id = int(challenge["user_id"])
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
    await login_layer._notify_success(legacy, owner_id, me)
    return JSONResponse(
        {
            "ok": True,
            "connected": True,
            "telegram_user_id": actual_user_id,
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None),
        }
    )


def _build_page_renderer(original_renderer):
    def render(token: str, challenge: Dict[str, Any]) -> str:
        stage = challenge.get("stage") or "phone"
        if stage == "phone":
            return original_renderer(token, challenge)
        return _code_or_password_page(token, challenge)

    return render


def _code_or_password_page(token: str, challenge: Dict[str, Any]) -> str:
    stage = challenge.get("stage") or "code"
    safe_token = html.escape(token, quote=True)

    if stage == "password":
        attempts = int(challenge.get("password_attempts") or 0)
        remaining = max(0, MAX_2FA_ATTEMPTS - attempts)
        body = f"""
<div id='panel'>
  <div class='ok'>Код принят. На аккаунте включена двухэтапная аутентификация.</div>
  <label>Облачный пароль Telegram</label>
  <input id='password' type='password' autocomplete='current-password' maxlength='{MAX_PASSWORD_LENGTH}' placeholder='Введите пароль'>
  <button onclick='verifyPassword()'>Подключить аккаунт</button>
  <p class='note'>Пароль используется только для текущего входа и не сохраняется. Осталось попыток: {remaining}.</p>
</div>
"""
    else:
        phone = html.escape(login_layer._mask_phone_for_page(str(challenge.get("phone") or "")))
        body = f"""
<div id='panel'>
  <div class='ok'>Код отправлен на {phone}</div>
  <label>Код из Telegram</label>
  <input id='code' inputmode='numeric' autocomplete='one-time-code' placeholder='12345' maxlength='8'>
  <button onclick='verifyCode()'>Подключить аккаунт</button>
  <p class='note'>Если на аккаунте включена 2FA, после кода появится отдельное поле облачного пароля.</p>
</div>
"""

    script = f"""
<script>
const token = '{safe_token}';
function setBusy(v) {{ document.querySelectorAll('button').forEach(b => b.disabled = v); }}
function showError(msg) {{
  const e=document.getElementById('error'); e.textContent=msg||''; e.style.display=msg?'block':'none';
}}
async function post(path, data) {{
  setBusy(true); showError('');
  try {{
    const r=await fetch('/connect/'+token+'/'+path, {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(data),
      cache:'no-store',
      credentials:'same-origin'
    }});
    const j=await r.json();
    if(!j.ok) throw new Error(j.error||'Ошибка');
    return j;
  }} finally {{ setBusy(false); }}
}}
function finish(j) {{
  const who=j.username ? '@'+j.username : (j.first_name||('ID '+j.telegram_user_id));
  document.getElementById('panel').innerHTML='<div class="ok">✅ '+who+' подключён</div><p>Можно закрыть страницу и вернуться в бота.</p>';
}}
async function verifyCode() {{
  try {{
    const j=await post('verify-code', {{code:document.getElementById('code').value}});
    if(j.requires_2fa) {{ location.reload(); return; }}
    finish(j);
  }} catch(e) {{ showError(e.message); }}
}}
async function verifyPassword() {{
  const field=document.getElementById('password');
  try {{
    const j=await post('verify-password', {{password:field.value}});
    field.value='';
    finish(j);
  }} catch(e) {{ field.value=''; showError(e.message); }}
}}
</script>
"""

    return (
        login_layer._page_head("Подключение Telegram")
        + "<div id='error' class='error' style='display:none'></div>"
        + body
        + script
        + login_layer._page_tail()
    )
