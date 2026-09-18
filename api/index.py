import os
import asyncio
import html

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, Update
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telethon import TelegramClient
from telethon.sessions import StringSession

from qstash import QStash

from redis_storage import (
    save_last_message,
    get_last_message,
    create_task,
    get_user_tasks,
    get_task,
    save_task,
    acquire_task_lock,
    release_task_lock,
    create_template,
    get_template,
    get_user_templates,
    update_template,
    delete_template,
)


# =========================================================
# ENVIRONMENT VARIABLES
# =========================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SESSION_STRING = os.environ["TELEGRAM_SESSION_STRING"]

QSTASH_TOKEN = os.environ["QSTASH_TOKEN"]

APP_URL = os.environ["APP_URL"].rstrip("/")

QSTASH_SECRET = os.environ.get("QSTASH_SECRET")


# =========================================================
# FASTAPI / TELEGRAM
# =========================================================

app = FastAPI()

bot = Bot(BOT_TOKEN)

dp = Dispatcher()

qstash = QStash(
    QSTASH_TOKEN
)


# =========================================================
# USER STATES
#
# ВАЖНО:
# Это временное состояние пользователя.
# Оно хранится только в памяти текущего Vercel-инстанса.
# Redis используется для постоянных данных/таймеров.
# =========================================================

states = {}


# =========================================================
# MAIN MENU
# =========================================================

def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📨 Новая рассылка", callback_data="new")
    kb.button(text="📚 Мои шаблоны", callback_data="templates")
    kb.button(text="🔁 Последнее сообщение", callback_data="last")
    kb.button(text="📊 Мои таймеры", callback_data="tasks")
    kb.button(text="📋 Мои группы", callback_data="groups")
    kb.button(text="⚙️ Настройки", callback_data="settings")
    kb.adjust(2, 2, 2)
    return kb.as_markup()


# TELEGRAM USER CLIENT
# =========================================================

async def create_client():

    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH
    )

    await client.connect()

    if not await client.is_user_authorized():

        await client.disconnect()

        raise RuntimeError(
            "Telegram session is not authorized"
        )

    return client


# =========================================================
# START
# =========================================================

@dp.message(CommandStart())
async def start(
    message: Message
):

    user_id = message.from_user.id

    states.pop(
        user_id,
        None
    )

    await message.answer(
        "🤖 <b>Панель рассылки</b>\n\n"
        "Выбери действие:",
        reply_markup=main_menu()
    )


# =========================================================
@dp.message(Command("s"))
async def show_menu_command(message: Message):
    states.pop(message.from_user.id, None)
    await message.answer(
        "🤖 <b>Панель рассылки</b>\n\nВыбери действие:",
        reply_markup=main_menu(),
        parse_mode="HTML"
    )


# NEW MESSAGE
# =========================================================

@dp.callback_query(
    F.data == "new"
)
async def new_message(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Написать сообщение", callback_data="write_message")
    kb.button(text="📚 Выбрать шаблон", callback_data="templates_for_broadcast")
    kb.button(text="🔁 Последнее сообщение", callback_data="last")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1)
    await callback.message.edit_text(
        "📨 <b>Новая рассылка</b>\n\nКак создать сообщение?",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "write_message")
async def write_message(callback: CallbackQuery):
    states[callback.from_user.id] = {"state": "message"}
    await callback.message.edit_text(
        "📝 <b>Введите сообщение</b>\n\nНапиши текст, который нужно отправить в выбранные группы.",
        parse_mode="HTML"
    )
    await callback.answer()


# =========================================================
# TEMPLATES
# =========================================================

@dp.callback_query(F.data == "templates")
async def templates_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    items = get_user_templates(user_id)
    kb = InlineKeyboardBuilder()
    for template in items:
        name = template["name"]
        if len(name) > 35:
            name = name[:32] + "..."
        kb.button(text=f"📄 {name}", callback_data=f"template:{template['id']}")
    kb.button(text="➕ Создать шаблон", callback_data="create_template")
    kb.button(text="◀️ Назад", callback_data="back")
    kb.adjust(1)
    text = "📚 <b>Мои шаблоны</b>\n\n" + (
        f"Всего: <b>{len(items)}</b>\n\nВыберите шаблон:"
        if items else
        "Шаблонов пока нет.\n\nСоздайте первый шаблон."
    )
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "templates_for_broadcast")
async def templates_for_broadcast(callback: CallbackQuery):
    items = get_user_templates(callback.from_user.id)
    if not items:
        await callback.answer("Сначала создай хотя бы один шаблон.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for template in items:
        name = template["name"]
        if len(name) > 35:
            name = name[:32] + "..."
        kb.button(text=f"📄 {name}", callback_data=f"use_template:{template['id']}")
    kb.button(text="➕ Создать шаблон", callback_data="create_template")
    kb.button(text="◀️ Назад", callback_data="new")
    kb.adjust(1)
    await callback.message.edit_text(
        "📚 <b>Выбор шаблона</b>\n\nВыберите текст для новой рассылки:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "create_template")
async def create_template_start(callback: CallbackQuery):
    states[callback.from_user.id] = {"state": "template_name"}
    await callback.message.edit_text(
        "➕ <b>Новый шаблон</b>\n\nВведите название шаблона.\n\nНапример: <code>Акция</code>",
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
    kb = InlineKeyboardBuilder()
    kb.button(text="📨 Использовать", callback_data=f"use_template:{tid}")
    kb.button(text="✏️ Изменить", callback_data=f"edit_template:{tid}")
    kb.button(text="🗑 Удалить", callback_data=f"delete_template:{tid}")
    kb.button(text="◀️ Назад", callback_data="templates")
    kb.adjust(1)
    await callback.message.edit_text(
        f"📄 <b>{html.escape(template['name'])}</b>\n\n{html.escape(template['message'])}",
        reply_markup=kb.as_markup(),
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
    states[user_id] = {"state": "groups", "message": template["message"], "template_id": template["id"]}
    await show_groups(callback, user_id)
    await callback.answer("Шаблон выбран.")


@dp.callback_query(F.data.startswith("edit_template:"))
async def edit_template_start(callback: CallbackQuery):
    user_id = callback.from_user.id
    template = get_template(callback.data.split(":", 1)[1])
    if not template or int(template.get("user_id", -1)) != int(user_id):
        await callback.answer("Шаблон не найден или нет доступа.", show_alert=True)
        return
    states[user_id] = {"state": "template_edit_name", "template_id": template["id"]}
    await callback.message.edit_text(
        "✏️ <b>Изменение шаблона</b>\n\nВведите новое название шаблона.",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("delete_template:"))
async def delete_template_handler(callback: CallbackQuery):
    if not delete_template(callback.from_user.id, callback.data.split(":", 1)[1]):
        await callback.answer("Не удалось удалить шаблон.", show_alert=True)
        return
    await callback.message.edit_text(
        "🗑 <b>Шаблон удалён.</b>",
        reply_markup=main_menu(),
        parse_mode="HTML"
    )
    await callback.answer("Удалено")


# LAST MESSAGE
# =========================================================

@dp.callback_query(
    F.data == "last"
)
async def last_message(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    text = get_last_message(
        user_id
    )

    if not text:

        await callback.answer(
            "Последнего сообщения нет.",
            show_alert=True
        )

        return

    states[user_id] = {
        "state": "groups",
        "message": text
    }

    await show_groups(
        callback,
        user_id
    )

    await callback.answer()


# =========================================================
# TEXT HANDLER
# =========================================================

@dp.message()
async def text_handler(
    message: Message
):

    user_id = message.from_user.id

    state = states.get(
        user_id
    )

    if not state:
        return

    if not message.text:
        return


    # -----------------------------------------------------
    # MESSAGE
    # -----------------------------------------------------

    if state["state"] == "message":

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

        state["state"] = "groups"

        # Сразу получаем группы
        # и сохраняем их в состоянии.

        groups = await get_groups()

        state["groups"] = groups

        state["selected"] = set()

        await show_groups(
            message,
            user_id
        )

        return


    # -----------------------------------------------------
    if state["state"] == "template_name":
        name = message.text.strip()
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

    if state["state"] == "template_message":
        text = message.text.strip()
        if not text:
            await message.answer("❌ Текст шаблона не может быть пустым.")
            return
        template = create_template(user_id, state["template_name"], text)
        states.pop(user_id, None)
        await message.answer(
            f"✅ <b>Шаблон создан!</b>\n\n📄 <b>{html.escape(template['name'])}</b>\n\n{html.escape(template['message'])}",
            reply_markup=main_menu(),
            parse_mode="HTML"
        )
        return

    if state["state"] == "template_edit_name":
        name = message.text.strip()
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

    if state["state"] == "template_edit_message":
        text = message.text.strip()
        if not text:
            await message.answer("❌ Текст шаблона не может быть пустым.")
            return
        template = update_template(user_id, state["template_id"], state["template_name"], text)
        states.pop(user_id, None)
        if not template:
            await message.answer("❌ Шаблон не найден.", reply_markup=main_menu())
            return
        await message.answer(
            f"✅ <b>Шаблон изменён!</b>\n\n📄 <b>{html.escape(template['name'])}</b>\n\n{html.escape(template['message'])}",
            reply_markup=main_menu(),
            parse_mode="HTML"
        )
        return


    # TIMER
    # -----------------------------------------------------

    if state["state"] == "timer":

        try:

            minutes = int(
                message.text.strip()
            )

            if minutes <= 0:
                raise ValueError

        except ValueError:

            await message.answer(
                "❌ Введи положительное "
                "число минут.\n\n"
                "Например: <code>20</code>"
            )

            return

        state["interval"] = minutes

        state["state"] = "repeats"

        await message.answer(
            "🔁 <b>Количество повторов</b>\n\n"
            "Например: <code>5</code>"
        )

        return


    # -----------------------------------------------------
    # REPEATS
    # -----------------------------------------------------

    if state["state"] == "repeats":

        try:

            repeats = int(
                message.text.strip()
            )

            if repeats <= 0:
                raise ValueError

        except ValueError:

            await message.answer(
                "❌ Введи положительное "
                "количество повторов."
            )

            return

        state["repeats"] = repeats

        state["state"] = "confirm"

        kb = InlineKeyboardBuilder()

        kb.button(
            text="🚀 Создать таймер",
            callback_data="create_timer"
        )

        kb.button(
            text="❌ Отмена",
            callback_data="cancel"
        )

        kb.adjust(1)

        await message.answer(
            "⏰ <b>Настройки таймера</b>\n\n"
            f"Интервал: "
            f"<b>{state['interval']} мин.</b>\n"
            f"Повторов: "
            f"<b>{repeats}</b>\n\n"
            f"Первая отправка через "
            f"<b>{state['interval']} мин.</b>",
            reply_markup=kb.as_markup()
        )

        return


# =========================================================
# GET GROUPS FROM TELEGRAM
# =========================================================

async def get_groups():

    client = await create_client()

    groups = []

    try:

        async for dialog in client.iter_dialogs():

            if dialog.is_group:

                groups.append(dialog)

    finally:

        await client.disconnect()

    return groups


# =========================================================
# SHOW GROUPS
#
# ВАЖНО:
# Здесь selected НЕ очищается.
#
# Раньше была ошибка:
#
# state["selected"] = set()
#
# каждый раз выполнялась при перерисовке меню.
#
# Из-за этого после нажатия на группу
# выбор сразу сбрасывался.
# =========================================================

async def show_groups(
    target,
    user_id
):

    state = states.get(
        user_id
    )

    if not state:
        return


    # -----------------------------------------------------
    # Получаем группы только если их ещё нет
    # -----------------------------------------------------

    if "groups" not in state:

        groups = await get_groups()

        state["groups"] = groups


    groups = state["groups"]


    # -----------------------------------------------------
    # Создаём selected только один раз
    # -----------------------------------------------------

    if "selected" not in state:

        state["selected"] = set()


    selected = state["selected"]


    # -----------------------------------------------------
    # Клавиатура
    # -----------------------------------------------------

    kb = InlineKeyboardBuilder()


    for index, group in enumerate(groups):

        name = group.name

        if len(name) > 35:

            name = name[:32] + "..."


        # Если группа выбрана
        if index in selected:

            prefix = "☑️"

        else:

            prefix = "⬜"


        kb.button(
            text=f"{prefix} {name}",
            callback_data=f"group:{index}"
        )


    # -----------------------------------------------------
    # SELECT ALL
    # -----------------------------------------------------

    if groups and len(selected) == len(groups):

        all_text = "⬜ Снять выбор со всех"

    else:

        all_text = "☑️ Выбрать все"


    kb.button(
        text=all_text,
        callback_data="all"
    )


    # -----------------------------------------------------
    # CONTINUE
    # -----------------------------------------------------

    kb.button(
        text=f"✅ Продолжить ({len(selected)})",
        callback_data="continue"
    )


    # -----------------------------------------------------
    # CANCEL
    # -----------------------------------------------------

    kb.button(
        text="❌ Отмена",
        callback_data="cancel"
    )


    kb.adjust(1)


    # -----------------------------------------------------
    # TEXT
    # -----------------------------------------------------

    text = (
        "📋 <b>Выбор групп</b>\n\n"
        f"Всего групп: <b>{len(groups)}</b>\n"
        f"Выбрано: <b>{len(selected)}</b>\n\n"
        "Нажимай на группы для выбора:"
    )


    # -----------------------------------------------------
    # EDIT EXISTING MESSAGE
    # -----------------------------------------------------

    if isinstance(
        target,
        CallbackQuery
    ):

        await target.message.edit_text(
            text,
            reply_markup=kb.as_markup()
        )

    # -----------------------------------------------------
    # SEND NEW MESSAGE
    # -----------------------------------------------------

    else:

        await target.answer(
            text,
            reply_markup=kb.as_markup()
        )


# =========================================================
# GROUP TOGGLE
# =========================================================

@dp.callback_query(
    F.data.startswith("group:")
)
async def group_toggle(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    state = states.get(
        user_id
    )


    if not state:

        await callback.answer(
            "Сессия устарела. Нажми /start",
            show_alert=True
        )

        return


    # -----------------------------------------------------
    # Индекс группы
    # -----------------------------------------------------

    try:

        index = int(
            callback.data.split(":")[1]
        )

    except (ValueError, IndexError):

        await callback.answer(
            "Ошибка выбора группы.",
            show_alert=True
        )

        return


    groups = state.get(
        "groups",
        []
    )


    if index < 0 or index >= len(groups):

        await callback.answer(
            "Группа не найдена.",
            show_alert=True
        )

        return


    # -----------------------------------------------------
    # Получаем СУЩЕСТВУЮЩИЙ selected
    # -----------------------------------------------------

    selected = state.setdefault(
        "selected",
        set()
    )


    # -----------------------------------------------------
    # Переключаем состояние
    # -----------------------------------------------------

    if index in selected:

        selected.remove(
            index
        )

        answer_text = "Группа снята"

    else:

        selected.add(
            index
        )

        answer_text = "Группа выбрана"


    # -----------------------------------------------------
    # Перерисовываем меню
    #
    # selected НЕ сбрасывается!
    # -----------------------------------------------------

    await show_groups(
        callback,
        user_id
    )


    await callback.answer(
        answer_text
    )


# =========================================================
# SELECT ALL
# =========================================================

@dp.callback_query(
    F.data == "all"
)
async def select_all(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    state = states.get(
        user_id
    )


    if not state:

        await callback.answer(
            "Сессия устарела. Нажми /start",
            show_alert=True
        )

        return


    groups = state.get(
        "groups",
        []
    )


    selected = state.setdefault(
        "selected",
        set()
    )


    # -----------------------------------------------------
    # Если уже всё выбрано —
    # снимаем выбор со всех
    # -----------------------------------------------------

    if groups and len(selected) == len(groups):

        selected.clear()

        answer_text = "Выбор со всех снят"


    # -----------------------------------------------------
    # Иначе выбираем все
    # -----------------------------------------------------

    else:

        selected.clear()

        selected.update(
            range(len(groups))
        )

        answer_text = "Выбраны все группы"


    await show_groups(
        callback,
        user_id
    )


    await callback.answer(
        answer_text
    )


# =========================================================
# CONTINUE
# =========================================================

@dp.callback_query(
    F.data == "continue"
)
async def continue_groups(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    state = states.get(
        user_id
    )


    if not state:

        await callback.answer(
            "Сессия устарела.",
            show_alert=True
        )

        return


    selected = state.get(
        "selected",
        set()
    )


    if not selected:

        await callback.answer(
            "Выбери хотя бы одну группу.",
            show_alert=True
        )

        return


    kb = InlineKeyboardBuilder()


    kb.button(
        text="⚡ Отправить сейчас",
        callback_data="send"
    )

    kb.button(
        text="⏰ Задать таймер",
        callback_data="timer"
    )

    kb.button(
        text="❌ Отмена",
        callback_data="cancel"
    )


    kb.adjust(1)


    await callback.message.edit_text(
        "⚙️ <b>Настройка рассылки</b>\n\n"
        f"Выбрано групп: "
        f"<b>{len(selected)}</b>\n\n"
        "Выбери действие:",
        reply_markup=kb.as_markup()
    )


    await callback.answer()


# =========================================================
# SEND NOW
# =========================================================

@dp.callback_query(
    F.data == "send"
)
async def send_now(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    state = states.get(
        user_id
    )


    if not state:
        return


    await callback.message.edit_text(
        "📨 <b>Начинаю отправку...</b>"
    )


    client = await create_client()

    success = 0
    failed = 0


    try:

        selected = state.get(
            "selected",
            set()
        )

        groups = state.get(
            "groups",
            []
        )


        for index in selected:

            group = groups[index]


            try:

                await client.send_message(
                    group.entity,
                    state["message"]
                )

                success += 1


            except Exception as e:

                failed += 1

                print(
                    f"Send error: {e}"
                )


            # 1 секунда между группами

            await asyncio.sleep(1)


    finally:

        await client.disconnect()


    states.pop(
        user_id,
        None
    )


    await callback.message.edit_text(
        "✅ <b>Рассылка завершена</b>\n\n"
        f"Успешно: <b>{success}</b>\n"
        f"Ошибок: <b>{failed}</b>",
        reply_markup=main_menu()
    )


    await callback.answer()


# =========================================================
# TIMER INPUT
# =========================================================

@dp.callback_query(
    F.data == "timer"
)
async def timer_start(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    state = states.get(
        user_id
    )


    if not state:
        return


    state["state"] = "timer"


    await callback.message.edit_text(
        "⏰ <b>Задай интервал</b>\n\n"
        "Через сколько минут повторять "
        "сообщение?\n\n"
        "Например:\n"
        "<code>20</code>"
    )


    await callback.answer()


# =========================================================
# QSTASH SCHEDULE
# =========================================================

def schedule_task(
    task_id: str,
    delay_minutes: int
):

    body = {
        "task_id": task_id
    }


    headers = {}


    if QSTASH_SECRET:

        headers[
            "X-Cron-Secret"
        ] = QSTASH_SECRET


    result = qstash.message.publish_json(

        url=f"{APP_URL}/api/process",

        body=body,

        headers=headers,

        delay=f"{delay_minutes}m",

        retries=3
    )


    print(
        "QStash message:",
        result.message_id
    )


    return result


# =========================================================
# CREATE TIMER
# =========================================================

@dp.callback_query(
    F.data == "create_timer"
)
async def create_timer(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    state = states.get(
        user_id
    )


    if not state:

        await callback.answer(
            "Сессия устарела.",
            show_alert=True
        )

        return


    selected = state.get(
        "selected",
        set()
    )

    groups = state.get(
        "groups",
        []
    )


    if not selected:

        await callback.answer(
            "Группы не выбраны.",
            show_alert=True
        )

        return


    # -----------------------------------------------------
    # ID выбранных групп
    # -----------------------------------------------------

    group_ids = [

        int(groups[index].id)

        for index in selected
    ]


    # -----------------------------------------------------
    # Создаём задачу в Redis
    # -----------------------------------------------------

    task = create_task(

        user_id=user_id,

        message=state["message"],

        groups=group_ids,

        interval_minutes=state["interval"],

        repeats=state["repeats"]
    )


    # -----------------------------------------------------
    # Создаём первый QStash запуск
    # -----------------------------------------------------

    try:

        schedule_task(
            task["id"],
            task["interval_minutes"]
        )


    except Exception as e:

        task["status"] = "error"

        save_task(task)

        print(
            f"QStash error: {e}"
        )


        await callback.message.edit_text(
            "❌ <b>Не удалось создать таймер.</b>\n\n"
            "Проверь QSTASH_TOKEN, "
            "APP_URL и настройки QStash."
        )

        await callback.answer()

        return


    states.pop(
        user_id,
        None
    )


    await callback.message.edit_text(
        "✅ <b>Таймер создан!</b>\n\n"
        f"⏱ Интервал: "
        f"<b>{task['interval_minutes']} мин.</b>\n"
        f"🔁 Повторов: "
        f"<b>{task['total_repeats']}</b>\n"
        f"📋 Групп: "
        f"<b>{len(group_ids)}</b>\n\n"
        "Первая отправка будет автоматически "
        "выполнена через заданный интервал.",
        reply_markup=main_menu()
    )


    await callback.answer()


# =========================================================
# ACTIVE TASKS
# =========================================================

@dp.callback_query(
    F.data == "tasks"
)
async def tasks(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    tasks_list = get_user_tasks(
        user_id
    )


    active = [
        task
        for task in tasks_list
        if task.get("status") == "active"
    ]


    if not active:

        await callback.message.edit_text(
            "📊 <b>Активных таймеров нет.</b>",
            reply_markup=main_menu()
        )

        await callback.answer()

        return


    text = (
        "📊 <b>Активные таймеры</b>\n\n"
    )


    kb = InlineKeyboardBuilder()


    for task in active:

        message_preview = task.get(
            "message",
            ""
        )

        if len(message_preview) > 60:

            message_preview = (
                message_preview[:57]
                + "..."
            )


        text += (
            f"🔁 "
            f"{task['completed_repeats']}/"
            f"{task['total_repeats']}\n"
            f"⏱ "
            f"{task['interval_minutes']} мин.\n"
            f"📝 "
            f"{message_preview}\n\n"
        )


        kb.button(
            text=f"🛑 Остановить {task['id'][:8]}",
            callback_data=f"stop:{task['id']}"
        )


    kb.button(
        text="◀️ Назад",
        callback_data="back"
    )


    kb.adjust(1)


    await callback.message.edit_text(
        text,
        reply_markup=kb.as_markup()
    )


    await callback.answer()


# =========================================================
# STOP TASK
# =========================================================

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


    if int(task["user_id"]) != int(
        user_id
    ):

        await callback.answer(
            "Нет доступа.",
            show_alert=True
        )

        return


    task["status"] = "cancelled"

    save_task(task)


    await callback.message.edit_text(
        "🛑 <b>Таймер остановлен.</b>",
        reply_markup=main_menu()
    )


    await callback.answer()


# =========================================================
@dp.callback_query(F.data == "settings")
async def settings_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "⚙️ <b>Настройки</b>\n\nДополнительные настройки можно добавить сюда позже.",
        reply_markup=main_menu(),
        parse_mode="HTML"
    )
    await callback.answer()


# GROUP LIST
# =========================================================

@dp.callback_query(
    F.data == "groups"
)
async def groups(
    callback: CallbackQuery
):

    try:

        group_list = await get_groups()

    except Exception as e:

        print(
            f"Get groups error: {e}"
        )

        await callback.message.edit_text(
            "❌ Не удалось получить список групп.\n\n"
            "Проверь Telegram session.",
            reply_markup=main_menu()
        )

        await callback.answer()

        return


    if not group_list:

        text = (
            "📋 <b>Групп не найдено.</b>"
        )

    else:

        text = (
            "📋 <b>Мои группы</b>\n\n"
        )


        for index, group in enumerate(
            group_list,
            start=1
        ):

            text += (
                f"{index}. "
                f"{group.name}\n"
            )


    await callback.message.edit_text(
        text,
        reply_markup=main_menu()
    )


    await callback.answer()


# =========================================================
# CANCEL / BACK
# =========================================================

@dp.callback_query(
    F.data.in_([
        "cancel",
        "back"
    ])
)
async def cancel(
    callback: CallbackQuery
):

    states.pop(
        callback.from_user.id,
        None
    )


    await callback.message.edit_text(
        "🤖 <b>Главное меню</b>",
        reply_markup=main_menu()
    )


    await callback.answer()


# =========================================================
# QSTASH PROCESS
# =========================================================

@app.post("/api/process")
async def process(
    request: Request
):

    # -----------------------------------------------------
    # Проверка нашего секретного заголовка
    # -----------------------------------------------------

    if QSTASH_SECRET:

        received = request.headers.get(
            "X-Cron-Secret"
        )


        if received != QSTASH_SECRET:

            return JSONResponse(
                {
                    "ok": False,
                    "error": "Unauthorized"
                },
                status_code=401
            )


    # -----------------------------------------------------
    # Получаем JSON
    # -----------------------------------------------------

    try:

        data = await request.json()

    except Exception:

        return JSONResponse(
            {
                "ok": False,
                "error": "Invalid JSON"
            },
            status_code=400
        )


    task_id = data.get(
        "task_id"
    )


    if not task_id:

        return JSONResponse(
            {
                "ok": False,
                "error": "task_id missing"
            },
            status_code=400
        )


    # -----------------------------------------------------
    # Загружаем задачу
    # -----------------------------------------------------

    task = get_task(
        task_id
    )


    if not task:

        return {
            "ok": True,
            "status": "task_not_found"
        }


    # -----------------------------------------------------
    # Если задача уже остановлена/завершена
    # -----------------------------------------------------

    if task["status"] != "active":

        return {
            "ok": True,
            "status": task["status"]
        }


    # -----------------------------------------------------
    # LOCK
    # -----------------------------------------------------

    if not acquire_task_lock(
        task_id,
        90
    ):

        return {
            "ok": True,
            "status": "already_processing"
        }


    try:

        client = await create_client()


        success = 0
        failed = 0


        try:

            # -------------------------------------------------
            # Получаем актуальные группы пользователя
            # -------------------------------------------------

            dialogs = {}


            async for dialog in client.iter_dialogs():

                if dialog.is_group:

                    dialogs[
                        int(dialog.id)
                    ] = dialog


            # -------------------------------------------------
            # Отправляем сообщение
            # -------------------------------------------------

            for group_id in task["groups"]:

                group = dialogs.get(
                    int(group_id)
                )


                if not group:

                    failed += 1

                    continue


                try:

                    await client.send_message(
                        group.entity,
                        task["message"]
                    )

                    success += 1


                except Exception as e:

                    failed += 1

                    print(
                        f"Scheduled send error "
                        f"{group_id}: {e}"
                    )


                # -------------------------------------------------
                # Задержка 1 секунда между группами
                # -------------------------------------------------

                await asyncio.sleep(1)


        finally:

            await client.disconnect()


        # -----------------------------------------------------
        # Увеличиваем количество выполненных повторов
        # -----------------------------------------------------

        task["completed_repeats"] += 1


        # -----------------------------------------------------
        # Все повторы выполнены
        # -----------------------------------------------------

        if (
            task["completed_repeats"]
            >= task["total_repeats"]
        ):

            task["status"] = "completed"

            save_task(task)


            return {
                "ok": True,
                "status": "completed",
                "success": success,
                "failed": failed
            }


        # -----------------------------------------------------
        # Следующий запуск
        # -----------------------------------------------------

        task["next_run"] = (
            task["next_run"]
            + task["interval_minutes"] * 60
        )


        save_task(task)


        # -----------------------------------------------------
        # Создаём следующий QStash запуск
        # -----------------------------------------------------

        schedule_task(
            task["id"],
            task["interval_minutes"]
        )


        return {
            "ok": True,
            "status": "scheduled_next",
            "success": success,
            "failed": failed,
            "completed": task[
                "completed_repeats"
            ],
            "total": task[
                "total_repeats"
            ]
        }


    except Exception as e:

        print(
            f"Process task error: {e}"
        )

        return JSONResponse(
            {
                "ok": False,
                "error": str(e)
            },
            status_code=500
        )


    finally:

        release_task_lock(
            task_id
        )


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

@app.post("/api/webhook")
async def webhook(
    request: Request
):

    try:

        body = await request.json()

        update = Update.model_validate(
            body
        )

        await dp.feed_update(
            bot,
            update
        )

        return {
            "ok": True
        }


    except Exception as e:

        print(
            f"Webhook error: {e}"
        )

        return JSONResponse(
            {
                "ok": False,
                "error": str(e)
            },
            status_code=500
        )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/")
async def root():

    return {
        "ok": True,
        "service": "telegram-broadcast-bot"
    }
