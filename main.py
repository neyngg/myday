import asyncio
import csv
import html
import os
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    CallbackQuery,
    WebAppInfo,
)
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Токен берётся из переменной окружения BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN", "8698693284:AAG0QfNmJUGA89ELYukCEE1198Yb0UtGe6s")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://myday-1mw4.onrender.com")
if not BOT_TOKEN:
    raise SystemExit("Задайте переменную окружения BOT_TOKEN")
REPORTS_DIR = Path("reports")
DB_NAME = "daily.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- FSM Состояния ---
class Creator(StatesGroup):
    task_name = State()
    goal_name = State()
    event_name = State()
    habit_name = State()


class Editor(StatesGroup):
    t_date = State()
    t_time = State()
    t_sub = State()
    t_note = State()
    g_tgt = State()
    g_unt = State()
    e_date = State()
    e_time = State()


async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, completed BOOLEAN NOT NULL DEFAULT 0, priority BOOLEAN NOT NULL DEFAULT 0)""")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS habits (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL)""")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, title TEXT NOT NULL, time TEXT, color TEXT NOT NULL DEFAULT '#3a86ff', emoji TEXT NOT NULL DEFAULT '📌')""")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS goals (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT '🎯', unit TEXT NOT NULL DEFAULT '', target REAL NOT NULL, current REAL NOT NULL DEFAULT 0)""")

        async with db.execute("PRAGMA table_info(tasks)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        if "done_on" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN done_on TEXT")
            await db.execute("UPDATE tasks SET done_on = '2000-01-01' WHERE completed = 1")
        if "repeat_type" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN repeat_type TEXT NOT NULL DEFAULT 'none'")
        if "until_date" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN until_date TEXT")
        if "start_date" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN start_date TEXT")

        new_cols = {
            "level": "INTEGER NOT NULL DEFAULT 2",
            "due_time": "TEXT",
            "remind_min": "INTEGER",
            "category": "TEXT NOT NULL DEFAULT ''",
            "note": "TEXT NOT NULL DEFAULT ''",
            "created_on": "TEXT",
            "reminded_on": "TEXT",
        }
        for name, ddl in new_cols.items():
            if name not in cols:
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
                if name == "level": await db.execute("UPDATE tasks SET level = 3 WHERE priority = 1")

        await db.execute("UPDATE tasks SET created_on = ? WHERE created_on IS NULL", (date.today().isoformat(),))
        await db.execute(
            """CREATE TABLE IF NOT EXISTS subtasks (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)""")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS completions (task_id INTEGER NOT NULL, day TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', PRIMARY KEY (task_id, day))""")
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute(
            "INSERT OR IGNORE INTO completions (task_id, day, category) SELECT id, done_on, category FROM tasks WHERE done_on IS NOT NULL AND done_on != '2000-01-01'")
        await db.commit()


# --- КЛАВИАТУРЫ И ЦЕНТРАЛЬНАЯ ЛОГИКА ---
def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Задачи"), KeyboardButton(text="🎯 Цели")],
            [KeyboardButton(text="📅 События"), KeyboardButton(text="🌱 Привычки")],
            [KeyboardButton(text="📊 Отчет активности")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие ниже..."
    )


async def go_home(chat_id: int, state: FSMContext):
    """Сбрасывает состояние, обновляет кнопку WebApp и показывает главное сообщение."""
    await state.clear()

    # Принудительно обновляем кнопку приложения для текущего чата
    await bot.set_chat_menu_button(
        chat_id=chat_id,
        menu_button=MenuButtonWebApp(text="Трекер", web_app=WebAppInfo(url=WEBAPP_URL))
    )

    await bot.send_message(
        chat_id=chat_id,
        text="Что делаем дальше?👇",
        reply_markup=main_menu_kb()
    )


# --- ФОРМАТИРОВАНИЕ КАРТОЧЕК (СОВРЕМЕННЫЙ HTML) ---
async def get_task_text(task_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur: r = await cur.fetchone()
        if not r: return "Задача удалена"
        async with db.execute("SELECT text FROM subtasks WHERE task_id = ?",
                              (task_id,)) as cur: subs = await cur.fetchall()

    rep_map = {"none": "Один раз", "daily": "Ежедневно", "weekdays": "По будням"}
    lvl_map = {1: "🟢 Низкий", 2: "🟡 Средний", 3: "🔴 Высокий"}

    lines = [
        "🌿 <b>Задача добавлена</b>\n",
        "<blockquote>",
        f"<b>Название:</b> {html.escape(r['text'])}",
        f"<b>Уровень:</b> {lvl_map.get(r['level'], '🟡 Средний')}",
        f"<b>Повтор:</b> {rep_map.get(r['repeat_type'], 'Один раз')}",
        f"<b>Срок:</b> {r['until_date'] or 'Нет'}",
        f"<b>Время:</b> {r['due_time'] or 'Нет'}"
    ]
    if r['note']: lines.append(f"<b>Заметка:</b> <i>{html.escape(r['note'])}</i>")
    lines.append("</blockquote>")
    if subs: lines.append("\n<b>Подзадачи:</b>\n" + "\n".join(f"▫️ {html.escape(s['text'])}" for s in subs))
    return "\n".join(lines)


def task_kb(task_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡️ Уровень", callback_data=f"t_lvl:{task_id}"),
         InlineKeyboardButton(text="🔁 Повтор", callback_data=f"t_rep:{task_id}")],
        [InlineKeyboardButton(text="📅 Срок", callback_data=f"t_date:{task_id}"),
         InlineKeyboardButton(text="⏰ Время", callback_data=f"t_time:{task_id}")],
        [InlineKeyboardButton(text="➕ Подзадача", callback_data=f"t_sub:{task_id}"),
         InlineKeyboardButton(text="📝 Заметка", callback_data=f"t_note:{task_id}")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="close_kb")]
    ])


async def get_goal_text(goal_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)) as cur: r = await cur.fetchone()
    if not r: return "Цель удалена"
    return (
        f"{r['emoji']} <b>Цель сохранена</b>\n\n"
        f"<blockquote>"
        f"<b>Название:</b> {html.escape(r['title'])}\n"
        f"<b>Значение:</b> {r['target']} {html.escape(r['unit'])}\n"
        f"<b>Накоплено:</b> {r['current']} {html.escape(r['unit'])}\n"
        f"</blockquote>"
    )


def goal_kb(goal_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Значение цели", callback_data=f"g_tgt:{goal_id}"),
         InlineKeyboardButton(text="⚖️ Ед. измерения", callback_data=f"g_unt:{goal_id}")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="close_kb")]
    ])


async def get_event_text(event_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM events WHERE id = ?", (event_id,)) as cur: r = await cur.fetchone()
    if not r: return "Событие удалено"
    return (
        f"{r['emoji']} <b>Событие сохранено</b>\n\n"
        f"<blockquote>"
        f"<b>Название:</b> {html.escape(r['title'])}\n"
        f"<b>Дата:</b> {r['date']}\n"
        f"<b>Время:</b> {r['time'] or 'Весь день'}\n"
        f"</blockquote>"
    )


def event_kb(event_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Дата", callback_data=f"e_date:{event_id}"),
         InlineKeyboardButton(text="⏰ Время", callback_data=f"e_time:{event_id}")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="close_kb")]
    ])


def habit_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Готово", callback_data="close_kb")]])


# --- БОТ: ГЛАВНОЕ МЕНЮ И СОЗДАНИЕ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('chat_id', ?)", (str(message.chat.id),))
        await db.commit()
    await message.answer("Привет! Твой трекер обновлен 🚀")
    await go_home(message.chat.id, state)


# 1. ЗАДАЧИ
@dp.message(F.text == "📝 Задачи")
async def btn_tasks(message: Message, state: FSMContext):
    await state.set_state(Creator.task_name)
    await message.answer("Введи название задачи:")


@dp.message(Creator.task_name)
async def process_new_task(message: Message, state: FSMContext):
    text = message.text.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "INSERT INTO tasks (text, completed, priority, level, repeat_type, start_date, created_on) VALUES (?, 0, 0, 2, 'none', ?, ?)",
            (text, date.today().isoformat(), date.today().isoformat())
        )
        await db.commit()
        task_id = cur.lastrowid
    await state.clear()
    await message.answer(await get_task_text(task_id), reply_markup=task_kb(task_id), parse_mode=ParseMode.HTML)


# 2. ЦЕЛИ
@dp.message(F.text == "🎯 Цели")
async def btn_goals(message: Message, state: FSMContext):
    await state.set_state(Creator.goal_name)
    await message.answer("Введи название цели:")


@dp.message(Creator.goal_name)
async def process_new_goal(message: Message, state: FSMContext):
    title = message.text.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("INSERT INTO goals (title, emoji, unit, target, current) VALUES (?, '🎯', '', 100, 0)",
                               (title,))
        await db.commit()
        goal_id = cur.lastrowid
    await state.clear()
    await message.answer(await get_goal_text(goal_id), reply_markup=goal_kb(goal_id), parse_mode=ParseMode.HTML)


# 3. СОБЫТИЯ
@dp.message(F.text == "📅 События")
async def btn_events(message: Message, state: FSMContext):
    await state.set_state(Creator.event_name)
    await message.answer("Как назвать событие?")


@dp.message(Creator.event_name)
async def process_new_event(message: Message, state: FSMContext):
    title = message.text.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("INSERT INTO events (date, title, color, emoji) VALUES (?, ?, '#3a86ff', '📌')",
                               (date.today().isoformat(), title))
        await db.commit()
        event_id = cur.lastrowid
    await state.clear()
    await message.answer(await get_event_text(event_id), reply_markup=event_kb(event_id), parse_mode=ParseMode.HTML)


# 4. ПРИВЫЧКИ
@dp.message(F.text == "🌱 Привычки")
async def btn_habits(message: Message, state: FSMContext):
    await state.set_state(Creator.habit_name)
    await message.answer("Как назвать привычку?")


@dp.message(Creator.habit_name)
async def process_new_habit(message: Message, state: FSMContext):
    text = message.text.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO habits (text) VALUES (?)", (text,))
        await db.commit()
    await state.clear()

    ans = f"🌱 <b>Привычка добавлена</b>\n\n<blockquote><b>Название:</b> {html.escape(text)}</blockquote>"
    await message.answer(ans, reply_markup=habit_kb(), parse_mode=ParseMode.HTML)


# 5. ОТЧЕТ АКТИВНОСТИ
@dp.message(F.text == "📊 Отчет активности")
async def btn_report(message: Message, state: FSMContext):
    today = date.today().isoformat()
    d_from = (date.today() - timedelta(days=6)).isoformat()

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks") as cur:
            rows = await cur.fetchall()
        async with db.execute("SELECT day FROM completions WHERE day BETWEEN ? AND ?", (d_from, today)) as cur:
            comps = await cur.fetchall()

    done_by = Counter(c["day"] for c in comps)
    total_done = sum(done_by.values())

    planned = 0
    cur_day = date.fromisoformat(d_from)
    end_day = date.fromisoformat(today)
    while cur_day <= end_day:
        k = cur_day.isoformat()
        planned += sum(1 for r in rows if occurs(r, k))
        cur_day += timedelta(days=1)

    pct = int((total_done / planned * 100)) if planned > 0 else 0

    text = (
        f"📊 <b>Отчет за последние 7 дней</b>\n\n"
        f"<blockquote>"
        f"<b>Запланировано:</b> {planned}\n"
        f"<b>Выполнено:</b> {total_done}\n"
        f"<b>Успеваемость:</b> {pct}%\n"
        f"</blockquote>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)
    await go_home(message.chat.id, state)


# --- ИНТЕРАКТИВНОЕ РЕДАКТИРОВАНИЕ (CALLBACKS) ---
@dp.callback_query(F.data == "close_kb")
async def cq_close_kb(cq: CallbackQuery, state: FSMContext):
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.answer("Сохранено!")
    await go_home(cq.message.chat.id, state)


@dp.callback_query(F.data.startswith("t_lvl:"))
async def cq_t_lvl(cq: CallbackQuery):
    task_id = int(cq.data.split(":")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT level FROM tasks WHERE id = ?", (task_id,)) as cur:
            r = await cur.fetchone()
            if r:
                lvl = r[0] % 3 + 1
                await db.execute("UPDATE tasks SET level = ? WHERE id = ?", (lvl, task_id))
                await db.commit()
    await cq.message.edit_text(await get_task_text(task_id), reply_markup=task_kb(task_id), parse_mode=ParseMode.HTML)
    await cq.answer()


@dp.callback_query(F.data.startswith("t_rep:"))
async def cq_t_rep(cq: CallbackQuery):
    task_id = int(cq.data.split(":")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT repeat_type FROM tasks WHERE id = ?", (task_id,)) as cur:
            r = await cur.fetchone()
            if r:
                nxt = {"none": "daily", "daily": "weekdays", "weekdays": "none"}[r[0]]
                await db.execute("UPDATE tasks SET repeat_type = ? WHERE id = ?", (nxt, task_id))
                await db.commit()
    await cq.message.edit_text(await get_task_text(task_id), reply_markup=task_kb(task_id), parse_mode=ParseMode.HTML)
    await cq.answer()


async def init_edit_state(cq: CallbackQuery, state: FSMContext, next_state, prompt_text: str):
    item_id = int(cq.data.split(":")[1])
    await state.set_state(next_state)
    msg = await cq.message.answer(f"<i>{prompt_text}</i>", parse_mode=ParseMode.HTML)
    await state.update_data(id=item_id, msg_id=cq.message.message_id, prompt_id=msg.message_id)
    await cq.answer()


@dp.callback_query(F.data.startswith("t_date:"))
async def cq_t_date(cq: CallbackQuery, state: FSMContext): await init_edit_state(cq, state, Editor.t_date,
                                                                                 "Введите срок (ГГГГ-ММ-ДД):")


@dp.callback_query(F.data.startswith("t_time:"))
async def cq_t_time(cq: CallbackQuery, state: FSMContext): await init_edit_state(cq, state, Editor.t_time,
                                                                                 "Введите время (ЧЧ:ММ):")


@dp.callback_query(F.data.startswith("t_sub:"))
async def cq_t_sub(cq: CallbackQuery, state: FSMContext): await init_edit_state(cq, state, Editor.t_sub,
                                                                                "Введите текст подзадачи:")


@dp.callback_query(F.data.startswith("t_note:"))
async def cq_t_note(cq: CallbackQuery, state: FSMContext): await init_edit_state(cq, state, Editor.t_note,
                                                                                 "Введите текст заметки:")


@dp.callback_query(F.data.startswith("g_tgt:"))
async def cq_g_tgt(cq: CallbackQuery, state: FSMContext): await init_edit_state(cq, state, Editor.g_tgt,
                                                                                "Введите новое значение цели (число):")


@dp.callback_query(F.data.startswith("g_unt:"))
async def cq_g_unt(cq: CallbackQuery, state: FSMContext): await init_edit_state(cq, state, Editor.g_unt,
                                                                                "Введите единицу измерения:")


@dp.callback_query(F.data.startswith("e_date:"))
async def cq_e_date(cq: CallbackQuery, state: FSMContext): await init_edit_state(cq, state, Editor.e_date,
                                                                                 "Введите новую дату (ГГГГ-ММ-ДД):")


@dp.callback_query(F.data.startswith("e_time:"))
async def cq_e_time(cq: CallbackQuery, state: FSMContext): await init_edit_state(cq, state, Editor.e_time,
                                                                                 "Введите время (ЧЧ:ММ):")


async def finish_edit(message: Message, state: FSMContext, entity_type: str, item_id: int):
    data = await state.get_data()
    try:
        await message.delete()
        if 'prompt_id' in data: await bot.delete_message(chat_id=message.chat.id, message_id=data['prompt_id'])
    except:
        pass

    if entity_type == 'task':
        await bot.edit_message_text(await get_task_text(item_id), chat_id=message.chat.id, message_id=data['msg_id'],
                                    reply_markup=task_kb(item_id), parse_mode=ParseMode.HTML)
    elif entity_type == 'goal':
        await bot.edit_message_text(await get_goal_text(item_id), chat_id=message.chat.id, message_id=data['msg_id'],
                                    reply_markup=goal_kb(item_id), parse_mode=ParseMode.HTML)
    elif entity_type == 'event':
        await bot.edit_message_text(await get_event_text(item_id), chat_id=message.chat.id, message_id=data['msg_id'],
                                    reply_markup=event_kb(item_id), parse_mode=ParseMode.HTML)
    await state.clear()


@dp.message(Editor.t_date)
async def process_t_date(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        d = datetime.fromisoformat(message.text.strip()).date().isoformat()
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE tasks SET until_date = ? WHERE id = ?", (d, data['id']))
            await db.commit()
        await finish_edit(message, state, 'task', data['id'])
    except ValueError:
        await message.answer("⚠️ Неверный формат даты. Отмена.")
        await state.clear()


@dp.message(Editor.t_time)
async def process_t_time(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        t = datetime.strptime(message.text.strip(), "%H:%M").time().strftime("%H:%M")
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE tasks SET due_time = ?, remind_min = 0 WHERE id = ?", (t, data['id']))
            await db.commit()
        await finish_edit(message, state, 'task', data['id'])
    except ValueError:
        await message.answer("⚠️ Неверный формат времени. Отмена.")
        await state.clear()


@dp.message(Editor.t_sub)
async def process_t_sub(message: Message, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO subtasks (task_id, text) VALUES (?, ?)", (data['id'], message.text.strip()))
        await db.commit()
    await finish_edit(message, state, 'task', data['id'])


@dp.message(Editor.t_note)
async def process_t_note(message: Message, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tasks SET note = ? WHERE id = ?", (message.text.strip(), data['id']))
        await db.commit()
    await finish_edit(message, state, 'task', data['id'])


@dp.message(Editor.g_tgt)
async def process_g_tgt(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        val = float(message.text.strip())
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE goals SET target = ? WHERE id = ?", (val, data['id']))
            await db.commit()
        await finish_edit(message, state, 'goal', data['id'])
    except ValueError:
        await message.answer("⚠️ Введите число. Отмена.")
        await state.clear()


@dp.message(Editor.g_unt)
async def process_g_unt(message: Message, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE goals SET unit = ? WHERE id = ?", (message.text.strip(), data['id']))
        await db.commit()
    await finish_edit(message, state, 'goal', data['id'])


@dp.message(Editor.e_date)
async def process_e_date(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        d = datetime.fromisoformat(message.text.strip()).date().isoformat()
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE events SET date = ? WHERE id = ?", (d, data['id']))
            await db.commit()
        await finish_edit(message, state, 'event', data['id'])
    except ValueError:
        await message.answer("⚠️ Неверный формат даты. Отмена.")
        await state.clear()


@dp.message(Editor.e_time)
async def process_e_time(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        t = datetime.strptime(message.text.strip(), "%H:%M").time().strftime("%H:%M")
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE events SET time = ? WHERE id = ?", (t, data['id']))
            await db.commit()
        await finish_edit(message, state, 'event', data['id'])
    except ValueError:
        await message.answer("⚠️ Неверный формат времени. Отмена.")
        await state.clear()


# --- FASTAPI И СЕРВЕР ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Фоновая установка кнопки при старте сервера
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Трекер", web_app=WebAppInfo(url=WEBAPP_URL)))
    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    reminder_task = asyncio.create_task(reminder_loop())
    yield
    polling_task.cancel()
    reminder_task.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)
Path("static").mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


class TaskCreate(BaseModel):
    text: str;
    priority: bool = False;
    level: int = 2;
    repeat: str = "daily"
    start: str | None = None;
    until: str | None = None;
    time: str | None = None
    remind: int | None = None;
    category: str = "";
    note: str = "";
    subtasks: list[str] = []


class MoveBody(BaseModel): to: str


class SubCreate(BaseModel): text: str


class HabitCreate(BaseModel): text: str


class EventCreate(BaseModel): date: str; title: str; time: str | None = None; color: str = "#3a86ff"; emoji: str = "📌"


class GoalCreate(BaseModel): title: str; target: float; unit: str = ""; emoji: str = "🎯"; current: float = 0


class GoalProgress(BaseModel): mode: str; value: float


def occurs(r, day: str) -> bool:
    rep, start = r["repeat_type"], r["start_date"] or r["created_on"] or "2000-01-01"
    if day < start: return False
    if rep == "none": return day == (r["until_date"] or start)
    if r["until_date"] and r["until_date"] < day: return False
    return not (rep == "weekdays" and date.fromisoformat(day).weekday() >= 5)


def is_done(r, today: str) -> bool: return r["done_on"] == today if r["repeat_type"] != "none" else r[
                                                                                                        "done_on"] is not None


def visible_today(r, today: str) -> bool:
    if r["start_date"] and r["start_date"] > today: return False
    if r["repeat_type"] != "none":
        if r["until_date"] and r["until_date"] < today: return False
        if r["repeat_type"] == "weekdays" and date.fromisoformat(today).weekday() >= 5: return False
    return True


def clean(t: TaskCreate):
    repeat = t.repeat if t.repeat in ("none", "daily", "weekdays") else "none"
    level = min(3, max(1, t.level))
    start, until = t.start or None, t.until or None
    if start and until and start > until: start, until = until, start
    time = t.time or None
    remind = t.remind if (time and t.remind is not None and t.remind >= 0) else None
    return (t.text.strip(), int(level == 3), level, repeat, start, until, time, remind, t.category.strip(),
            t.note.strip())


@app.get("/api/tasks")
async def get_tasks(today: str, scope: str = "today"):
    d0 = date.fromisoformat(today)
    week = [(d0 + timedelta(days=i)).isoformat() for i in range(7)]
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks ORDER BY level DESC, id DESC") as cur: rows = await cur.fetchall()
        async with db.execute(
            "SELECT id, task_id, text, done FROM subtasks ORDER BY id") as cur: subs = await cur.fetchall()
    by_task: dict[int, list] = {}
    for s in subs: by_task.setdefault(s["task_id"], []).append(
        {"id": s["id"], "text": s["text"], "done": bool(s["done"])})
    tasks = []
    for r in rows:
        if scope == "week" and not (visible_today(r, today) or any(occurs(r, d) for d in week)):
            continue
        elif scope != "all" and not visible_today(r, today):
            continue
        done = is_done(r, today)
        tasks.append({
            "id": r["id"], "text": r["text"], "priority": r["level"] == 3, "level": r["level"],
            "repeat": r["repeat_type"], "start": r["start_date"], "until": r["until_date"],
            "time": r["due_time"], "remind": r["remind_min"], "category": r["category"], "note": r["note"],
            "completed": done,
            "overdue": r["repeat_type"] == "none" and bool(r["until_date"] and r["until_date"] < today and not done),
            "subs": by_task.get(r["id"], []),
        })
    tasks.sort(key=lambda t: t["completed"])
    return {"tasks": tasks}


@app.post("/api/tasks")
async def add_task(task: TaskCreate):
    v = clean(task)
    if not v[0]: raise HTTPException(400, "Пустое название")
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "INSERT INTO tasks (text, completed, priority, level, repeat_type, start_date, until_date, due_time, remind_min, category, note, created_on) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*v, date.today().isoformat()))
        for s in task.subtasks:
            if s.strip(): await db.execute("INSERT INTO subtasks (task_id, text) VALUES (?, ?)",
                                           (cur.lastrowid, s.strip()))
        await db.commit()
    return {"status": "ok", "id": cur.lastrowid}


@app.put("/api/tasks/{task_id}")
async def update_task(task_id: int, task: TaskCreate):
    v = clean(task)
    if not v[0]: raise HTTPException(400, "Пустое название")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE tasks SET text=?, priority=?, level=?, repeat_type=?, start_date=?, until_date=?, due_time=?, remind_min=?, category=?, note=?, reminded_on=NULL WHERE id=?",
            (*v, task_id))
        await db.commit()
    return {"status": "ok"}


@app.post("/api/tasks/{task_id}/toggle")
async def toggle_task(task_id: int, today: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT repeat_type, done_on, category FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
        if row:
            rep, done_on, cat = row
            was_done = (done_on == today) if rep != "none" else (done_on is not None)
            if was_done:
                await db.execute("DELETE FROM completions WHERE task_id = ? AND day = ?", (task_id, done_on))
                await db.execute("UPDATE tasks SET done_on = NULL WHERE id = ?", (task_id,))
            else:
                await db.execute("INSERT OR IGNORE INTO completions (task_id, day, category) VALUES (?, ?, ?)",
                                 (task_id, today, cat))
                await db.execute("UPDATE tasks SET done_on = ? WHERE id = ?", (today, task_id))
            await db.commit()
    return {"status": "ok"}


@app.post("/api/tasks/{task_id}/duplicate")
async def duplicate_task(task_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur: r = await cur.fetchone()
        if not r: raise HTTPException(404, "Задача не найдена")
        cur = await db.execute(
            "INSERT INTO tasks (text, completed, priority, level, repeat_type, start_date, until_date, due_time, remind_min, category, note, created_on) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["text"] + " (копия)", r["priority"], r["level"], r["repeat_type"], r["start_date"], r["until_date"],
             r["due_time"], r["remind_min"], r["category"], r["note"], date.today().isoformat()))
        await db.execute("INSERT INTO subtasks (task_id, text) SELECT ?, text FROM subtasks WHERE task_id = ?",
                         (cur.lastrowid, task_id))
        await db.commit()
    return {"status": "ok", "id": cur.lastrowid}


@app.post("/api/tasks/{task_id}/move")
async def move_task(task_id: int, body: MoveBody):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT repeat_type FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
        if not row: raise HTTPException(404, "Задача не найдена")
        if row[0] == "none":
            await db.execute("UPDATE tasks SET until_date = ?, start_date = NULL, reminded_on = NULL WHERE id = ?",
                             (body.to, task_id))
        else:
            await db.execute("UPDATE tasks SET start_date = ?, reminded_on = NULL WHERE id = ?", (body.to, task_id))
        await db.commit()
    return {"status": "ok"}


@app.post("/api/tasks/carry-over")
async def carry_over(today: str):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "UPDATE tasks SET until_date = ?, reminded_on = NULL WHERE repeat_type = 'none' AND done_on IS NULL AND until_date IS NOT NULL AND until_date < ?",
            (today, today))
        await db.commit()
    return {"status": "ok", "moved": cur.rowcount}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM subtasks WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
    return {"status": "ok"}


@app.post("/api/tasks/{task_id}/subtasks")
async def add_subtask(task_id: int, s: SubCreate):
    if not s.text.strip(): raise HTTPException(400, "Пустое название")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO subtasks (task_id, text) VALUES (?, ?)", (task_id, s.text.strip()))
        await db.commit()
    return {"status": "ok"}


@app.post("/api/subtasks/{sub_id}/toggle")
async def toggle_subtask(sub_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE subtasks SET done = 1 - done WHERE id = ?", (sub_id,))
        await db.commit()
    return {"status": "ok"}


@app.delete("/api/subtasks/{sub_id}")
async def delete_subtask(sub_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM subtasks WHERE id = ?", (sub_id,))
        await db.commit()
    return {"status": "ok"}


async def build_stats(d_from: str, d_to: str, today: str):
    try:
        start, end = date.fromisoformat(d_from), date.fromisoformat(d_to)
    except ValueError:
        raise HTTPException(400, "Неверная дата")
    if end < start or (end - start).days > 400: raise HTTPException(400, "Период от 1 до 400 дней")
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks") as cur: rows = await cur.fetchall()
        async with db.execute("SELECT day, category FROM completions WHERE day BETWEEN ? AND ?",
                              (d_from, d_to)) as cur: comps = await cur.fetchall()
    done_by = Counter(c["day"] for c in comps)
    cats = Counter(c["category"] for c in comps)
    days, cur_day = [], start
    while cur_day <= end:
        k = cur_day.isoformat()
        days.append({"date": k, "planned": sum(1 for r in rows if occurs(r, k)), "done": done_by.get(k, 0)})
        cur_day += timedelta(days=1)
    overdue = sum(1 for r in rows if
                  r["repeat_type"] == "none" and r["until_date"] and r["until_date"] < today and r["done_on"] is None)
    return {"days": days, "done": sum(d["done"] for d in days), "planned": sum(d["planned"] for d in days),
            "active_days": sum(1 for d in days if d["done"] > 0), "overdue": overdue,
            "categories": [{"name": k, "done": v} for k, v in cats.most_common()]}


@app.get("/api/stats")
async def get_stats(today: str, date_from: str = Query(alias="from"), date_to: str = Query(alias="to")):
    return await build_stats(date_from, date_to, today)


@app.get("/api/stats/export")
async def export_stats(today: str, date_from: str = Query(alias="from"), date_to: str = Query(alias="to")):
    st = await build_stats(date_from, date_to, today)
    path = REPORTS_DIR / f"report_{date_from}_{date_to}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Дата", "Запланировано", "Выполнено"])
        w.writerows([d["date"], d["planned"], d["done"]] for d in st["days"])
        w.writerow([])
        w.writerow(["Категория", "Выполнено"])
        w.writerows([c["name"] or "Без категории", c["done"]] for c in st["categories"])
    return {"status": "ok", "path": str(path.resolve())}


async def check_reminders():
    now = datetime.now()
    today = now.date().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT value FROM settings WHERE key = 'chat_id'") as cur:
            chat = await cur.fetchone()
        if not chat: return
        async with db.execute("SELECT * FROM tasks WHERE due_time IS NOT NULL AND remind_min IS NOT NULL") as cur:
            rows = await cur.fetchall()
        for r in rows:
            one_off = r["repeat_type"] == "none"
            day = (r["until_date"] or r["start_date"] or r["created_on"]) if one_off else today
            if not one_off and not occurs(r, today): continue
            if r["reminded_on"] == day or is_done(r, today): continue
            due = datetime.fromisoformat(f"{day}T{r['due_time']}")
            if due - timedelta(minutes=r["remind_min"]) <= now < due + timedelta(hours=1):
                await bot.send_message(int(chat["value"]),
                                       f"⏰ <b>Напоминание:</b> {html.escape(r['text'])} — в {r['due_time']}",
                                       parse_mode=ParseMode.HTML)
                await db.execute("UPDATE tasks SET reminded_on = ? WHERE id = ?", (day, r["id"]))
                await db.commit()


async def reminder_loop():
    while True:
        try:
            await check_reminders()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("Ошибка напоминаний:", e)
        await asyncio.sleep(30)


@app.get("/api/habits")
async def get_habits():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, text FROM habits ORDER BY id DESC") as cursor: return {
            "habits": [dict(row) for row in await cursor.fetchall()]}


@app.post("/api/habits")
async def add_habit_api(habit: HabitCreate):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO habits (text) VALUES (?)", (habit.text,))
        await db.commit()
    return {"status": "ok"}


@app.delete("/api/habits/{habit_id}")
async def delete_habit(habit_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
        await db.commit()
    return {"status": "ok"}


@app.get("/api/events")
async def get_events(month: str):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT id, date, title, time, color, emoji FROM events WHERE date LIKE ? ORDER BY date, COALESCE(time, '99:99'), id",
                (month + "-%",)) as cursor:
            return {"events": [dict(row) for row in await cursor.fetchall()]}


@app.post("/api/events")
async def add_event_api(ev: EventCreate):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO events (date, title, time, color, emoji) VALUES (?, ?, ?, ?, ?)",
                         (ev.date, ev.title, ev.time or None, ev.color, ev.emoji))
        await db.commit()
    return {"status": "ok"}


@app.delete("/api/events/{event_id}")
async def delete_event_api(event_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        await db.commit()
    return {"status": "ok"}


@app.get("/api/goals")
async def get_goals_api():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, title, emoji, unit, target, current FROM goals ORDER BY id DESC") as cursor: return {
            "goals": [dict(row) for row in await cursor.fetchall()]}


@app.post("/api/goals")
async def add_goal_api(goal: GoalCreate):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO goals (title, emoji, unit, target, current) VALUES (?, ?, ?, ?, ?)",
                         (goal.title, goal.emoji, goal.unit.strip(), goal.target, max(goal.current, 0)))
        await db.commit()
    return {"status": "ok"}


@app.post("/api/goals/{goal_id}/progress")
async def update_goal_progress(goal_id: int, p: GoalProgress):
    async with aiosqlite.connect(DB_NAME) as db:
        if p.mode == "set":
            await db.execute("UPDATE goals SET current = MAX(?, 0) WHERE id = ?", (p.value, goal_id))
        else:
            await db.execute("UPDATE goals SET current = MAX(current + ?, 0) WHERE id = ?", (p.value, goal_id))
        await db.commit()
    return {"status": "ok"}


@app.delete("/api/goals/{goal_id}")
async def delete_goal_api(goal_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        await db.commit()
    return {"status": "ok"}


@app.get("/")
async def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)