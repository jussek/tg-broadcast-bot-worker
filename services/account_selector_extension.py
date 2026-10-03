"""UI for switching between multiple Telegram accounts owned by one bot user."""

from __future__ import annotations

import html

from storage.telegram_accounts import clear_group_cache, list_accounts, set_active_account


def install_account_selector(legacy) -> None:
    if getattr(legacy, "_ACCOUNT_SELECTOR_INSTALLED", False):
        return
    legacy._ACCOUNT_SELECTOR_INSTALLED = True

    previous_main_menu = legacy.main_menu_keyboard

    def main_menu_keyboard():
        kb = previous_main_menu()
        rows = [list(row) for row in kb.inline_keyboard]
        insert_at = max(0, len(rows) - 1)
        rows.insert(
            insert_at,
            [legacy.InlineKeyboardButton(text="🔀 Аккаунты", callback_data="telegram_accounts")],
        )
        return legacy.InlineKeyboardMarkup(inline_keyboard=rows)

    legacy.main_menu_keyboard = main_menu_keyboard

    async def render_accounts(callback) -> None:
        user_id = int(callback.from_user.id)
        accounts = list_accounts(user_id)
        if not accounts:
            await legacy.safe_edit(
                callback,
                "👤 Нет подключённых Telegram-аккаунтов.",
                legacy.InlineKeyboardMarkup(inline_keyboard=[
                    [legacy.InlineKeyboardButton(text="➕ Подключить", callback_data="telegram_account")],
                    [legacy.InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")],
                ]),
            )
            await legacy.safe_answer(callback)
            return

        lines = ["🔀 <b>Telegram-аккаунты</b>", ""]
        rows = []
        for account in accounts:
            account_id = int(account.get("telegram_user_id") or 0)
            username = account.get("username")
            first_name = account.get("first_name") or "Telegram"
            label = f"@{html.escape(username)}" if username else html.escape(first_name)
            marker = "✅ " if account.get("active") else ""
            lines.append(f"{marker}{label} — <code>{account_id}</code>")
            rows.append([
                legacy.InlineKeyboardButton(
                    text=f"{marker}{label}",
                    callback_data=f"telegram_use:{account_id}",
                )
            ])

        rows.append([legacy.InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="telegram_reconnect")])
        rows.append([legacy.InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")])
        await legacy.safe_edit(
            callback,
            "\n".join(lines),
            legacy.InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await legacy.safe_answer(callback)

    @legacy.dp.callback_query(legacy.F.data == "telegram_accounts")
    async def cb_telegram_accounts(callback):
        await render_accounts(callback)

    @legacy.dp.callback_query(legacy.F.data.startswith("telegram_use:"))
    async def cb_telegram_use(callback):
        user_id = int(callback.from_user.id)
        try:
            account_id = int(callback.data.split(":", 1)[1])
            account = set_active_account(user_id, account_id)
            clear_group_cache(user_id)
            username = account.get("username")
            label = f"@{username}" if username else (account.get("first_name") or str(account_id))
            await legacy.safe_answer(callback, f"✅ Активный аккаунт: {label}")
            await render_accounts(callback)
        except Exception:
            await legacy.safe_answer(callback, "❌ Не удалось переключить аккаунт", show_alert=True)
