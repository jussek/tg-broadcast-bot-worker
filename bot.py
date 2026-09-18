import asyncio
import html
import os

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telethon import TelegramClient
from telethon.sessions import StringSession

from redis_storage import (
    save_last_message,
    get_last_message,
    create_task,
    get_user_tasks,
    get_task,
    save_task,
    create_template,
    get_template,
    get_user_templates,
    update_template,
    set_template_groups,
    set_template_group_ids,
    delete_template,
    create_chat_group,
    get_chat_group,
    get_user_chat_groups,
    update_chat_group,
    set_chat_group_chats,
    delete_chat_group,
)


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")

if not SESSION_STRING:
    raise RuntimeError(
        "TELEGRAM_SESSION_STRING не найден в .env"
    )


# ============================================================
# TELETHON USER ACCOUNT
# ============================================================

user_client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
)


# ============================================================
# AIROGRAM BOT
# ============================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# TEMPORARY USER STATES
# ============================================================

user_states = {}


# ============================================================
# MAIN MENU
# ============================================================

def main_keyboard():

    builder = InlineKeyboardBuilder()

    builder.button(text="📨 Новая рассылка", callback_data="new_message")
    builder.button(text="📚 Мои шаблоны", callback_data="templates")
    builder.button(text="🔁 Повторить последнее", callback_data="repeat_last")
    builder.button(text="⏰ Таймеры", callback_data="active_tasks")
    builder.button(text="📋 Мои группы", callback_data="groups")
    builder.button(text="⚙️ Настройки", callback_data="settings")

    builder.adjust(2, 2, 2)
    return builder.as_markup()


# START
# ============================================================

@dp.message(CommandStart())
async def start(message: Message):

    user_states.pop(
        message.from_user.id,
        None
    )

    await message.answer(
        "🤖 <b>Панель управления</b>\n\n"
        "Выберите необходимое действие:",
        reply_markup=main_keyboard()
    )


# ============================================================
@dp.message(Command("s"))
async def show_menu_command(message: Message):
    user_states.pop(message.from_user.id, None)
    await message.answer(
        "🤖 <b>Панель управления</b>\n\nВыберите необходимое действие:",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )


# NEW MESSAGE
# ============================================================

@dp.callback_query(F.data == "new_message")
async def new_message(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Написать сообщение", callback_data="write_message")
    builder.button(text="📚 Выбрать шаблон", callback_data="templates_for_broadcast")
    builder.button(text="🔁 Повторить последнее", callback_data="repeat_last")
    builder.button(text="❌ Отмена", callback_data="cancel")
    builder.adjust(1)
    await callback.message.edit_text(
        "📨 <b>Новая рассылка</b>\n\nКак создать сообщение?",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "write_message")
async def write_message(callback: CallbackQuery):
    user_states[callback.from_user.id] = {"state": "waiting_message"}
    await callback.message.edit_text(
        "📝 <b>Введите сообщение</b>\n\nОтправьте текст, который будет использоваться для рассылки.",
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================================
# TEMPLATES
# ============================================================

@dp.callback_query(F.data == "templates")
async def templates_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    items = get_user_templates(user_id)
    builder = InlineKeyboardBuilder()
    for template in items:
        name = template["name"]
        if len(name) > 35:
            name = name[:32] + "..."
        builder.button(text=f"📄 {name}", callback_data=f"template:{template['id']}")
    builder.button(text="➕ Создать шаблон", callback_data="create_template")
    builder.button(text="◀️ Назад", callback_data="back_menu")
    builder.adjust(1)
    text = "📚 <b>Мои шаблоны</b>\n\n" + (
        f"Всего: <b>{len(items)}</b>\n\nВыберите шаблон:"
        if items else
        "Шаблонов пока нет.\n\nСоздайте первый шаблон."
    )
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "templates_for_broadcast")
async def templates_for_broadcast(callback: CallbackQuery):
    items = get_user_templates(callback.from_user.id)
    if not items:
        await callback.answer("Сначала создай хотя бы один шаблон.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    for template in items:
        name = template["name"]
        if len(name) > 35:
            name = name[:32] + "..."
        builder.button(text=f"📄 {name}", callback_data=f"use_template:{template['id']}")
    builder.button(text="➕ Создать шаблон", callback_data="create_template")
    builder.button(text="◀️ Назад", callback_data="new_message")
    builder.adjust(1)
    await callback.message.edit_text(
        "📚 <b>Выбор шаблона</b>\n\nВыберите текст для новой рассылки:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "create_template")
async def create_template_start(callback: CallbackQuery):
    user_states[callback.from_user.id] = {"state": "template_name"}
    await callback.message.edit_text(
        "➕ <b>Новый шаблон</b>\n\n"
        "Введите название шаблона.\n\n"
        "Например: <code>Акция</code>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("template:"))
async def open_template(callback: CallbackQuery):
    user_id = callback.from_user.id
    template = get_template(callback.data.split(":", 1)[1])
    if not template or int(template.get("user_id", -1)) != int(user_id):
        await callback.answer("Шаблон не найден или нет доступа.", show_alert=True)
        return
    tid = template["id"]
    builder = InlineKeyboardBuilder()
    builder.button(text="📨 Использовать", callback_data=f"use_template:{tid}")
    builder.button(text="✏️ Изменить", callback_data=f"edit_template:{tid}")
    builder.button(text="📂 Изменить группы", callback_data=f"template_groups:{tid}")
    builder.button(text="🗑 Удалить", callback_data=f"delete_template:{tid}")
    builder.button(text="◀️ Назад", callback_data="templates")
    builder.adjust(1)
    saved_group_ids = template.get("group_ids", [])
    legacy_groups = template.get("groups", [])
    groups_info = (
        f"📂 <b>Групп рассылки:</b> {len(saved_group_ids)}\n\n"
        if saved_group_ids else
        (f"📋 <b>Старых чатов:</b> {len(legacy_groups)}\n\n" if legacy_groups else "📂 <b>Группы рассылки:</b> не сохранены\n\n")
    )
    await callback.message.edit_text(
        f"📄 <b>{html.escape(template['name'])}</b>\n\n"
        f"{html.escape(template['message'])}\n\n"
        f"{groups_info}",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("use_template:"))
async def use_template(callback: CallbackQuery):
    user_id = callback.from_user.id
    template = get_template(callback.data.split(":", 1)[1])
    if not template or int(template.get("user_id", -1)) != int(user_id):
        await callback.answer("Шаблон не найден или нет доступа.", show_alert=True)
        return
    user_states[user_id] = {
        "state": "select_groups",
        "message": template["message"],
        "template_id": template["id"],
        "template_group_ids": {str(x) for x in template.get("group_ids", [])},
        # Совместимость со старыми шаблонами, где сохранялись ID чатов.
        "legacy_template_chat_ids": {int(x) for x in template.get("groups", [])},
    }
    await load_groups(callback, user_id)
    await callback.answer("Шаблон выбран.")


@dp.callback_query(F.data.startswith("edit_template:"))
async def edit_template_start(callback: CallbackQuery):
    user_id = callback.from_user.id
    template = get_template(callback.data.split(":", 1)[1])
    if not template or int(template.get("user_id", -1)) != int(user_id):
        await callback.answer("Шаблон не найден или нет доступа.", show_alert=True)
        return
    user_states[user_id] = {
        "state": "template_edit_name",
        "template_id": template["id"]
    }
    await callback.message.edit_text(
        "✏️ <b>Изменение шаблона</b>\n\nВведите новое название шаблона.",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("template_groups:"))
async def template_groups_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    template_id = callback.data.split(":", 1)[1]
    template = get_template(template_id)

    if not template or int(template.get("user_id", -1)) != int(user_id):
        await callback.answer("Шаблон не найден или нет доступа.", show_alert=True)
        return

    user_states[user_id] = {
        "state": "template_change_groups",
        "template_id": template_id,
        "message": template["message"],
        "template_group_ids": {str(x) for x in template.get("group_ids", [])}
    }

    await load_groups(callback, user_id)
    await callback.answer("Выберите группы для шаблона.")


@dp.callback_query(F.data.startswith("delete_template:"))
async def delete_template_handler(callback: CallbackQuery):
    if not delete_template(callback.from_user.id, callback.data.split(":", 1)[1]):
        await callback.answer("Не удалось удалить шаблон.", show_alert=True)
        return
    await callback.message.edit_text(
        "🗑 <b>Шаблон удалён.</b>",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer("Удалено")


# REPEAT LAST MESSAGE
# ============================================================

@dp.callback_query(F.data == "repeat_last")
async def repeat_last(callback: CallbackQuery):

    user_id = callback.from_user.id

    text = get_last_message(user_id)

    if not text:

        await callback.answer(
            "⚠️ Последнее сообщение не найдено.",
            show_alert=True
        )

        return

    user_states[user_id] = {
        "state": "select_groups",
        "message": text
    }

    await load_groups(
        callback,
        user_id
    )

    await callback.answer()


# ============================================================
# TIMER FROM LAST MESSAGE
# ============================================================

@dp.callback_query(F.data == "timer_from_last")
async def timer_from_last(callback: CallbackQuery):

    user_id = callback.from_user.id

    text = get_last_message(user_id)

    if not text:

        await callback.answer(
            "⚠️ Сначала создайте новое сообщение.",
            show_alert=True
        )

        return

    user_states[user_id] = {
        "state": "select_groups",
        "message": text
    }

    await load_groups(
        callback,
        user_id
    )

    await callback.answer()


# ============================================================
# COMMON MESSAGE HANDLER
# ============================================================

@dp.message()
async def handle_user_message(message: Message):

    user_id = message.from_user.id

    state = user_states.get(user_id)

    if not state:
        return

    current_state = state.get("state")


    # ========================================================
    # NEW MESSAGE
    # ========================================================

    if current_state == "waiting_message":

        if not message.text:

            await message.answer(
                "❌ Поддерживается только текстовый формат."
            )

            return

        text = message.text.strip()

        if not text:

            await message.answer(
                "❌ Сообщение не может быть пустым."
            )

            return

        save_last_message(
            user_id,
            text
        )

        state["message"] = text
        state["state"] = "select_groups"

        await load_groups(
            message,
            user_id
        )

        return


    # ========================================================
    # CUSTOM CHAT GROUP CREATION / RENAME
    # ========================================================

    if current_state == "chat_group_name":
        name = message.text.strip() if message.text else ""
        if not name:
            await message.answer("❌ Название не может быть пустым.")
            return
        if len(name) > 60:
            await message.answer("❌ Максимум 60 символов.")
            return
        state["chat_group_name"] = name
        state["state"] = "chat_group_create_chats"
        state["chat_picker_selected_ids"] = set()
        await load_chat_picker(message, user_id)
        return

    if current_state == "chat_group_rename":
        name = message.text.strip() if message.text else ""
        if not name:
            await message.answer("❌ Название не может быть пустым.")
            return
        item = get_chat_group(state["chat_group_id"])
        if not item or int(item.get("user_id", -1)) != int(user_id):
            user_states.pop(user_id, None)
            await message.answer("❌ Группа не найдена.", reply_markup=main_keyboard())
            return
        updated = update_chat_group(user_id, state["chat_group_id"], name, item.get("chat_ids", []))
        user_states.pop(user_id, None)
        await message.answer(
            f"✅ Группа переименована в <b>{html.escape(updated['name'])}</b>.",
            reply_markup=main_keyboard(), parse_mode="HTML"
        )
        return

    # ========================================================
    if current_state == "template_name":
        name = message.text.strip() if message.text else ""
        if not name:
            await message.answer("❌ Название шаблона не может быть пустым.")
            return
        if len(name) > 80:
            await message.answer("❌ Название слишком длинное. Максимум 80 символов.")
            return
        state["template_name"] = name
        state["state"] = "template_message"
        await message.answer(
            "📝 <b>Текст шаблона</b>\n\nОтправьте текст, который нужно сохранить.",
            parse_mode="HTML"
        )
        return

    if current_state == "template_message":
        text = message.text.strip() if message.text else ""
        if not text:
            await message.answer("❌ Текст шаблона не может быть пустым.")
            return

        # Шаблон создаём только после выбора групп, чтобы сохранить
        # именно те группы, которые пользователь выберет дальше.
        state["message"] = text
        state["state"] = "template_select_groups"

        await load_groups(message, user_id)
        return

    if current_state == "template_edit_name":
        name = message.text.strip() if message.text else ""
        if not name:
            await message.answer("❌ Название не может быть пустым.")
            return
        if len(name) > 80:
            await message.answer("❌ Название слишком длинное. Максимум 80 символов.")
            return
        state["template_name"] = name
        state["state"] = "template_edit_message"
        await message.answer(
            "📝 <b>Новый текст шаблона</b>\n\nОтправьте новый текст.",
            parse_mode="HTML"
        )
        return

    if current_state == "template_edit_message":
        text = message.text.strip() if message.text else ""
        if not text:
            await message.answer("❌ Текст шаблона не может быть пустым.")
            return
        template = update_template(
            user_id,
            state["template_id"],
            state["template_name"],
            text
        )
        user_states.pop(user_id, None)
        if not template:
            await message.answer("❌ Шаблон не найден.", reply_markup=main_keyboard())
            return
        await message.answer(
            f"✅ <b>Шаблон изменён!</b>\n\n📄 <b>{html.escape(template['name'])}</b>\n\n{html.escape(template['message'])}",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
        return


    # ========================================================
    # SAVE TEMPLATE AFTER GROUP SELECTION
    # ========================================================

    if current_state == "template_select_groups":

        # Эта ветка фактически используется через callback continue_groups,
        # но оставляем состояние явно обозначенным для защиты от случайного текста.
        await message.answer(
            "📋 Сначала выберите группы кнопками выше, затем нажмите «Продолжить»."
        )
        return


    # TIMER INTERVAL
    # ========================================================

    if current_state == "waiting_timer":

        if not message.text:

            await message.answer(
                "❌ Введите количество минут."
            )

            return

        try:

            minutes = int(
                message.text.strip()
            )

            if minutes <= 0:
                raise ValueError

        except ValueError:

            await message.answer(
                "❌ Введите положительное целое число.\n\n"
                "Пример: <code>20</code>",
                parse_mode="HTML"
            )

            return

        state["interval"] = minutes
        state["state"] = "waiting_repeats"

        await message.answer(
            "🔁 <b>Количество повторов</b>\n\n"
            "Укажите, сколько раз отправить сообщение:\n\n"
            "Пример: <code>5</code>",
            parse_mode="HTML"
        )

        return


    # ========================================================
    # REPEATS
    # ========================================================

    if current_state == "waiting_repeats":

        if not message.text:

            await message.answer(
                "❌ Введите количество повторов."
            )

            return

        try:

            repeats = int(
                message.text.strip()
            )

            if repeats <= 0:
                raise ValueError

        except ValueError:

            await message.answer(
                "❌ Введите положительное целое число.\n\n"
                "Пример:\n"
                "<code>5</code>"
            )

            return

        state["repeats"] = repeats
        state["state"] = "timer_confirmation"

        interval = state["interval"]

        builder = InlineKeyboardBuilder()

        builder.button(
            text="🚀 Запустить таймер",
            callback_data="start_timer"
        )

        builder.button(
            text="❌ Отмена",
            callback_data="cancel"
        )

        builder.adjust(1)

        await message.answer(
            "⏰ <b>Подтверждение настроек</b>\n\n"
            f"⏱ Интервал: <b>{interval} мин.</b>\n"
            f"🔁 Повторов: <b>{repeats}</b>\n\n"
            f"Первая отправка произойдёт через "
            f"<b>{interval} минут</b>.",
            reply_markup=builder.as_markup()
        )

        return


# ============================================================
# TELEGRAM CHATS / CUSTOM BROADCAST GROUPS
# ============================================================

async def load_chat_dialogs():
    if not user_client.is_connected():
        await user_client.connect()

    result = []
    async for dialog in user_client.iter_dialogs():
        if dialog.is_group:
            result.append(dialog)
    return result


async def load_groups(message_or_callback, user_id):
    """Показывает именно пользовательские группы рассылки, а не каждый чат."""
    groups = get_user_chat_groups(user_id)

    if not groups:
        text = (
            "📂 <b>Группы рассылки ещё не созданы.</b>\n\n"
            "Создай группу один раз, добавь в неё нужные чаты — "
            "после этого для рассылки достаточно выбрать одну кнопку группы."
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Создать группу", callback_data="create_chat_group")
        builder.button(text="❌ Отмена", callback_data="cancel")
        builder.adjust(1)
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        else:
            await message_or_callback.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        return

    state = user_states[user_id]
    state["broadcast_groups"] = groups

    saved = state.get("template_group_ids")
    if saved:
        state["selected"] = {
            i for i, group in enumerate(groups) if str(group["id"]) in saved
        }
    elif state.get("legacy_template_chat_ids"):
        # Старые шаблоны хранили ID отдельных чатов.
        # Автоматически отмечаем группы, в которых есть такие чаты.
        legacy_ids = state["legacy_template_chat_ids"]
        state["selected"] = {
            i for i, group in enumerate(groups)
            if legacy_ids.intersection(int(x) for x in group.get("chat_ids", []))
        }
    else:
        state["selected"] = set()

    await show_groups(message_or_callback, user_id)


async def show_groups(message_or_callback, user_id):
    state = user_states.get(user_id)
    if not state:
        return

    groups = state.get("broadcast_groups", [])
    selected = state.setdefault("selected", set())
    builder = InlineKeyboardBuilder()

    for index, group in enumerate(groups):
        prefix = "☑️" if index in selected else "☐"
        name = group["name"]
        if len(name) > 38:
            name = name[:35] + "..."
        builder.button(
            text=f"{prefix} 📂 {name} ({len(group.get('chat_ids', []))})",
            callback_data=f"broadcast_group_toggle:{index}"
        )

    builder.button(
        text="☑️ Снять выбор со всех" if len(selected) == len(groups) else "☑️ Выбрать все группы",
        callback_data="broadcast_groups_all"
    )
    builder.button(text="➕ Создать группу", callback_data="create_chat_group")
    builder.button(text="📋 Управление группами", callback_data="groups")
    builder.button(text="✅ Продолжить", callback_data="continue_groups")
    builder.button(text="❌ Отмена", callback_data="cancel")
    builder.adjust(1)

    text = (
        "📂 <b>Выбор групп рассылки</b>\n\n"
        f"Выбрано групп: <b>{len(selected)}</b> из <b>{len(groups)}</b>\n\n"
        "Теперь не нужно выбирать каждый чат отдельно — "
        "выбирай готовую группу."
    )
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message_or_callback.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("broadcast_group_toggle:"))
async def toggle_broadcast_group(callback: CallbackQuery):
    state = user_states.get(callback.from_user.id)
    if not state:
        await callback.answer("Сессия устарела.", show_alert=True)
        return
    index = int(callback.data.split(":", 1)[1])
    selected = state.setdefault("selected", set())
    if index in selected:
        selected.remove(index)
    else:
        selected.add(index)
    await show_groups(callback, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "broadcast_groups_all")
async def select_all_broadcast_groups(callback: CallbackQuery):
    state = user_states.get(callback.from_user.id)
    if not state:
        await callback.answer("Сессия устарела.", show_alert=True)
        return
    groups = state.get("broadcast_groups", [])
    selected = state.setdefault("selected", set())
    if len(selected) == len(groups):
        selected.clear()
        await callback.answer("Все группы сняты.")
    else:
        selected.update(range(len(groups)))
        await callback.answer("Все группы выбраны.")
    await show_groups(callback, callback.from_user.id)


# ============================================================
# CUSTOM GROUP MANAGEMENT
# ============================================================

@dp.callback_query(F.data == "groups")
async def groups(callback: CallbackQuery):
    user_id = callback.from_user.id
    items = get_user_chat_groups(user_id)
    builder = InlineKeyboardBuilder()
    for item in items:
        name = item["name"]
        if len(name) > 35:
            name = name[:32] + "..."
        builder.button(text=f"📂 {name} ({len(item.get('chat_ids', []))})", callback_data=f"chat_group:{item['id']}")
    builder.button(text="➕ Создать группу", callback_data="create_chat_group")
    builder.button(text="◀️ Назад", callback_data="back_menu")
    builder.adjust(1)
    text = (
        "📂 <b>Мои группы рассылки</b>\n\n"
        + ("Выбирай группу вместо отдельных чатов.\n\n" if items else "Групп пока нет. Создай первую.\n\n")
        + f"Всего групп: <b>{len(items)}</b>"
    )
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "create_chat_group")
async def create_chat_group_start(callback: CallbackQuery):
    user_states[callback.from_user.id] = {"state": "chat_group_name"}
    await callback.message.edit_text(
        "➕ <b>Новая группа рассылки</b>\n\nВведите название.\n\n"
        "Например: <code>Клиенты</code>, <code>Работа</code>, <code>Реклама</code>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("chat_group:"))
async def open_chat_group(callback: CallbackQuery):
    user_id = callback.from_user.id
    group_id = callback.data.split(":", 1)[1]
    item = get_chat_group(group_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить чаты", callback_data=f"edit_chat_group:{group_id}")
    builder.button(text="✏️ Переименовать", callback_data=f"rename_chat_group:{group_id}")
    builder.button(text="🗑 Удалить", callback_data=f"delete_chat_group:{group_id}")
    builder.button(text="◀️ Назад", callback_data="groups")
    builder.adjust(1)
    await callback.message.edit_text(
        f"📂 <b>{html.escape(item['name'])}</b>\n\n"
        f"Чатов в группе: <b>{len(item.get('chat_ids', []))}</b>\n\n"
        "Эта группа будет доступна одним пунктом при создании рассылки.",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


async def load_chat_picker(message_or_callback, user_id):
    chats = await load_chat_dialogs()
    state = user_states[user_id]
    state["chat_picker"] = chats
    selected_ids = {int(x) for x in state.get("chat_picker_selected_ids", set())}
    state["chat_picker_selected_ids"] = selected_ids
    builder = InlineKeyboardBuilder()
    for i, dialog in enumerate(chats):
        prefix = "☑️" if int(dialog.id) in selected_ids else "☐"
        name = dialog.name
        if len(name) > 42:
            name = name[:39] + "..."
        builder.button(text=f"{prefix} {name}", callback_data=f"chat_picker_toggle:{i}")
    builder.button(text="☑️ Выбрать все" if len(selected_ids) < len(chats) else "☑️ Снять все", callback_data="chat_picker_all")
    builder.button(text="✅ Сохранить группу", callback_data="save_chat_group")
    builder.button(text="❌ Отмена", callback_data="cancel")
    builder.adjust(1)
    text = "📋 <b>Чаты группы</b>\n\n" + f"Выбрано: <b>{len(selected_ids)}</b> из <b>{len(chats)}</b>\n\nВыбери чаты, которые должны входить в эту группу."
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message_or_callback.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("edit_chat_group:"))
async def edit_chat_group(callback: CallbackQuery):
    user_id = callback.from_user.id
    group_id = callback.data.split(":", 1)[1]
    item = get_chat_group(group_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    user_states[user_id] = {
        "state": "chat_group_edit_chats",
        "chat_group_id": group_id,
        "chat_group_name": item["name"],
        "chat_picker_selected_ids": set(item.get("chat_ids", [])),
    }
    await load_chat_picker(callback, user_id)
    await callback.answer()


@dp.callback_query(F.data.startswith("rename_chat_group:"))
async def rename_chat_group(callback: CallbackQuery):
    user_id = callback.from_user.id
    group_id = callback.data.split(":", 1)[1]
    item = get_chat_group(group_id)
    if not item or int(item.get("user_id", -1)) != int(user_id):
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    user_states[user_id] = {"state": "chat_group_rename", "chat_group_id": group_id}
    await callback.message.edit_text("✏️ Введите новое название группы:")
    await callback.answer()


@dp.callback_query(F.data.startswith("delete_chat_group:"))
async def delete_chat_group_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    group_id = callback.data.split(":", 1)[1]
    if not delete_chat_group(user_id, group_id):
        await callback.answer("Не удалось удалить группу.", show_alert=True)
        return
    user_states.pop(user_id, None)
    await callback.message.edit_text("🗑 <b>Группа удалена.</b>", reply_markup=main_keyboard(), parse_mode="HTML")
    await callback.answer("Удалено")


@dp.callback_query(F.data.startswith("chat_picker_toggle:"))
async def chat_picker_toggle(callback: CallbackQuery):
    state = user_states.get(callback.from_user.id)
    if not state:
        await callback.answer("Сессия устарела.", show_alert=True)
        return
    i = int(callback.data.split(":", 1)[1])
    dialog = state["chat_picker"][i]
    selected = state["chat_picker_selected_ids"]
    cid = int(dialog.id)
    if cid in selected:
        selected.remove(cid)
    else:
        selected.add(cid)
    await load_chat_picker(callback, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "chat_picker_all")
async def chat_picker_all(callback: CallbackQuery):
    state = user_states.get(callback.from_user.id)
    if not state:
        await callback.answer("Сессия устарела.", show_alert=True)
        return
    chats = state["chat_picker"]
    selected = state["chat_picker_selected_ids"]
    if len(selected) == len(chats):
        selected.clear()
    else:
        selected.update(int(x.id) for x in chats)
    await load_chat_picker(callback, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "save_chat_group")
async def save_chat_group(callback: CallbackQuery):
    user_id = callback.from_user.id
    state = user_states.get(user_id)
    if not state:
        await callback.answer("Сессия устарела.", show_alert=True)
        return
    chat_ids = list(state.get("chat_picker_selected_ids", set()))
    if not chat_ids:
        await callback.answer("Добавь хотя бы один чат.", show_alert=True)
        return
    if state["state"] == "chat_group_edit_chats":
        item = set_chat_group_chats(user_id, state["chat_group_id"], chat_ids)
        msg = f"✅ Группа <b>{html.escape(item['name'])}</b> обновлена.\n\nЧатов: <b>{len(chat_ids)}</b>"
    else:
        item = create_chat_group(user_id, state["chat_group_name"], chat_ids)
        msg = f"✅ Группа <b>{html.escape(item['name'])}</b> создана.\n\nЧатов: <b>{len(chat_ids)}</b>"
    user_states.pop(user_id, None)
    await callback.message.edit_text(msg, reply_markup=main_keyboard(), parse_mode="HTML")
    await callback.answer("Сохранено")


@dp.callback_query(F.data == "continue_groups")
async def continue_groups(callback: CallbackQuery):
    user_id = callback.from_user.id
    state = user_states.get(user_id)
    if not state:
        await callback.answer("Сессия устарела.", show_alert=True)
        return
    selected_indexes = state.get("selected", set())
    if not selected_indexes:
        await callback.answer("Выбери хотя бы одну группу.", show_alert=True)
        return

    selected_groups = [state["broadcast_groups"][i] for i in sorted(selected_indexes)]
    selected_group_ids = [str(group["id"]) for group in selected_groups]
    chat_ids = set()
    for group in selected_groups:
        chat_ids.update(int(x) for x in group.get("chat_ids", []))

    dialogs = await load_chat_dialogs()
    selected_dialogs = [d for d in dialogs if int(d.id) in chat_ids]
    if not selected_dialogs:
        await callback.answer("В выбранных группах не найдено доступных чатов.", show_alert=True)
        return

    # Если шаблон редактирует набор групп — сохраняем именно IDs групп.
    if state.get("state") == "template_change_groups":
        template = get_template(state["template_id"])
        if not template or int(template.get("user_id", -1)) != int(user_id):
            user_states.pop(user_id, None)
            await callback.answer("Шаблон не найден или нет доступа.", show_alert=True)
            return
        updated = set_template_group_ids(user_id, state["template_id"], selected_group_ids)
        user_states.pop(user_id, None)
        await callback.message.edit_text(
            "✅ <b>Группы шаблона обновлены!</b>\n\n"
            f"📄 <b>{html.escape(updated['name'])}</b>\n"
            f"📂 Групп сохранено: <b>{len(selected_group_ids)}</b>",
            reply_markup=main_keyboard(), parse_mode="HTML"
        )
        await callback.answer("Группы сохранены.")
        return

    if state.get("state") == "template_select_groups":
        template = create_template(
            user_id=user_id,
            name=state["template_name"],
            message=state["message"],
            group_ids=selected_group_ids
        )
        user_states.pop(user_id, None)
        names = "\n".join(f"• {html.escape(g['name'])}" for g in selected_groups)
        await callback.message.edit_text(
            "✅ <b>Шаблон создан!</b>\n\n"
            f"📄 <b>{html.escape(template['name'])}</b>\n\n"
            f"📝 {html.escape(template['message'])}\n\n"
            f"📂 <b>Группы:</b>\n{names}",
            reply_markup=main_keyboard(), parse_mode="HTML"
        )
        await callback.answer("Шаблон сохранён с группами.")
        return

    state["groups"] = selected_dialogs
    state["selected"] = set(range(len(selected_dialogs)))
    state["selected_group_ids"] = selected_group_ids
    state["state"] = "settings"

    names = "\n".join(f"• {html.escape(g['name'])}" for g in selected_groups)
    builder = InlineKeyboardBuilder()
    builder.button(text="⚡ Отправить сейчас", callback_data="send_now")
    builder.button(text="⏰ Настроить таймер", callback_data="timer")
    builder.button(text="📂 Изменить группы", callback_data="change_broadcast_groups")
    builder.button(text="❌ Отмена", callback_data="cancel")
    builder.adjust(1)
    await callback.message.edit_text(
        "⚙️ <b>Настройки рассылки</b>\n\n"
        f"<b>Сообщение:</b>\n{html.escape(state['message'])}\n\n"
        f"<b>Группы:</b>\n{names}\n\n"
        f"Чатов будет затронуто: <b>{len(selected_dialogs)}</b>\n\n"
        "Выбери действие:",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "change_broadcast_groups")
async def change_broadcast_groups(callback: CallbackQuery):
    user_id = callback.from_user.id
    state = user_states.get(user_id)
    if not state:
        await callback.answer("Сессия устарела.", show_alert=True)
        return
    state["state"] = "select_groups"
    await load_groups(callback, user_id)
    await callback.answer()


# ============================================================
# SEND NOW
# ============================================================

@dp.callback_query(
    F.data == "send_now"
)
async def send_now(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    state = user_states.get(user_id)

    if not state:

        await callback.answer(
            "Сессия устарела.",
            show_alert=True
        )

        return


    groups = [
        state["groups"][i]
        for i in sorted(
            state["selected"]
        )
    ]

    text = state["message"]


    await callback.message.edit_text(
        "📨 <b>Начинаю рассылку...</b>\n\n"
        f"Групп: <b>{len(groups)}</b>\n"
        "Задержка между отправками: <b>1 сек.</b>"
    )


    success = 0
    failed = 0


    for group in groups:

        try:

            await user_client.send_message(
                group.entity,
                text
            )

            success += 1

            print(
                f"Отправлено: {group.name}"
            )

        except Exception as e:

            failed += 1

            print(
                f"Ошибка {group.name}: {e}"
            )


        # 1 секунда перед следующей отправкой

        await asyncio.sleep(1)


    await callback.message.edit_text(
        "✅ <b>Рассылка завершена</b>\n\n"
        f"Успешно: <b>{success}</b>\n"
        f"Ошибок: <b>{failed}</b>",
        reply_markup=main_keyboard()
    )


    user_states.pop(
        user_id,
        None
    )

    await callback.answer()


# ============================================================
# START TIMER
# ============================================================

@dp.callback_query(
    F.data == "timer"
)
async def timer(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    state = user_states.get(user_id)

    if not state:

        await callback.answer(
            "Сессия устарела.",
            show_alert=True
        )

        return


    state["state"] = "waiting_timer"


    await callback.message.edit_text(
        "⏰ <b>Настройка таймера</b>\n\n"
        "Через сколько минут делать "
        "следующую отправку?\n\n"
        "Например:\n"
        "<code>20</code>\n\n"
        "Можно указать любое положительное "
        "целое число."
    )

    await callback.answer()


# ============================================================
# START TIMER / SAVE TO REDIS
# ============================================================

@dp.callback_query(
    F.data == "start_timer"
)
async def start_timer(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    state = user_states.get(user_id)

    if not state:

        await callback.answer(
            "Сессия устарела.",
            show_alert=True
        )

        return


    groups = [
        state["groups"][i]
        for i in sorted(
            state["selected"]
        )
    ]


    group_ids = [
        group.id
        for group in groups
    ]


    task = create_task(

        user_id=user_id,

        message=state["message"],

        groups=group_ids,

        interval_minutes=state["interval"],

        repeats=state["repeats"]
    )


    await callback.message.edit_text(
        "✅ <b>Таймер создан!</b>\n\n"

        f"⏱ Интервал: "
        f"<b>{task['interval_minutes']} мин.</b>\n"

        f"🔁 Повторов: "
        f"<b>{task['total_repeats']}</b>\n"

        f"📊 Выполнено: "
        f"<b>0/{task['total_repeats']}</b>\n\n"

        f"📋 Групп: "
        f"<b>{len(group_ids)}</b>\n\n"

        "⏳ Первая отправка будет выполнена "
        "после наступления указанного интервала.\n\n"

        f"ID задачи:\n"
        f"<code>{task['id']}</code>",

        reply_markup=main_keyboard()
    )


    user_states.pop(
        user_id,
        None
    )

    await callback.answer(
        "Таймер сохранён."
    )


# ============================================================
# ACTIVE TASKS
# ============================================================

@dp.callback_query(
    F.data == "active_tasks"
)
async def active_tasks(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    tasks = get_user_tasks(
        user_id
    )


    active = [
        task
        for task in tasks
        if task.get("status") == "active"
    ]


    if not active:

        await callback.message.edit_text(
            "📊 <b>Активных рассылок нет.</b>\n\n"
            "Создай таймер через "
            "«📨 Новая рассылка».",
            reply_markup=main_keyboard()
        )

        await callback.answer()

        return


    lines = []


    for number, task in enumerate(
        active,
        start=1
    ):

        message_text = task["message"]

        if len(message_text) > 80:

            message_text = (
                message_text[:77]
                + "..."
            )


        lines.append(
            f"<b>{number}.</b>\n"
            f"🔁 Повторы: "
            f"<b>{task['completed_repeats']}/"
            f"{task['total_repeats']}</b>\n"
            f"⏱ Интервал: "
            f"<b>{task['interval_minutes']} мин.</b>\n"
            f"📨 {message_text}"
        )


    builder = InlineKeyboardBuilder()


    for task in active:

        builder.button(
            text=(
                f"🛑 Остановить "
                f"{task['id'][:8]}"
            ),
            callback_data=(
                f"stop:{task['id']}"
            )
        )


    builder.button(
        text="◀️ Назад",
        callback_data="back_menu"
    )


    builder.adjust(1)


    await callback.message.edit_text(
        "📊 <b>Активные рассылки</b>\n\n"
        + "\n\n".join(lines),
        reply_markup=builder.as_markup()
    )


    await callback.answer()


# ============================================================
# STOP TASK
# ============================================================

@dp.callback_query(
    F.data.startswith("stop:")
)
async def stop_task(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    task_id = callback.data.split(
        ":",
        1
    )[1]


    task = get_task(
        task_id
    )


    if not task:

        await callback.answer(
            "Задача не найдена.",
            show_alert=True
        )

        return


    if task["user_id"] != user_id:

        await callback.answer(
            "Нет доступа.",
            show_alert=True
        )

        return


    task["status"] = "cancelled"

    save_task(task)


    await callback.message.edit_text(
        "🛑 <b>Рассылка остановлена.</b>",
        reply_markup=main_keyboard()
    )


    await callback.answer(
        "Таймер остановлен."
    )


# ============================================================
# BACK TO MENU
# ============================================================

@dp.callback_query(
    F.data == "back_menu"
)
async def back_menu(
    callback: CallbackQuery
):

    await callback.message.edit_text(
        "🤖 <b>Панель управления</b>\n\n"
        "Выбери действие:",
        reply_markup=main_keyboard()
    )

    await callback.answer()


# ============================================================
# CANCEL
# ============================================================

@dp.callback_query(
    F.data == "cancel"
)
async def cancel(
    callback: CallbackQuery
):

    user_states.pop(
        callback.from_user.id,
        None
    )


    await callback.message.edit_text(
        "❌ <b>Операция отменена.</b>",
        reply_markup=main_keyboard()
    )


    await callback.answer()


# ============================================================
# RESTART BOT
# ============================================================

@dp.callback_query(
    F.data == "restart"
)
async def restart_bot(
    callback: CallbackQuery
):

    import sys
    import os

    await callback.message.edit_text(
        "🔄 <b>Бот перезапускается...</b>\n\n"
        "Подождите несколько секунд.",
        reply_markup=main_keyboard()
    )

    await callback.answer()

    
    os.execv(
        sys.executable,
        [sys.executable] + sys.argv
    )


# ============================================================
# RESTART BOT (COMMAND)
# ============================================================

@dp.message(Command("restart"))
async def restart_command(message: Message):

    import sys
    import os

    await message.answer(
        "🔄 <b>Бот перезапускается...</b>\n\n"
        "Подождите несколько секунд.",
        reply_markup=main_keyboard()
    )

    
    os.execv(
        sys.executable,
        [sys.executable] + sys.argv
    )


# ============================================================
# START APPLICATION
# ============================================================

async def main():

    print(
        "Подключаем пользовательский аккаунт..."
    )


    await user_client.connect()


    if not await user_client.is_user_authorized():

        raise RuntimeError(
            "Telegram-сессия не авторизована."
        )


    me = await user_client.get_me()


    print(
        f"Пользовательский аккаунт: "
        f"{me.first_name} "
        f"{me.last_name or ''}"
    )


    print(
        "Бот запускается..."
    )


    await dp.start_polling(
        bot
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "\nБот остановлен."
        )