import html
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, Update
from aiogram.utils.keyboard import InlineKeyboardBuilder
from qstash import QStash

from redis_storage import (
    save_last_message, get_last_message,
    create_task, get_user_tasks, get_task, save_task,
    create_template, get_template, get_user_templates,
    update_template, delete_template,
    create_group_set, get_group_set, get_user_group_sets,
    update_group_set, delete_group_set,
    save_user_state, get_user_state, delete_user_state,
    acquire_task_lock, release_task_lock,
)
from worker_client import get_chats, send_message

BOT_TOKEN = os.environ["BOT_TOKEN"]
APP_URL = os.environ["APP_URL"].rstrip("/")
QSTASH_TOKEN = os.environ.get("QSTASH_TOKEN")
QSTASH_SECRET = os.environ.get("QSTASH_SECRET")

app = FastAPI()
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
qstash = QStash(QSTASH_TOKEN) if QSTASH_TOKEN else None


def menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📨 Новая рассылка", callback_data="new")
    kb.button(text="📚 Мои шаблоны", callback_data="templates")
    kb.button(text="🔁 Последнее сообщение", callback_data="last")
    kb.button(text="📊 Мои таймеры", callback_data="tasks")
    kb.button(text="📋 Мои группы", callback_data="groups")
    kb.button(text="⚙️ Настройки", callback_data="settings")
    kb.adjust(2, 2, 2)
    return kb.as_markup()


def state(user_id):
    return get_user_state(user_id) or {}


def set_state(user_id, value):
    save_user_state(user_id, value)


def clear_state(user_id):
    delete_user_state(user_id)


def unique(values):
    return list(dict.fromkeys(str(v) for v in values))


def chat_label(chat):
    icon = "📢" if chat.get("type") == "channel" else "👥"
    title = chat.get("title") or "Без названия"
    return f"{icon} {title}"


def resolve_union(chats, selected_chat_ids, selected_set_ids, sets):
    result = unique(selected_chat_ids)
    by_id = {str(c["id"]): c for c in chats}
    for set_id in selected_set_ids:
        item = next((x for x in sets if x["id"] == set_id), None)
        if item:
            result.extend(str(x) for x in item.get("groups", []))
    result = unique(result)
    return [cid for cid in result if cid in by_id]


def schedule_task(task_id, delay_minutes):
    if not qstash:
        raise RuntimeError("QSTASH_TOKEN не настроен")
    headers = {"X-Cron-Secret": QSTASH_SECRET} if QSTASH_SECRET else {}
    return qstash.message.publish_json(
        url=f"{APP_URL}/api/process",
        body={"task_id": task_id},
        headers=headers,
        delay=f"{int(delay_minutes)}m",
        retries=3,
    )


async def load_chats_or_error():
    try:
        return await get_chats(), None
    except Exception as exc:
        return None, str(exc)


async def render_set_picker(target, user_id):
    st=state(user_id); chats=st.get("chats",[]); selected=set(str(x) for x in st.get("set_chat_ids",[]))
    kb=InlineKeyboardBuilder()
    page=max(0,int(st.get("set_page",0))); page_size=40; start=page*page_size; end=min(len(chats),start+page_size)
    for i in range(start,end):
        c=chats[i]; cid=str(c["id"]); prefix="☑️" if cid in selected else "⬜"
        kb.button(text=f"{prefix} {chat_label(c)[:50]}", callback_data=f"setchat:{i}")
    if len(chats)>page_size:
        if page>0: kb.button(text="⬅️ Предыдущая страница",callback_data="set_page:-1")
        if end<len(chats): kb.button(text="➡️ Следующая страница",callback_data="set_page:1")
    kb.button(text="☑️ Выбрать все",callback_data="set_all")
    kb.button(text=f"💾 Сохранить ({len(selected)})",callback_data="save_set")
    kb.button(text="❌ Отмена",callback_data="cancel")
    kb.adjust(1)
    text=f"📦 <b>Состав объединения «{html.escape(st.get('set_name',''))}»</b>\n\nВыбрано: <b>{len(selected)}</b>\nВыберите группы и каналы. При рассылке пересечения будут удаляться автоматически."
    if isinstance(target,CallbackQuery): await target.message.edit_text(text,reply_markup=kb.as_markup(),parse_mode="HTML")
    else: await target.answer(text,reply_markup=kb.as_markup(),parse_mode="HTML")


@dp.callback_query(F.data.startswith("set_page:"))
async def set_page(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); delta=int(callback.data.split(":")[1]); total=len(st.get("chats",[])); pages=max(1,(total+39)//40); st["set_page"]=max(0,min(pages-1,int(st.get("set_page",0))+delta)); set_state(uid,st); await render_set_picker(callback,uid); await callback.answer()


@dp.callback_query(F.data.startswith("setchat:"))
async def set_chat_toggle(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); chats=st.get("chats",[])
    try: i=int(callback.data.split(":")[1]); cid=str(chats[i]["id"])
    except Exception: await callback.answer("Чат не найден.",show_alert=True); return
    selected=set(st.get("set_chat_ids",[])); selected.remove(cid) if cid in selected else selected.add(cid); st["set_chat_ids"]=list(selected); set_state(uid,st)
    await render_set_picker(callback,uid); await callback.answer()


@dp.callback_query(F.data == "set_all")
async def set_all(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); all_ids={str(c["id"]) for c in st.get("chats",[])}; selected=set(st.get("set_chat_ids",[]))
    st["set_chat_ids"]=[] if selected==all_ids else list(all_ids); set_state(uid,st); await render_set_picker(callback,uid); await callback.answer()


@dp.callback_query(F.data == "save_set")
async def save_set(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); ids=unique(st.get("set_chat_ids",[]))
    if not ids: await callback.answer("Выберите хотя бы один чат.",show_alert=True); return
    item = update_group_set(uid, st["set_id"], st["set_name"], ids) if st.get("set_id") else create_group_set(uid, st["set_name"], ids)
    clear_state(uid)
    await callback.message.edit_text(f"✅ <b>{'Объединение обновлено' if st.get('set_id') else 'Объединение создано'}</b>\n\n📦 {html.escape(item['name'])}\nЧатов: <b>{len(ids)}</b>",reply_markup=menu(),parse_mode="HTML"); await callback.answer()


async def render_set_picker(target, user_id):
    st=state(user_id); chats=st.get("chats",[]); selected=set(str(x) for x in st.get("set_chat_ids",[]))
    kb=InlineKeyboardBuilder()
    page=max(0,int(st.get("set_page",0))); page_size=40; start=page*page_size; end=min(len(chats),start+page_size)
    for i in range(start,end):
        c=chats[i]; cid=str(c["id"]); prefix="☑️" if cid in selected else "⬜"
        kb.button(text=f"{prefix} {chat_label(c)[:50]}", callback_data=f"setchat:{i}")
    if len(chats)>page_size:
        if page>0: kb.button(text="⬅️ Предыдущая страница",callback_data="set_page:-1")
        if end<len(chats): kb.button(text="➡️ Следующая страница",callback_data="set_page:1")
    kb.button(text="☑️ Выбрать все",callback_data="set_all")
    kb.button(text=f"💾 Сохранить ({len(selected)})",callback_data="save_set")
    kb.button(text="❌ Отмена",callback_data="cancel")
    kb.adjust(1)
    text=f"📦 <b>Состав объединения «{html.escape(st.get('set_name',''))}»</b>\n\nВыбрано: <b>{len(selected)}</b>\nВыберите группы и каналы. При рассылке пересечения будут удаляться автоматически."
    if isinstance(target,CallbackQuery): await target.message.edit_text(text,reply_markup=kb.as_markup(),parse_mode="HTML")
    else: await target.answer(text,reply_markup=kb.as_markup(),parse_mode="HTML")


@dp.callback_query(F.data.startswith("set_page:"))
async def set_page(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); delta=int(callback.data.split(":")[1]); total=len(st.get("chats",[])); pages=max(1,(total+39)//40); st["set_page"]=max(0,min(pages-1,int(st.get("set_page",0))+delta)); set_state(uid,st); await render_set_picker(callback,uid); await callback.answer()


@dp.callback_query(F.data.startswith("setchat:"))
async def set_chat_toggle(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); chats=st.get("chats",[])
    try: i=int(callback.data.split(":")[1]); cid=str(chats[i]["id"])
    except Exception: await callback.answer("Чат не найден.",show_alert=True); return
    selected=set(st.get("set_chat_ids",[])); selected.remove(cid) if cid in selected else selected.add(cid); st["set_chat_ids"]=list(selected); set_state(uid,st)
    await render_set_picker(callback,uid); await callback.answer()


@dp.callback_query(F.data == "set_all")
async def set_all(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); all_ids={str(c["id"]) for c in st.get("chats",[])}; selected=set(st.get("set_chat_ids",[]))
    st["set_chat_ids"]=[] if selected==all_ids else list(all_ids); set_state(uid,st); await render_set_picker(callback,uid); await callback.answer()


@dp.callback_query(F.data == "save_set")
async def save_set(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); ids=unique(st.get("set_chat_ids",[]))
    if not ids: await callback.answer("Выберите хотя бы один чат.",show_alert=True); return
    item = update_group_set(uid, st["set_id"], st["set_name"], ids) if st.get("set_id") else create_group_set(uid, st["set_name"], ids)
    clear_state(uid)
    await callback.message.edit_text(f"✅ <b>{'Объединение обновлено' if st.get('set_id') else 'Объединение создано'}</b>\n\n📦 {html.escape(item['name'])}\nЧатов: <b>{len(ids)}</b>",reply_markup=menu(),parse_mode="HTML"); await callback.answer()


async def render_chat_picker(target, user_id):
    st = state(user_id)
    chats = st.get("chats") or []
    selected = set(str(x) for x in st.get("selected_chat_ids", []))
    sets = get_user_group_sets(user_id)
    selected_sets = set(st.get("selected_set_ids", []))

    kb = InlineKeyboardBuilder()
    page=max(0,int(st.get("chat_page",0))); page_size=40; start=page*page_size; end=min(len(chats),start+page_size)
    for i in range(start,end):
        chat=chats[i]; cid=str(chat["id"])
        prefix = "☑️" if cid in selected else "⬜"
        kb.button(text=f"{prefix} {chat_label(chat)[:50]}", callback_data=f"chat:{i}")
    if len(chats)>page_size:
        if page>0: kb.button(text="⬅️ Предыдущая страница",callback_data="chat_page:-1")
        if end<len(chats): kb.button(text="➡️ Следующая страница",callback_data="chat_page:1")
    for item in sets[:30]:
        prefix = "☑️" if item["id"] in selected_sets else "📦"
        kb.button(text=f"{prefix} {item['name'][:42]}", callback_data=f"set:{item['id']}")
    kb.button(text="☑️ Выбрать все чаты", callback_data="all_chats")
    kb.button(text="📦 Управление объединениями", callback_data="group_sets")
    kb.button(text=f"✅ Продолжить ({len(resolve_union(chats, selected, selected_sets, sets))})", callback_data="continue")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1)
    text = (
        "📋 <b>Выбор чатов</b>\n\n"
        f"👥 Групп: <b>{sum(c['type']=='group' for c in chats)}</b>\n"
        f"📢 Каналов: <b>{sum(c['type']=='channel' for c in chats)}</b>\n"
        f"📦 Объединений: <b>{len(sets)}</b>\n"
        f"🎯 Получится уникальных чатов: <b>{len(resolve_union(chats, selected, selected_sets, sets))}</b>\n\n"
        "Можно выбирать отдельные чаты и несколько объединений одновременно.\n"
        "Пересечения автоматически убираются."
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@dp.message(CommandStart())
async def start(message: Message):
    clear_state(message.from_user.id)
    await message.answer("🤖 <b>Панель управления</b>\n\nВыберите действие:", reply_markup=menu(), parse_mode="HTML")


@dp.message(Command("s"))
async def show_menu(message: Message):
    clear_state(message.from_user.id)
    await message.answer("🤖 <b>Главное меню</b>", reply_markup=menu(), parse_mode="HTML")


@dp.callback_query(F.data == "new")
async def new(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Написать сообщение", callback_data="write")
    kb.button(text="📚 Выбрать шаблон", callback_data="templates_broadcast")
    kb.button(text="🔁 Последнее сообщение", callback_data="last")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1)
    await callback.message.edit_text("📨 <b>Новая рассылка</b>", reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "write")
async def write(callback: CallbackQuery):
    set_state(callback.from_user.id, {"state": "message"})
    await callback.message.edit_text("📝 <b>Введите сообщение</b>\n\nОтправьте текст для рассылки.", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "last")
async def last(callback: CallbackQuery):
    text = get_last_message(callback.from_user.id)
    if not text:
        await callback.answer("Последнего сообщения нет.", show_alert=True)
        return
    set_state(callback.from_user.id, {"state": "groups", "message": text, "selected_chat_ids": [], "selected_set_ids": []})
    chats, err = await load_chats_or_error()
    if err:
        await callback.message.edit_text(f"❌ <b>Не удалось получить чаты</b>\n\n<code>{html.escape(err)}</code>", reply_markup=menu(), parse_mode="HTML")
    else:
        st = state(callback.from_user.id); st["chats"] = chats; set_state(callback.from_user.id, st)
        await render_chat_picker(callback, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "templates")
async def templates(callback: CallbackQuery):
    items = get_user_templates(callback.from_user.id)
    kb = InlineKeyboardBuilder()
    for item in items[:50]:
        kb.button(text=f"📄 {item['name'][:45]}", callback_data=f"template:{item['id']}")
    kb.button(text="➕ Создать шаблон", callback_data="create_template")
    kb.button(text="◀️ Назад", callback_data="back")
    kb.adjust(1)
    text = "📚 <b>Мои шаблоны</b>\n\n" + ("Выберите шаблон:" if items else "Шаблонов пока нет.")
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "templates_broadcast")
async def templates_broadcast(callback: CallbackQuery):
    items = get_user_templates(callback.from_user.id)
    if not items:
        await callback.answer("Сначала создай шаблон.", show_alert=True); return
    kb = InlineKeyboardBuilder()
    for item in items[:50]:
        kb.button(text=f"📄 {item['name'][:45]}", callback_data=f"use:{item['id']}")
    kb.button(text="◀️ Назад", callback_data="new")
    kb.adjust(1)
    await callback.message.edit_text("📚 <b>Выбор шаблона</b>", reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "create_template")
async def create_template_start(callback: CallbackQuery):
    set_state(callback.from_user.id, {"state": "template_name"})
    await callback.message.edit_text("➕ <b>Название шаблона</b>", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("template:"))
async def open_template(callback: CallbackQuery):
    tid = callback.data.split(":", 1)[1]; item = get_template(tid); uid = callback.from_user.id
    if not item or int(item.get("user_id", -1)) != int(uid):
        await callback.answer("Нет доступа.", show_alert=True); return
    kb = InlineKeyboardBuilder()
    kb.button(text="📨 Использовать", callback_data=f"use:{tid}")
    kb.button(text="✏️ Изменить", callback_data=f"edit_template:{tid}")
    kb.button(text="🗑 Удалить", callback_data=f"delete_template:{tid}")
    kb.button(text="◀️ Назад", callback_data="templates")
    kb.adjust(1)
    await callback.message.edit_text(f"📄 <b>{html.escape(item['name'])}</b>\n\n{html.escape(item['message'])}", reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("use:"))
async def use_template(callback: CallbackQuery):
    tid = callback.data.split(":", 1)[1]; item = get_template(tid); uid = callback.from_user.id
    if not item or int(item.get("user_id", -1)) != int(uid):
        await callback.answer("Нет доступа.", show_alert=True); return
    chats, err = await load_chats_or_error()
    if err:
        await callback.message.edit_text(f"❌ <b>Не удалось получить чаты</b>\n\n<code>{html.escape(err)}</code>", reply_markup=menu(), parse_mode="HTML")
        await callback.answer(); return
    set_state(uid, {"state":"groups", "message":item["message"], "template_id":tid, "chats":chats, "selected_chat_ids":[], "selected_set_ids":[]})
    await render_chat_picker(callback, uid); await callback.answer()


@dp.callback_query(F.data.startswith("edit_template:"))
async def edit_template_start(callback: CallbackQuery):
    tid = callback.data.split(":", 1)[1]; item = get_template(tid); uid = callback.from_user.id
    if not item or int(item.get("user_id", -1)) != int(uid): await callback.answer("Нет доступа.", show_alert=True); return
    set_state(uid, {"state":"template_edit_name", "template_id":tid})
    await callback.message.edit_text("✏️ <b>Введите новое название</b>", parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data.startswith("delete_template:"))
async def delete_template_cb(callback: CallbackQuery):
    tid = callback.data.split(":", 1)[1]
    ok=delete_template(callback.from_user.id, tid)
    await templates(callback)
    if not ok: await callback.answer("Не найдено.", show_alert=True)


@dp.message()
async def text_handler(message: Message):
    uid = message.from_user.id; st = state(uid); text = (message.text or "").strip()
    if not st or not text: return
    mode = st.get("state")
    if mode == "message":
        if len(text) > 4096: await message.answer("❌ Максимум 4096 символов."); return
        save_last_message(uid, text)
        st.update({"state":"groups", "message":text, "selected_chat_ids":[], "selected_set_ids":[]})
        chats, err = await load_chats_or_error()
        if err:
            clear_state(uid); await message.answer(f"❌ <b>Не удалось получить список Telegram-чатов.</b>\n\nОшибка: <code>{html.escape(err)}</code>", reply_markup=menu(), parse_mode="HTML"); return
        st["chats"] = chats; set_state(uid, st); await render_chat_picker(message, uid); return
    if mode == "template_name":
        if len(text)>80: await message.answer("❌ Максимум 80 символов."); return
        st.update({"state":"template_message", "template_name":text}); set_state(uid, st); await message.answer("📝 <b>Введите текст шаблона</b>", parse_mode="HTML"); return
    if mode == "template_message":
        item=create_template(uid, st["template_name"], text); clear_state(uid); await message.answer(f"✅ Шаблон <b>{html.escape(item['name'])}</b> создан.", reply_markup=menu(), parse_mode="HTML"); return
    if mode == "template_edit_name":
        st.update({"state":"template_edit_message", "template_name":text}); set_state(uid, st); await message.answer("📝 <b>Введите новый текст</b>", parse_mode="HTML"); return
    if mode == "template_edit_message":
        item=update_template(uid, st["template_id"], st["template_name"], text); clear_state(uid)
        await message.answer("✅ Шаблон изменён." if item else "❌ Шаблон не найден.", reply_markup=menu()); return
    if mode == "set_name":
        if len(text) > 80: await message.answer("❌ Максимум 80 символов."); return
        chats, err = await load_chats_or_error()
        if err:
            clear_state(uid); await message.answer(f"❌ Не удалось получить чаты.\n\n<code>{html.escape(err)}</code>", reply_markup=menu(), parse_mode="HTML"); return
        st.update({"state":"set_chats", "set_name":text, "chats":chats, "set_chat_ids":[]}); set_state(uid, st)
        await render_set_picker(message, uid); return
    if mode == "rename_set":
        existing=get_group_set(st["set_id"]) or {}
        item=update_group_set(uid, st["set_id"], text, existing.get("groups", []))
        clear_state(uid); await message.answer("✅ Объединение переименовано." if item else "❌ Объединение не найдено.", reply_markup=menu()); return
    if mode == "set_name":
        if len(text) > 80: await message.answer("❌ Максимум 80 символов."); return
        chats, err = await load_chats_or_error()
        if err:
            clear_state(uid); await message.answer(f"❌ Не удалось получить чаты.\n\n<code>{html.escape(err)}</code>", reply_markup=menu(), parse_mode="HTML"); return
        st.update({"state":"set_chats", "set_name":text, "chats":chats, "set_chat_ids":[]}); set_state(uid, st)
        await render_set_picker(message, uid); return
    if mode == "rename_set":
        item=update_group_set(uid, st["set_id"], text, get_group_set(st["set_id"]).get("groups", []))
        clear_state(uid); await message.answer("✅ Объединение переименовано." if item else "❌ Объединение не найдено.", reply_markup=menu()); return
    if mode == "timer_interval":
        try: minutes=int(text); assert minutes>0
        except Exception: await message.answer("❌ Введите положительное число минут."); return
        st.update({"state":"timer_repeats", "interval":minutes}); set_state(uid, st); await message.answer("🔁 <b>Количество повторов</b>", parse_mode="HTML"); return
    if mode == "timer_repeats":
        try: repeats=int(text); assert repeats>0
        except Exception: await message.answer("❌ Введите положительное количество повторов."); return
        st.update({"state":"timer_confirm", "repeats":repeats}); set_state(uid, st)
        kb=InlineKeyboardBuilder(); kb.button(text="🚀 Создать таймер", callback_data="create_timer"); kb.button(text="❌ Отмена", callback_data="cancel"); kb.adjust(1)
        await message.answer(f"⏰ <b>Интервал:</b> {st['interval']} мин.\n<b>Повторов:</b> {repeats}", reply_markup=kb.as_markup(), parse_mode="HTML"); return


@dp.callback_query(F.data.startswith("chat_page:"))
async def chat_page(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); delta=int(callback.data.split(":")[1]); total=len(st.get("chats",[])); pages=max(1,(total+39)//40); st["chat_page"]=max(0,min(pages-1,int(st.get("chat_page",0))+delta)); set_state(uid,st); await render_chat_picker(callback,uid); await callback.answer()


@dp.callback_query(F.data.startswith("chat:"))
async def chat_toggle(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); chats=st.get("chats",[])
    try: i=int(callback.data.split(":")[1]); cid=str(chats[i]["id"])
    except Exception: await callback.answer("Чат не найден.", show_alert=True); return
    selected=set(st.get("selected_chat_ids",[])); selected.remove(cid) if cid in selected else selected.add(cid)
    st["selected_chat_ids"]=list(selected); set_state(uid,st); await render_chat_picker(callback,uid); await callback.answer()


@dp.callback_query(F.data.startswith("set:"))
async def set_toggle(callback: CallbackQuery):
    uid=callback.from_user.id; sid=callback.data.split(":",1)[1]; sets=get_user_group_sets(uid); st=state(uid)
    if not any(x["id"]==sid for x in sets): await callback.answer("Объединение не найдено.", show_alert=True); return
    selected=set(st.get("selected_set_ids",[])); selected.remove(sid) if sid in selected else selected.add(sid)
    st["selected_set_ids"]=list(selected); set_state(uid,st); await render_chat_picker(callback,uid); await callback.answer()


@dp.callback_query(F.data == "all_chats")
async def all_chats(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); chats=st.get("chats",[]); selected=set(st.get("selected_chat_ids",[]))
    all_ids={str(c["id"]) for c in chats}; st["selected_chat_ids"]=[] if selected==all_ids else list(all_ids); set_state(uid,st); await render_chat_picker(callback,uid); await callback.answer()


@dp.callback_query(F.data == "continue")
async def continue_selection(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); chats=st.get("chats",[]); sets=get_user_group_sets(uid)
    ids=resolve_union(chats, st.get("selected_chat_ids",[]), st.get("selected_set_ids",[]), sets)
    if not ids: await callback.answer("Выберите хотя бы один чат или объединение.", show_alert=True); return
    st["resolved_chat_ids"]=ids; st["state"]="broadcast_action"; set_state(uid,st)
    kb=InlineKeyboardBuilder(); kb.button(text="⚡ Отправить сейчас",callback_data="send"); kb.button(text="⏰ Задать таймер",callback_data="timer"); kb.button(text="◀️ Назад",callback_data="back_chats"); kb.adjust(1)
    await callback.message.edit_text(f"⚙️ <b>Рассылка</b>\n\nУникальных чатов: <b>{len(ids)}</b>",reply_markup=kb.as_markup(),parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data == "back_chats")
async def back_chats(callback: CallbackQuery): await render_chat_picker(callback, callback.from_user.id); await callback.answer()


@dp.callback_query(F.data == "send")
async def send_now(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); ids=st.get("resolved_chat_ids",[])
    if not st.get("message") or not ids: await callback.answer("Сессия устарела.",show_alert=True); return
    await callback.message.edit_text("📨 <b>Отправляю...</b>",parse_mode="HTML")
    try: result=await send_message(ids,st["message"])
    except Exception as exc:
        await callback.message.edit_text(f"❌ <b>Ошибка worker</b>\n\n<code>{html.escape(str(exc))}</code>",reply_markup=menu(),parse_mode="HTML"); clear_state(uid); await callback.answer(); return
    clear_state(uid)
    await callback.message.edit_text(f"✅ <b>Рассылка завершена</b>\n\nУспешно: <b>{result['success']}</b>\nОшибок: <b>{result['failed']}</b>",reply_markup=menu(),parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data == "timer")
async def timer_start(callback: CallbackQuery):
    st=state(callback.from_user.id); st["state"]="timer_interval"; set_state(callback.from_user.id,st)
    await callback.message.edit_text("⏰ <b>Интервал</b>\n\nЧерез сколько минут повторять?",parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data == "create_timer")
async def create_timer_cb(callback: CallbackQuery):
    uid=callback.from_user.id; st=state(uid); ids=st.get("resolved_chat_ids",[])
    if not ids: await callback.answer("Чаты не выбраны.",show_alert=True); return
    task=create_task(uid,st["message"],ids,int(st["interval"]),int(st["repeats"]))
    try: schedule_task(task["id"],task["interval_minutes"])
    except Exception as exc:
        task["status"]="error"; save_task(task); await callback.message.edit_text(f"❌ Не удалось запланировать: <code>{html.escape(str(exc))}</code>",reply_markup=menu(),parse_mode="HTML"); clear_state(uid); await callback.answer(); return
    clear_state(uid); await callback.message.edit_text("✅ <b>Таймер создан</b>\n\nПервая отправка произойдёт через заданный интервал.",reply_markup=menu(),parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data == "tasks")
async def tasks(callback: CallbackQuery):
    active=[x for x in get_user_tasks(callback.from_user.id) if x.get("status")=="active"]
    kb=InlineKeyboardBuilder(); text="📊 <b>Активные таймеры</b>\n\n"
    for t in active:
        text+=f"🔁 {t['completed_repeats']}/{t['total_repeats']} · ⏱ {t['interval_minutes']} мин.\n📝 {html.escape(t['message'][:80])}\n\n"; kb.button(text=f"🛑 Остановить {t['id'][:8]}",callback_data=f"stop:{t['id']}")
    if not active: text+="Активных таймеров нет."
    kb.button(text="◀️ Назад",callback_data="back"); kb.adjust(1)
    await callback.message.edit_text(text,reply_markup=kb.as_markup(),parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data.startswith("stop:"))
async def stop(callback: CallbackQuery):
    tid=callback.data.split(":",1)[1]; t=get_task(tid)
    if not t or int(t["user_id"])!=int(callback.from_user.id): await callback.answer("Нет доступа.",show_alert=True); return
    t["status"]="cancelled"; save_task(t); await callback.message.edit_text("🛑 <b>Таймер остановлен.</b>",reply_markup=menu(),parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data == "groups")
async def groups_menu(callback: CallbackQuery):
    uid=callback.from_user.id; sets=get_user_group_sets(uid); kb=InlineKeyboardBuilder()
    kb.button(text="📋 Список чатов",callback_data="groups_list"); kb.button(text="➕ Создать объединение",callback_data="create_set");
    for s in sets[:30]: kb.button(text=f"📦 {s['name'][:45]}",callback_data=f"open_set:{s['id']}")
    kb.button(text="◀️ Назад",callback_data="back"); kb.adjust(1)
    await callback.message.edit_text(f"📋 <b>Мои группы</b>\n\nОбъединений: <b>{len(sets)}</b>",reply_markup=kb.as_markup(),parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data == "groups_list")
async def groups_list(callback: CallbackQuery):
    chats,err=await load_chats_or_error()
    if err: text=f"❌ <b>Ошибка worker</b>\n\n<code>{html.escape(err)}</code>"
    else: text="📋 <b>Telegram-чаты</b>\n\n"+"\n".join(f"{'📢' if c['type']=='channel' else '👥'} {html.escape(c['title'])} · <code>{c['id']}</code>" for c in chats[:100])
    kb=InlineKeyboardBuilder(); kb.button(text="◀️ Назад",callback_data="groups"); kb.adjust(1)
    await callback.message.edit_text(text,reply_markup=kb.as_markup(),parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data == "create_set")
async def create_set_start(callback: CallbackQuery):
    set_state(callback.from_user.id,{"state":"set_name","set_chat_ids":[]}); await callback.message.edit_text("📦 <b>Название объединения</b>\n\nНапример: Школы",parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data.startswith("open_set:"))
async def open_set(callback: CallbackQuery):
    sid=callback.data.split(":",1)[1]; item=get_group_set(sid); uid=callback.from_user.id
    if not item or int(item.get("user_id",-1))!=int(uid): await callback.answer("Нет доступа.",show_alert=True); return
    kb=InlineKeyboardBuilder(); kb.button(text="✏️ Переименовать",callback_data=f"rename_set:{sid}"); kb.button(text="🧩 Изменить состав",callback_data=f"edit_set:{sid}"); kb.button(text="🗑 Удалить",callback_data=f"delete_set:{sid}"); kb.button(text="◀️ Назад",callback_data="groups"); kb.adjust(1)
    await callback.message.edit_text(f"📦 <b>{html.escape(item['name'])}</b>\n\nЧатов в объединении: <b>{len(item.get('groups',[]))}</b>",reply_markup=kb.as_markup(),parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data.startswith("edit_set:"))
async def edit_set(callback: CallbackQuery):
    uid=callback.from_user.id; sid=callback.data.split(":",1)[1]; item=get_group_set(sid)
    if not item or int(item.get("user_id",-1))!=int(uid): await callback.answer("Нет доступа.",show_alert=True); return
    chats,err=await load_chats_or_error()
    if err: await callback.message.edit_text(f"❌ Ошибка worker\n\n<code>{html.escape(err)}</code>",reply_markup=menu(),parse_mode="HTML"); await callback.answer(); return
    st={"state":"set_chats","set_id":sid,"set_name":item["name"],"chats":chats,"set_chat_ids":[str(x) for x in item.get("groups",[])]}
    set_state(uid,st); await render_set_picker(callback,uid); await callback.answer()


@dp.callback_query(F.data.startswith("rename_set:"))
async def rename_set(callback: CallbackQuery):
    sid=callback.data.split(":",1)[1]; set_state(callback.from_user.id,{"state":"rename_set","set_id":sid}); await callback.message.edit_text("✏️ <b>Введите новое название</b>",parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data.startswith("delete_set:"))
async def delete_set(callback: CallbackQuery):
    sid=callback.data.split(":",1)[1]; ok=delete_group_set(callback.from_user.id,sid); await callback.answer("Удалено." if ok else "Не найдено.",show_alert=not ok); await groups_menu(callback)


@dp.callback_query(F.data == "settings")
async def settings(callback: CallbackQuery):
    await callback.message.edit_text("⚙️ <b>Настройки</b>\n\nTelegram подключён через отдельный постоянный worker.",reply_markup=menu(),parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data == "back")
async def back(callback: CallbackQuery):
    clear_state(callback.from_user.id); await callback.message.edit_text("🤖 <b>Главное меню</b>",reply_markup=menu(),parse_mode="HTML"); await callback.answer()


@dp.callback_query(F.data == "cancel")
async def cancel(callback: CallbackQuery):
    clear_state(callback.from_user.id); await callback.message.edit_text("❌ <b>Операция отменена.</b>",reply_markup=menu(),parse_mode="HTML"); await callback.answer()


@app.post("/api/process")
async def process(request: Request):
    if QSTASH_SECRET and request.headers.get("X-Cron-Secret") != QSTASH_SECRET:
        return JSONResponse({"ok":False,"error":"Unauthorized"},status_code=401)
    try: data=await request.json()
    except Exception: return JSONResponse({"ok":False,"error":"Invalid JSON"},status_code=400)
    tid=data.get("task_id")
    task=get_task(tid) if tid else None
    if not task: return {"ok":True,"status":"task_not_found"}
    if task.get("status")!="active": return {"ok":True,"status":task.get("status")}
    if not acquire_task_lock(tid,120): return {"ok":True,"status":"already_processing"}
    try:
        result=await send_message(task["groups"],task["message"])
        task["completed_repeats"]=int(task.get("completed_repeats",0))+1
        if task["completed_repeats"]>=int(task["total_repeats"]):
            task["status"]="completed"; save_task(task)
            return {"ok":True,"status":"completed",**result}
        task["next_run"]=time.time()+int(task["interval_minutes"])*60; save_task(task)
        schedule_task(task["id"],task["interval_minutes"])
        return {"ok":True,"status":"scheduled_next",**result,"completed":task["completed_repeats"],"total":task["total_repeats"]}
    except Exception as exc:
        task["last_error"]=str(exc); save_task(task)
        return JSONResponse({"ok":False,"error":str(exc)},status_code=500)
    finally:
        release_task_lock(tid)


@app.post("/api/webhook")
async def webhook(request: Request):
    try:
        update=Update.model_validate(await request.json()); await dp.feed_update(bot,update); return {"ok":True}
    except Exception as exc:
        print("Webhook error:",repr(exc)); return JSONResponse({"ok":False,"error":str(exc)},status_code=500)


@app.get("/")
async def root(): return {"ok":True,"service":"telegram-broadcast-bot","architecture":"vercel + persistent telethon worker + redis"}
