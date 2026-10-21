import asyncio
import csv
import difflib
import html
import math
import os
import re
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Токен берётся из переменной окружения BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN", "8698693284:AAG0QfNmJUGA89ELYukCEE1198Yb0UtGe6s")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://myday-1mw4.onrender.com")
# Часовой пояс пользователя: «сегодня» для бота и напоминаний считается в нём, а не по часам сервера (на хостинге это UTC)
TZ_NAME = os.getenv("TZ_NAME", "Asia/Yekaterinburg")
if not BOT_TOKEN:
    raise SystemExit("Задайте переменную окружения BOT_TOKEN")
REPORTS_DIR = Path("reports")
DB_NAME = "daily.db"

try:
    LOCAL_TZ = ZoneInfo(TZ_NAME)
except Exception:  # нет базы часовых поясов — работаем по времени сервера
    LOCAL_TZ = None

MAX_PLAN_AHEAD_DAYS = 730  # дальше двух лет перенести задачу нельзя
HABIT_BACKFILL_DAYS = 30  # привычку можно отметить задним числом не глубже чем на месяц
PLAN_LIMIT = 10  # сколько задач и привычек показывать в плане в Telegram
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# =====================================================================
#  ОБЩЕЕ: время, ошибки, валидация
# =====================================================================
def now_local() -> datetime:
    return datetime.now(LOCAL_TZ).replace(tzinfo=None) if LOCAL_TZ else datetime.now()


def today_iso() -> str:
    return now_local().date().isoformat()


class DomainError(Exception):
    """Ожидаемая ошибка бизнес-правил: API отдаёт её как {"detail": ...}, бот показывает пользователю."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_day(value, field: str = "Дата") -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise DomainError(400, f"{field}: ожидается формат ГГГГ-ММ-ДД")


def client_today(value: str | None) -> str:
    """«Сегодня» от клиента: проверяем формат и что оно не уехало от серверного больше чем на двое суток."""
    server = now_local().date()
    if value is None:
        return server.isoformat()
    d = parse_day(value, "Текущая дата")
    if abs((d - server).days) > 2:
        raise DomainError(400, "Дата на устройстве сильно расходится с серверной — проверьте часы")
    return d.isoformat()


def parse_time(value, field: str = "Время") -> str:
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").strftime("%H:%M")
    except (TypeError, ValueError):
        raise DomainError(400, f"{field}: ожидается формат ЧЧ:ММ")


def check_positive(value, field: str = "Значение") -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise DomainError(400, f"{field}: нужно число")
    if not math.isfinite(v) or v <= 0 or v > 1e12:
        raise DomainError(400, f"{field}: нужно число больше нуля")
    return v


def check_number(value, field: str = "Значение") -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise DomainError(400, f"{field}: нужно число")
    if not math.isfinite(v) or abs(v) > 1e12:
        raise DomainError(400, f"{field}: слишком большое или некорректное число")
    return v


DAY_WORDS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
NOT_DAY_WORDS = {"завтрак", "завтрака", "завтраку", "завтраки", "завтраков"}  # похожи на «завтра», но это не дата
MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTH_FORMS = {**{w: i for i, w in enumerate(MONTHS_GEN, 1)}, **{w: i for i, w in enumerate(MONTHS_NOM, 1)}}


def fuzzy_day_word(t: str) -> int | None:
    """сегодня / завтра / послезавтра, в том числе с опечаткой («щавтра», «завтро»). Возвращает сдвиг в днях."""
    if not re.fullmatch(r"[а-яё]{4,}", t) or t in NOT_DAY_WORDS:
        return None
    if t in DAY_WORDS:
        return DAY_WORDS[t]
    close = difflib.get_close_matches(t, list(DAY_WORDS), n=1, cutoff=0.8)
    return DAY_WORDS[close[0]] if close else None


def month_from_word(w: str) -> int | None:
    """Номер месяца по слову: «ноября», «ноябр», «нояб», «нбря» (с опечаткой)."""
    w = w.lower()
    if w in MONTH_FORMS:
        return MONTH_FORMS[w]
    if len(w) >= 3:
        hits = {n for form, n in MONTH_FORMS.items() if form.startswith(w)}
        if len(hits) == 1:
            return hits.pop()
        if hits:
            return None
    close = difflib.get_close_matches(w, list(MONTH_FORMS), n=1, cutoff=0.75)
    return MONTH_FORMS[close[0]] if close else None


def build_date(d: int, mo: int, y: str | None, base: date) -> str:
    year = base.year if y is None else (int(y) if len(y) == 4 else 2000 + int(y))
    try:
        res = date(year, mo, d)
        if y is None and res < base:  # «25.01» в конце года — это январь следующего
            res = date(year + 1, mo, d)
    except ValueError:
        raise DomainError(400, "Такой даты не существует")
    return res.isoformat()


def match_date(text: str, today: str) -> str | None:
    """Дата из текста или None, если это не дата. Понимает: сегодня/завтра/послезавтра (с опечатками),
    25.10, 25 10, 25/10, 25.10.2026, 22 ноября, 22 ноябр, 22 нояб 2026, 2026-10-25."""
    t = (text or "").strip().lower().strip(" .,;!")
    if not t:
        return None
    base = date.fromisoformat(today)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return parse_day(t).isoformat()
    shift = fuzzy_day_word(t)
    if shift is not None:
        return (base + timedelta(days=shift)).isoformat()
    m = re.fullmatch(r"(\d{1,2})[./\s-]+(\d{1,2})(?:[./\s-]+(\d{2}|\d{4}))?", t)
    if m:
        return build_date(int(m[1]), int(m[2]), m[3], base)
    m = re.fullmatch(r"(\d{1,2})\s*([а-яё]{3,})\.?(?:\s+(\d{2}|\d{4})\s*(?:г|г\.|года)?)?", t)
    if m:
        mo = month_from_word(m[2])
        if mo:
            return build_date(int(m[1]), mo, m[3], base)
    return None


def parse_user_date(text: str, today: str) -> str:
    """Дата из сообщения пользователя в боте (см. match_date)."""
    iso = match_date(text, today)
    if iso:
        return iso
    raise DomainError(400, "Не понял дату. Примеры: 25.10, 22 ноября, завтра")


# --- быстрое добавление задачи одним сообщением: З "название" В/С/Н завтра | 22.11 | 22 ноября ---
PRIO_WORDS = {"в": 3, "высокий": 3, "важно": 3, "с": 2, "средний": 2, "н": 1, "низкий": 1}
QUICK_RE = r"^\s*з(?=[\s\"«“„])\s*\S"
QUICK_HELP = ('Формат: З "название" В/С/Н дата\n'
              'Приоритет: В — высокий, С — средний, Н — низкий. Дата: завтра, 22.11 или 22 ноября.\n'
              'Пример: З "Купить хлеб" В завтра')


def parse_priority(tok: str) -> int | None:
    t = tok.lower().strip(" .,;!")
    if t in PRIO_WORDS:
        return PRIO_WORDS[t]
    if len(t) >= 5 and re.fullmatch(r"[а-яё]+", t):
        close = difflib.get_close_matches(t, [w for w in PRIO_WORDS if len(w) > 1], n=1, cutoff=0.8)
        if close:
            return PRIO_WORDS[close[0]]
    return None


def parse_quick_add(text: str, today: str) -> tuple[str, int | None, str | None]:
    """«З "Купить хлеб" В 22 ноября» → (название, уровень 1-3 или None, плановая дата или None).
    Название можно писать без кавычек — тогда приоритет и дата берутся из конца сообщения."""
    body = re.sub(r"^\s*з\s*", "", text.strip(), count=1, flags=re.I)
    m = re.match(r"^[\"«“„](.+?)[\"»”“]\s*(.*)$", body, re.S)
    quoted = bool(m)
    title = m[1].strip() if m else ""
    rest = (m[2] if m else body).split()
    level: int | None = None
    planned: str | None = None
    keep = 0 if quoted else 1  # без кавычек в названии остаётся хотя бы одно слово
    while len(rest) > keep:
        if level is None:
            p = parse_priority(rest[-1])
            if p is not None:
                level = p
                rest.pop()
                continue
        if planned is None:
            found = None
            for n in (3, 2, 1):
                if len(rest) - n >= keep:
                    found = match_date(" ".join(rest[-n:]), today)
                    if found:
                        planned = found
                        del rest[-n:]
                        break
            if found:
                continue
        break
    if quoted:
        if rest:
            raise DomainError(400, f"Не понял «{' '.join(rest)}»")
    else:
        title = " ".join(rest)
    if not title:
        raise DomainError(400, "Не нашёл название задачи")
    return title, level, planned


def short_date(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.day} {MONTHS_GEN[d.month - 1][:3]}"


# --- FSM Состояния ---
class Creator(StatesGroup):
    task_name = State()
    goal_name = State()
    event_name = State()
    habit_name = State()


class Editor(StatesGroup):
    t_date = State()
    t_due = State()
    t_time = State()
    t_sub = State()
    t_note = State()
    g_tgt = State()
    g_unt = State()
    e_date = State()
    e_time = State()
    p_date = State()


# =====================================================================
#  БАЗА ДАННЫХ
# =====================================================================
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

        today = today_iso()
        await db.execute("UPDATE tasks SET created_on = ? WHERE created_on IS NULL", (today,))

        # Три разные даты задачи:
        #   created_on   — когда создана (никогда не меняется)
        #   planned_date — на какой день запланирована; перенос меняет только её
        #                  (у повторяющейся — первый день серии)
        #   due_date     — срок исполнения (у повторяющейся — последний день серии)
        # Старые start_date / until_date остаются в таблице как архив и больше не используются.
        if "planned_date" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN planned_date TEXT")
            await db.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT")
            # повторяющиеся: серия как была
            await db.execute(
                "UPDATE tasks SET planned_date = COALESCE(start_date, created_on, ?), due_date = until_date "
                "WHERE repeat_type != 'none'", (today,))
            # разовые выполненные: день, в который их раньше учитывал отчёт (срок, иначе начало)
            await db.execute(
                "UPDATE tasks SET planned_date = COALESCE(until_date, start_date, created_on, ?), due_date = until_date "
                "WHERE repeat_type = 'none' AND done_on IS NOT NULL", (today,))
            # разовые невыполненные: с просроченным сроком остаются на дне срока (будут «просрочены»),
            # остальные видны сегодня как и раньше
            await db.execute(
                "UPDATE tasks SET due_date = until_date, planned_date = CASE "
                "WHEN until_date IS NOT NULL AND until_date < ?1 THEN until_date "
                "ELSE MAX(COALESCE(start_date, created_on, ?1), ?1) END "
                "WHERE repeat_type = 'none' AND done_on IS NULL", (today,))

        await db.execute(
            """CREATE TABLE IF NOT EXISTS subtasks (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)""")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS completions (task_id INTEGER NOT NULL, day TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', PRIMARY KEY (task_id, day))""")
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute(
            "INSERT OR IGNORE INTO completions (task_id, day, category) SELECT id, done_on, category FROM tasks WHERE done_on IS NOT NULL AND done_on != '2000-01-01'")

        # Привычки: журнал выполнения по датам — из него считаются серии
        async with db.execute("PRAGMA table_info(habits)") as cur:
            hcols = {r[1] for r in await cur.fetchall()}
        if "created_on" not in hcols:
            await db.execute("ALTER TABLE habits ADD COLUMN created_on TEXT")
        await db.execute("UPDATE habits SET created_on = ? WHERE created_on IS NULL", (today,))
        await db.execute(
            """CREATE TABLE IF NOT EXISTS habit_logs (habit_id INTEGER NOT NULL, day TEXT NOT NULL, PRIMARY KEY (habit_id, day))""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_habit_logs_day ON habit_logs (day)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_completions_day ON completions (day)")
        await db.commit()


# =====================================================================
#  ЗАДАЧИ: правила дат
# =====================================================================
def planned_of(r) -> str:
    return r["planned_date"] or r["created_on"] or "2000-01-01"


def occurs(r, day: str) -> bool:
    """Запланирована ли задача на день (для отчётов). Разовая — только в плановый день."""
    start = planned_of(r)
    if day < start: return False
    if r["repeat_type"] == "none": return day == start
    if r["due_date"] and r["due_date"] < day: return False
    return not (r["repeat_type"] == "weekdays" and date.fromisoformat(day).weekday() >= 5)


def is_done(r, today: str) -> bool:
    return r["done_on"] == today if r["repeat_type"] != "none" else r["done_on"] is not None


def is_overdue(r, today: str) -> bool:
    """Просрочена по плану: разовая, не выполнена, плановый день уже прошёл. Только такие переносит «на сегодня»."""
    return r["repeat_type"] == "none" and r["done_on"] is None and planned_of(r) < today


def is_past_due(r, today: str) -> bool:
    """Срок исполнения истёк (не зависит от переносов плана)."""
    return r["repeat_type"] == "none" and r["done_on"] is None and bool(r["due_date"]) and r["due_date"] < today


def visible_today(r, today: str) -> bool:
    if planned_of(r) > today: return False
    if r["repeat_type"] != "none":
        if r["due_date"] and r["due_date"] < today: return False
        if r["repeat_type"] == "weekdays" and date.fromisoformat(today).weekday() >= 5: return False
    return True


# =====================================================================
#  ЗАДАЧИ: операции (общие для API и Telegram-бота)
# =====================================================================
class TaskCreate(BaseModel):
    text: str
    priority: bool = False
    level: int = 2
    repeat: str = "daily"
    planned: str | None = None  # плановая дата (у повторяющейся — начало серии)
    due: str | None = None  # срок (у повторяющейся — конец серии)
    start: str | None = None  # устаревшее имя planned
    until: str | None = None  # устаревшее имя due
    time: str | None = None
    remind: int | None = None
    category: str = ""
    note: str = ""
    subtasks: list[str] = []
    today: str | None = None  # «сегодня» на устройстве — для даты создания и плана по умолчанию


def clean_task(t: TaskCreate) -> dict:
    text = t.text.strip()
    if not text: raise DomainError(400, "Пустое название")
    if len(text) > 300: raise DomainError(400, "Название слишком длинное")
    if t.repeat not in ("none", "daily", "weekdays"): raise DomainError(400, "Неизвестный тип повтора")
    level = min(3, max(1, t.level))
    planned_raw, due_raw = t.planned or t.start, t.due or t.until
    planned = parse_day(planned_raw, "Плановая дата").isoformat() if planned_raw else None
    due = parse_day(due_raw, "Срок").isoformat() if due_raw else None
    time = parse_time(t.time) if t.time else None
    remind = t.remind if (time and t.remind is not None and t.remind >= 0) else None
    return dict(text=text, priority=int(level == 3), level=level, repeat=t.repeat, planned=planned, due=due,
                time=time, remind=remind, category=t.category.strip(), note=t.note.strip())


def check_series(repeat: str, planned: str, due: str | None):
    if repeat != "none" and due and due < planned:
        raise DomainError(400, "Дата окончания повтора раньше даты начала")


async def fetch_task(db, task_id: int):
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
        r = await cur.fetchone()
    if not r: raise DomainError(404, "Задача не найдена")
    return r


async def list_tasks(db, today: str, scope: str = "today") -> list[dict]:
    if scope not in ("today", "week", "all"): raise DomainError(400, "Неизвестный период")
    d0 = date.fromisoformat(today)
    week = [(d0 + timedelta(days=i)).isoformat() for i in range(7)]
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM tasks ORDER BY level DESC, id DESC") as cur: rows = await cur.fetchall()
    async with db.execute("SELECT id, task_id, text, done FROM subtasks ORDER BY id") as cur: subs = await cur.fetchall()
    by_task: dict[int, list] = {}
    for s in subs:
        by_task.setdefault(s["task_id"], []).append({"id": s["id"], "text": s["text"], "done": bool(s["done"])})
    tasks = []
    for r in rows:
        if scope == "week": show = visible_today(r, today) or any(occurs(r, d) for d in week)
        elif scope == "all": show = True
        else: show = visible_today(r, today)
        if not show: continue
        tasks.append({
            "id": r["id"], "text": r["text"], "priority": r["level"] == 3, "level": r["level"],
            "repeat": r["repeat_type"], "created": r["created_on"], "planned": planned_of(r), "due": r["due_date"],
            "time": r["due_time"], "remind": r["remind_min"], "category": r["category"], "note": r["note"],
            "completed": is_done(r, today),
            "overdue": is_overdue(r, today),
            "past_due": is_past_due(r, today),
            "subs": by_task.get(r["id"], []),
        })
    # Порядок для дневного планировщика: невыполненные вперёд; по дню плана (просроченные раньше сегодняшних);
    # внутри дня — сначала задачи со временем по возрастанию, затем без времени по важности
    def order(t):
        day = t["planned"] if (t["repeat"] == "none" and t["planned"] < today) else today
        return (t["completed"], day, t["time"] is None, t["time"] or "", -t["level"], -t["id"])
    tasks.sort(key=order)
    return tasks


async def create_task(db, task: TaskCreate) -> int:
    v = clean_task(task)
    today = client_today(task.today)
    planned = v["planned"] or today
    check_series(v["repeat"], planned, v["due"])
    cur = await db.execute(
        "INSERT INTO tasks (text, completed, priority, level, repeat_type, planned_date, due_date, due_time, remind_min, category, note, created_on) "
        "VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (v["text"], v["priority"], v["level"], v["repeat"], planned, v["due"], v["time"], v["remind"],
         v["category"], v["note"], today))
    for s in task.subtasks:
        if s.strip(): await db.execute("INSERT INTO subtasks (task_id, text) VALUES (?, ?)", (cur.lastrowid, s.strip()))
    await db.commit()
    return cur.lastrowid


async def update_task(db, task_id: int, task: TaskCreate):
    v = clean_task(task)
    old = await fetch_task(db, task_id)
    planned = v["planned"] or planned_of(old)  # пустая плановая дата в форме = оставить как была
    check_series(v["repeat"], planned, v["due"])
    # created_on, done_on и журнал выполнения здесь не трогаем
    await db.execute(
        "UPDATE tasks SET text=?, priority=?, level=?, repeat_type=?, planned_date=?, due_date=?, due_time=?, remind_min=?, category=?, note=?, reminded_on=NULL WHERE id=?",
        (v["text"], v["priority"], v["level"], v["repeat"], planned, v["due"], v["time"], v["remind"],
         v["category"], v["note"], task_id))
    await db.commit()


async def toggle_task_db(db, task_id: int, today: str):
    row = await fetch_task(db, task_id)
    rep, done_on, cat = row["repeat_type"], row["done_on"], row["category"]
    was_done = (done_on == today) if rep != "none" else (done_on is not None)
    if was_done:
        await db.execute("DELETE FROM completions WHERE task_id = ? AND day = ?", (task_id, done_on))
        await db.execute("UPDATE tasks SET done_on = NULL WHERE id = ?", (task_id,))
    else:
        await db.execute("INSERT OR IGNORE INTO completions (task_id, day, category) VALUES (?, ?, ?)",
                         (task_id, today, cat))
        await db.execute("UPDATE tasks SET done_on = ? WHERE id = ?", (today, task_id))
    await db.commit()


async def move_task_db(db, task_id: int, to: str, today: str) -> str:
    """Перенос = явная смена плановой даты одной разовой невыполненной задачи.
    Срок, дата создания и история выполнения (done_on, completions) не меняются."""
    row = await fetch_task(db, task_id)
    if row["repeat_type"] != "none":
        raise DomainError(400, "Повторяющуюся задачу перенести нельзя — она появляется по расписанию. "
                               "Измените дату начала в редакторе")
    if row["done_on"] is not None:
        raise DomainError(409, "Задача уже выполнена — переносить нечего")
    target = parse_day(to, "Дата переноса")
    base = date.fromisoformat(today)
    if target < base: raise DomainError(400, "Нельзя перенести задачу в прошлое")
    if (target - base).days > MAX_PLAN_AHEAD_DAYS: raise DomainError(400, "Слишком далёкая дата переноса")
    iso = target.isoformat()
    if iso != planned_of(row):
        await db.execute("UPDATE tasks SET planned_date = ?, reminded_on = NULL WHERE id = ?", (iso, task_id))
        await db.commit()
    return iso


async def carry_over_db(db, today: str) -> int:
    """Переносит на сегодня только действительно просроченные невыполненные разовые задачи."""
    cur = await db.execute(
        "UPDATE tasks SET planned_date = ?1, reminded_on = NULL WHERE repeat_type = 'none' AND done_on IS NULL "
        "AND COALESCE(planned_date, created_on, '2000-01-01') < ?1", (today,))
    await db.commit()
    return cur.rowcount


async def set_task_due(db, task_id: int, value: str | None):
    row = await fetch_task(db, task_id)
    due = parse_day(value, "Срок").isoformat() if value else None
    check_series(row["repeat_type"], planned_of(row), due)
    await db.execute("UPDATE tasks SET due_date = ?, reminded_on = NULL WHERE id = ?", (due, task_id))
    await db.commit()


# =====================================================================
#  ПРИВЫЧКИ: журнал и серии
# =====================================================================
def habit_streaks(days: set[str], today: str) -> tuple[int, int]:
    """(текущая серия, лучшая серия). Текущая не обрывается, пока сегодняшний день ещё не закончился."""
    t = date.fromisoformat(today)
    cur = 0
    d = t if today in days else t - timedelta(days=1)
    while d.isoformat() in days:
        cur += 1
        d -= timedelta(days=1)
    best = run = 0
    prev = None
    for s in sorted(days):
        d = date.fromisoformat(s)
        run = run + 1 if prev and (d - prev).days == 1 else 1
        best = max(best, run)
        prev = d
    return cur, best


def habit_view(row, days: set[str], today: str) -> dict:
    t = date.fromisoformat(today)
    cur, best = habit_streaks(days, today)
    return {
        "id": row["id"], "text": row["text"], "created": row["created_on"],
        "done_today": today in days, "streak": cur, "best": best, "total": len(days),
        "week": [{"date": (t - timedelta(days=i)).isoformat(), "done": (t - timedelta(days=i)).isoformat() in days}
                 for i in range(6, -1, -1)],
    }


async def list_habits(db, today: str) -> list[dict]:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT id, text, created_on FROM habits ORDER BY id DESC") as cur: rows = await cur.fetchall()
    async with db.execute("SELECT habit_id, day FROM habit_logs") as cur: logs = await cur.fetchall()
    by: dict[int, set] = {}
    for l in logs: by.setdefault(l["habit_id"], set()).add(l["day"])
    return [habit_view(r, by.get(r["id"], set()), today) for r in rows]


async def toggle_habit_db(db, habit_id: int, day: str, today: str) -> dict:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT id, text, created_on FROM habits WHERE id = ?", (habit_id,)) as cur:
        row = await cur.fetchone()
    if not row: raise DomainError(404, "Привычка не найдена")
    d, t = parse_day(day, "День"), date.fromisoformat(today)
    if d > t: raise DomainError(400, "Нельзя отметить привычку в будущем")
    if (t - d).days > HABIT_BACKFILL_DAYS:
        raise DomainError(400, f"Отметить можно не глубже чем на {HABIT_BACKFILL_DAYS} дней назад")
    iso = d.isoformat()
    async with db.execute("SELECT 1 FROM habit_logs WHERE habit_id = ? AND day = ?", (habit_id, iso)) as cur:
        exists = await cur.fetchone()
    if exists: await db.execute("DELETE FROM habit_logs WHERE habit_id = ? AND day = ?", (habit_id, iso))
    else: await db.execute("INSERT INTO habit_logs (habit_id, day) VALUES (?, ?)", (habit_id, iso))
    await db.commit()
    async with db.execute("SELECT day FROM habit_logs WHERE habit_id = ?", (habit_id,)) as cur:
        days = {r["day"] for r in await cur.fetchall()}
    return habit_view(row, days, today)


async def create_habit(db, text: str, today: str | None = None) -> int:
    text = (text or "").strip()
    if not text: raise DomainError(400, "Пустое название")
    if len(text) > 120: raise DomainError(400, "Название слишком длинное")
    cur = await db.execute("INSERT INTO habits (text, created_on) VALUES (?, ?)", (text, today or today_iso()))
    await db.commit()
    return cur.lastrowid


# =====================================================================
#  ЦЕЛИ: операции
# =====================================================================
async def fetch_goal(db, goal_id: int):
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT id, title, emoji, unit, target, current FROM goals WHERE id = ?", (goal_id,)) as cur:
        r = await cur.fetchone()
    if not r: raise DomainError(404, "Цель не найдена")
    return r


async def set_goal_target(db, goal_id: int, target) -> float:
    await fetch_goal(db, goal_id)
    v = check_positive(target, "Значение цели")
    await db.execute("UPDATE goals SET target = ? WHERE id = ?", (v, goal_id))
    await db.commit()
    return v


async def apply_goal_progress(db, goal_id: int, mode: str, value) -> dict:
    if mode not in ("add", "set"): raise DomainError(400, "Режим прогресса: add или set")
    await fetch_goal(db, goal_id)
    v = check_number(value, "Значение")
    if mode == "set":
        if v < 0: raise DomainError(400, "Прогресс не может быть отрицательным")
        await db.execute("UPDATE goals SET current = ? WHERE id = ?", (v, goal_id))
    else:
        await db.execute("UPDATE goals SET current = MAX(current + ?, 0) WHERE id = ?", (v, goal_id))
    await db.commit()
    return dict(await fetch_goal(db, goal_id))


# =====================================================================
#  TELEGRAM-БОТ
# =====================================================================
# --- КЛАВИАТУРЫ ---
def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 План")],
            [KeyboardButton(text="📝 Задачи"), KeyboardButton(text="🎯 Цели")],
            [KeyboardButton(text="📅 События"), KeyboardButton(text="🌱 Привычки")],
            [KeyboardButton(text="📊 Отчет активности")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие ниже..."
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
    repeating = r["repeat_type"] != "none"

    lines = [
        "🌿 <b>Задача добавлена</b>\n",
        "<blockquote>",
        f"<b>Название:</b> {html.escape(r['text'])}",
        f"<b>Уровень:</b> {lvl_map.get(r['level'], '🟡 Средний')}",
        f"<b>Повтор:</b> {rep_map.get(r['repeat_type'], 'Один раз')}",
        f"<b>{'Начало' if repeating else 'План'}:</b> {planned_of(r)}",
        f"<b>{'Конец' if repeating else 'Срок'}:</b> {r['due_date'] or 'Нет'}",
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
        [InlineKeyboardButton(text="📅 План", callback_data=f"t_date:{task_id}"),
         InlineKeyboardButton(text="⏳ Срок", callback_data=f"t_due:{task_id}")],
        [InlineKeyboardButton(text="⏰ Время", callback_data=f"t_time:{task_id}"),
         InlineKeyboardButton(text="➕ Подзадача", callback_data=f"t_sub:{task_id}")],
        [InlineKeyboardButton(text="📝 Заметка", callback_data=f"t_note:{task_id}"),
         InlineKeyboardButton(text="✅ Готово", callback_data="close_kb")]
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


# --- ПЛАН НА СЕГОДНЯ: посмотреть → выполнить → перенести ---
async def build_plan(today: str):
    async with aiosqlite.connect(DB_NAME) as db:
        tasks = await list_tasks(db, today, "today")
        habits = await list_habits(db, today)
    # просроченные — наверх, выполненные — вниз
    tasks.sort(key=lambda t: (t["completed"], not t["overdue"], t["time"] is None, t["time"] or "", -t["level"]))
    slipped = sum(1 for t in tasks if t["overdue"])
    shown, shown_habits = tasks[:PLAN_LIMIT], habits[:PLAN_LIMIT]
    d = date.fromisoformat(today)
    lines = [f"📋 <b>План на {d.day} {MONTHS_GEN[d.month - 1]}</b>", ""]
    rows = []
    if shown:
        for i, t in enumerate(shown, 1):
            mark = "✅" if t["completed"] else ("⚠️" if t["overdue"] else "⬜")
            name = html.escape(t["text"])
            extra = []
            if t["time"]: extra.append(t["time"])
            if t["overdue"]: extra.append(f"план был {short_date(t['planned'])}")
            if t["past_due"]: extra.append(f"срок {short_date(t['due'])} прошёл")
            if t["repeat"] != "none": extra.append("🔁")
            lines.append(f"{mark} {i}. {'<s>' + name + '</s>' if t['completed'] else name}"
                         + (f" — {' · '.join(extra)}" if extra else ""))
            if t["completed"]:
                rows.append([InlineKeyboardButton(text=f"↩️ {i}", callback_data=f"pl_done:{t['id']}")])
            else:
                row = [InlineKeyboardButton(text=f"✅ {i}", callback_data=f"pl_done:{t['id']}")]
                if t["repeat"] == "none":
                    row += [InlineKeyboardButton(text=f"➡️ {i} на завтра", callback_data=f"pl_tom:{t['id']}"),
                            InlineKeyboardButton(text=f"📅 {i}", callback_data=f"pl_date:{t['id']}")]
                rows.append(row)
        if len(tasks) > PLAN_LIMIT: lines.append(f"…и ещё {len(tasks) - PLAN_LIMIT} в приложении")
    else:
        lines.append("На сегодня задач нет 🌿")
    if shown_habits:
        lines += ["", "<b>Привычки</b>"]
        for h in shown_habits:
            streak = f" · 🔥 {h['streak']}" if h["streak"] else ""
            lines.append(f"{'✅' if h['done_today'] else '⬜'} {html.escape(h['text'])}{streak}")
            rows.append([InlineKeyboardButton(text=f"{'✅' if h['done_today'] else '⬜'} {h['text'][:28]}",
                                              callback_data=f"pl_hab:{h['id']}")])
    tail = []
    if slipped: tail.append(InlineKeyboardButton(text=f"⏩ Просроченные на сегодня ({slipped})", callback_data="pl_carry"))
    tail.append(InlineKeyboardButton(text="🔄 Обновить", callback_data="pl_ref"))
    rows.append(tail)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def refresh_plan(cq: CallbackQuery, note: str | None = None, alert: bool = False):
    text, kb = await build_plan(today_iso())
    try:
        await cq.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:  # «message is not modified» — план не изменился
        pass
    await cq.answer(note, show_alert=alert)


# --- БОТ: СТАРТ И СОЗДАНИЕ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('chat_id', ?)", (str(message.chat.id),))
        await db.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать планировать", callback_data="start_planning")]
    ])
    await message.answer("Добро пожаловать в трекер!", reply_markup=kb)


@dp.callback_query(F.data == "start_planning")
async def cq_start_planning(cq: CallbackQuery, state: FSMContext):
    await cq.message.delete()

    # Обновляем кнопку WebApp слева от поля ввода
    await bot.set_chat_menu_button(
        chat_id=cq.message.chat.id,
        menu_button=MenuButtonWebApp(text="Трекер", web_app=WebAppInfo(url=WEBAPP_URL))
    )

    await cq.message.answer('Выбери, что добавить 👇\n\nИли быстро: <code>З "Купить хлеб" В завтра</code>\n'
                            'В/С/Н — приоритет, дата: завтра, 22.11 или 22 ноября',
                            reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await cq.answer()


# 0. ПЛАН
@dp.message(Command("plan"))
@dp.message(F.text == "📋 План")
async def btn_plan(message: Message, state: FSMContext):
    await state.clear()
    text, kb = await build_plan(today_iso())
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# 1. ЗАДАЧИ
@dp.message(F.text == "📝 Задачи")
async def btn_tasks(message: Message, state: FSMContext):
    await state.set_state(Creator.task_name)
    await message.answer("Введи название задачи:")


@dp.message(Creator.task_name)
async def process_new_task(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Нужен текст — введи название задачи:")
        return
    today = today_iso()
    async with aiosqlite.connect(DB_NAME) as db:
        task_id = await create_task(db, TaskCreate(text=text, repeat="none", today=today))
    await state.clear()
    await message.answer(await get_task_text(task_id), reply_markup=task_kb(task_id), parse_mode=ParseMode.HTML)


# 2. ЦЕЛИ
@dp.message(F.text == "🎯 Цели")
async def btn_goals(message: Message, state: FSMContext):
    await state.set_state(Creator.goal_name)
    await message.answer("Введи название цели:")


@dp.message(Creator.goal_name)
async def process_new_goal(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("⚠️ Нужен текст — введи название цели:")
        return
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
    title = (message.text or "").strip()
    if not title:
        await message.answer("⚠️ Нужен текст — как назвать событие?")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("INSERT INTO events (date, title, color, emoji) VALUES (?, ?, '#3a86ff', '📌')",
                               (today_iso(), title))
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
    text = (message.text or "").strip()
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await create_habit(db, text)
    except DomainError as e:
        await message.answer(f"⚠️ {e.message}. Попробуй ещё раз:")
        return
    await state.clear()

    ans = f"🌱 <b>Привычка добавлена</b>\n\n<blockquote><b>Название:</b> {html.escape(text)}</blockquote>\n\nОтмечать её можно в «📋 План»."
    await message.answer(ans, reply_markup=habit_kb(), parse_mode=ParseMode.HTML)


# 5. ОТЧЕТ АКТИВНОСТИ
@dp.message(F.text == "📊 Отчет активности")
async def btn_report(message: Message, state: FSMContext):
    today = today_iso()
    d_from = (date.fromisoformat(today) - timedelta(days=6)).isoformat()

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


# --- ИНТЕРАКТИВНОЕ РЕДАКТИРОВАНИЕ (CALLBACKS) ---
@dp.callback_query(F.data == "close_kb")
async def cq_close_kb(cq: CallbackQuery, state: FSMContext):
    # При нажатии "Готово" просто убираем клавиатуру под карточкой
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.answer("Сохранено!")
    await state.clear()

    # Тихо, на фоне, страхуем кнопку WebApp
    await bot.set_chat_menu_button(
        chat_id=cq.message.chat.id,
        menu_button=MenuButtonWebApp(text="Трекер", web_app=WebAppInfo(url=WEBAPP_URL))
    )


# --- план: кнопки под сообщением ---
@dp.callback_query(F.data.startswith("pl_done:"))
async def cq_pl_done(cq: CallbackQuery):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await toggle_task_db(db, int(cq.data.split(":")[1]), today_iso())
    except DomainError as e:
        return await cq.answer(e.message, show_alert=True)
    await refresh_plan(cq)


@dp.callback_query(F.data.startswith("pl_tom:"))
async def cq_pl_tom(cq: CallbackQuery):
    today = today_iso()
    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await move_task_db(db, int(cq.data.split(":")[1]), tomorrow, today)
    except DomainError as e:
        return await cq.answer(e.message, show_alert=True)
    await refresh_plan(cq, "Перенесено на завтра")


@dp.callback_query(F.data.startswith("pl_hab:"))
async def cq_pl_hab(cq: CallbackQuery):
    today = today_iso()
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await toggle_habit_db(db, int(cq.data.split(":")[1]), today, today)
    except DomainError as e:
        return await cq.answer(e.message, show_alert=True)
    await refresh_plan(cq)


@dp.callback_query(F.data == "pl_carry")
async def cq_pl_carry(cq: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        n = await carry_over_db(db, today_iso())
    await refresh_plan(cq, f"Перенесено на сегодня: {n}" if n else "Просроченных задач нет")


@dp.callback_query(F.data == "pl_ref")
async def cq_pl_ref(cq: CallbackQuery):
    await refresh_plan(cq, "Обновлено")


@dp.callback_query(F.data.startswith("t_lvl:"))
async def cq_t_lvl(cq: CallbackQuery):
    task_id = int(cq.data.split(":")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT level FROM tasks WHERE id = ?", (task_id,)) as cur:
            r = await cur.fetchone()
            if r:
                lvl = r[0] % 3 + 1
                await db.execute("UPDATE tasks SET level = ?, priority = ? WHERE id = ?", (lvl, int(lvl == 3), task_id))
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


@dp.callback_query(F.data.startswith("pl_date:"))
async def cq_pl_date(cq: CallbackQuery, state: FSMContext): await init_edit_state(
    cq, state, Editor.p_date, "На какую дату перенести? Например: 25.10, 2026-10-25, завтра")


@dp.callback_query(F.data.startswith("t_date:"))
async def cq_t_date(cq: CallbackQuery, state: FSMContext): await init_edit_state(
    cq, state, Editor.t_date, "На какую дату запланировать? Например: 25.10, 2026-10-25, завтра")


@dp.callback_query(F.data.startswith("t_due:"))
async def cq_t_due(cq: CallbackQuery, state: FSMContext): await init_edit_state(
    cq, state, Editor.t_due, "Введите срок (например 25.10 или 2026-10-25), «нет» — убрать срок:")


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
                                                                                "Введите новое значение цели (число больше нуля):")


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
    except Exception:
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
    elif entity_type == 'plan':
        text, kb = await build_plan(today_iso())
        try:
            await bot.edit_message_text(text, chat_id=message.chat.id, message_id=data['msg_id'],
                                        reply_markup=kb, parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            pass
    await state.clear()


@dp.message(Editor.p_date)
async def process_p_date(message: Message, state: FSMContext):
    data = await state.get_data()
    today = today_iso()
    try:
        to = parse_user_date(message.text, today)
        async with aiosqlite.connect(DB_NAME) as db:
            await move_task_db(db, data['id'], to, today)
    except DomainError as e:
        # остаёмся в режиме ввода, чтобы можно было поправить дату; любая кнопка меню выходит из него
        await message.answer(f"⚠️ {e.message}. Введите дату ещё раз (например 25.10) или нажмите кнопку меню:")
        return
    await finish_edit(message, state, 'plan', data['id'])


@dp.message(Editor.t_date)
async def process_t_date(message: Message, state: FSMContext):
    data = await state.get_data()
    today = today_iso()
    try:
        to = parse_user_date(message.text, today)
        async with aiosqlite.connect(DB_NAME) as db:
            await move_task_db(db, data['id'], to, today)
    except DomainError as e:
        await message.answer(f"⚠️ {e.message}. Отмена.")
        await state.clear()
        return
    await finish_edit(message, state, 'task', data['id'])


@dp.message(Editor.t_due)
async def process_t_due(message: Message, state: FSMContext):
    data = await state.get_data()
    raw = (message.text or "").strip().lower()
    try:
        value = None if raw in ("нет", "-", "0") else parse_user_date(raw, today_iso())
        async with aiosqlite.connect(DB_NAME) as db:
            await set_task_due(db, data['id'], value)
    except DomainError as e:
        await message.answer(f"⚠️ {e.message}. Отмена.")
        await state.clear()
        return
    await finish_edit(message, state, 'task', data['id'])


@dp.message(Editor.t_time)
async def process_t_time(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        t = parse_time(message.text)
    except DomainError:
        await message.answer("⚠️ Неверный формат времени. Отмена.")
        await state.clear()
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tasks SET due_time = ?, remind_min = 0, reminded_on = NULL WHERE id = ?", (t, data['id']))
        await db.commit()
    await finish_edit(message, state, 'task', data['id'])


@dp.message(Editor.t_sub)
async def process_t_sub(message: Message, state: FSMContext):
    data = await state.get_data()
    text = (message.text or "").strip()
    if text:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO subtasks (task_id, text) VALUES (?, ?)", (data['id'], text))
            await db.commit()
    await finish_edit(message, state, 'task', data['id'])


@dp.message(Editor.t_note)
async def process_t_note(message: Message, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tasks SET note = ? WHERE id = ?", ((message.text or "").strip(), data['id']))
        await db.commit()
    await finish_edit(message, state, 'task', data['id'])


@dp.message(Editor.g_tgt)
async def process_g_tgt(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await set_goal_target(db, data['id'], (message.text or "").strip().replace(",", "."))
    except DomainError as e:
        await message.answer(f"⚠️ {e.message}. Отмена.")
        await state.clear()
        return
    await finish_edit(message, state, 'goal', data['id'])


@dp.message(Editor.g_unt)
async def process_g_unt(message: Message, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE goals SET unit = ? WHERE id = ?", ((message.text or "").strip(), data['id']))
        await db.commit()
    await finish_edit(message, state, 'goal', data['id'])


@dp.message(Editor.e_date)
async def process_e_date(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        d = parse_user_date(message.text, today_iso())
    except DomainError as e:
        await message.answer(f"⚠️ {e.message}. Отмена.")
        await state.clear()
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE events SET date = ? WHERE id = ?", (d, data['id']))
        await db.commit()
    await finish_edit(message, state, 'event', data['id'])


@dp.message(Editor.e_time)
async def process_e_time(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        t = parse_time(message.text)
    except DomainError:
        await message.answer("⚠️ Неверный формат времени. Отмена.")
        await state.clear()
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE events SET time = ? WHERE id = ?", (t, data['id']))
        await db.commit()
    await finish_edit(message, state, 'event', data['id'])


# Быстрое добавление: З "название" В/С/Н завтра. Регистрируется последним и только вне режимов ввода,
# чтобы не перехватывать ответы на вопросы бота
@dp.message(StateFilter(None), F.text.regexp(QUICK_RE, flags=re.I))
async def quick_add(message: Message):
    today = today_iso()
    try:
        title, level, planned = parse_quick_add(message.text, today)
        if planned and planned < today:
            raise DomainError(400, "Эта дата уже прошла")
        async with aiosqlite.connect(DB_NAME) as db:
            task_id = await create_task(
                db, TaskCreate(text=title, repeat="none", level=level or 2, planned=planned, today=today))
    except DomainError as e:
        await message.answer(f"⚠️ {html.escape(e.message)}\n\n{html.escape(QUICK_HELP)}", parse_mode=ParseMode.HTML)
        return
    await message.answer(await get_task_text(task_id), reply_markup=task_kb(task_id), parse_mode=ParseMode.HTML)


# =====================================================================
#  FASTAPI И СЕРВЕР
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
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


# Все ошибки API отдаются в одном формате {"detail": "понятный текст"} — клиент показывает его как есть
@app.exception_handler(DomainError)
async def domain_error_handler(request, exc: DomainError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError):
    first = (exc.errors() or [{}])[0]
    field = ".".join(str(x) for x in first.get("loc", ()) if x not in ("body", "query", "path")) or "запрос"
    return JSONResponse({"detail": f"Некорректное значение: {field}"}, status_code=422)


class MoveBody(BaseModel):
    to: str
    today: str | None = None


class SubCreate(BaseModel): text: str


class HabitCreate(BaseModel): text: str


class EventCreate(BaseModel): date: str; title: str; time: str | None = None; color: str = "#3a86ff"; emoji: str = "📌"


class GoalCreate(BaseModel): title: str; target: float; unit: str = ""; emoji: str = "🎯"; current: float = 0


class GoalProgress(BaseModel): mode: str; value: float


# --- задачи ---
@app.get("/api/tasks")
async def get_tasks(today: str | None = None, scope: str = "today"):
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        return {"tasks": await list_tasks(db, t, scope)}


@app.post("/api/tasks")
async def add_task(task: TaskCreate):
    async with aiosqlite.connect(DB_NAME) as db:
        return {"status": "ok", "id": await create_task(db, task)}


@app.put("/api/tasks/{task_id}")
async def update_task_api(task_id: int, task: TaskCreate):
    async with aiosqlite.connect(DB_NAME) as db:
        await update_task(db, task_id, task)
    return {"status": "ok"}


@app.post("/api/tasks/{task_id}/toggle")
async def toggle_task(task_id: int, today: str | None = None):
    async with aiosqlite.connect(DB_NAME) as db:
        await toggle_task_db(db, task_id, client_today(today))
    return {"status": "ok"}


@app.post("/api/tasks/{task_id}/duplicate")
async def duplicate_task(task_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        r = await fetch_task(db, task_id)
        today = today_iso()
        planned = planned_of(r)
        if r["repeat_type"] == "none" and planned < today: planned = today  # копия не должна рождаться просроченной
        cur = await db.execute(
            "INSERT INTO tasks (text, completed, priority, level, repeat_type, planned_date, due_date, due_time, remind_min, category, note, created_on) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["text"] + " (копия)", r["priority"], r["level"], r["repeat_type"], planned, r["due_date"],
             r["due_time"], r["remind_min"], r["category"], r["note"], today))
        await db.execute("INSERT INTO subtasks (task_id, text) SELECT ?, text FROM subtasks WHERE task_id = ?",
                         (cur.lastrowid, task_id))
        await db.commit()
    return {"status": "ok", "id": cur.lastrowid}


@app.post("/api/tasks/{task_id}/move")
async def move_task(task_id: int, body: MoveBody):
    async with aiosqlite.connect(DB_NAME) as db:
        planned = await move_task_db(db, task_id, body.to, client_today(body.today))
    return {"status": "ok", "planned": planned}


@app.post("/api/tasks/carry-over")
async def carry_over(today: str | None = None):
    async with aiosqlite.connect(DB_NAME) as db:
        moved = await carry_over_db(db, client_today(today))
    return {"status": "ok", "moved": moved}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await fetch_task(db, task_id)
        await db.execute("DELETE FROM subtasks WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))  # история в completions остаётся для отчётов
        await db.commit()
    return {"status": "ok"}


@app.post("/api/tasks/{task_id}/subtasks")
async def add_subtask(task_id: int, s: SubCreate):
    text = s.text.strip()
    if not text: raise DomainError(400, "Пустое название")
    async with aiosqlite.connect(DB_NAME) as db:
        await fetch_task(db, task_id)
        await db.execute("INSERT INTO subtasks (task_id, text) VALUES (?, ?)", (task_id, text))
        await db.commit()
    return {"status": "ok"}


@app.post("/api/subtasks/{sub_id}/toggle")
async def toggle_subtask(sub_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("UPDATE subtasks SET done = 1 - done WHERE id = ?", (sub_id,))
        if cur.rowcount == 0: raise DomainError(404, "Подзадача не найдена")
        await db.commit()
    return {"status": "ok"}


@app.delete("/api/subtasks/{sub_id}")
async def delete_subtask(sub_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("DELETE FROM subtasks WHERE id = ?", (sub_id,))
        if cur.rowcount == 0: raise DomainError(404, "Подзадача не найдена")
        await db.commit()
    return {"status": "ok"}


# --- отчёты ---
async def build_stats(d_from: str, d_to: str, today: str):
    start, end = parse_day(d_from, "Начало периода"), parse_day(d_to, "Конец периода")
    if end < start or (end - start).days > 400: raise DomainError(400, "Период от 1 до 400 дней")
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
    overdue = sum(1 for r in rows if is_overdue(r, today))
    return {"days": days, "done": sum(d["done"] for d in days), "planned": sum(d["planned"] for d in days),
            "active_days": sum(1 for d in days if d["done"] > 0), "overdue": overdue,
            "categories": [{"name": k, "done": v} for k, v in cats.most_common()]}


@app.get("/api/stats")
async def get_stats(today: str | None = None, date_from: str = Query(alias="from"), date_to: str = Query(alias="to")):
    return await build_stats(date_from, date_to, client_today(today))


@app.get("/api/stats/export")
async def export_stats(today: str | None = None, date_from: str = Query(alias="from"), date_to: str = Query(alias="to")):
    st = await build_stats(date_from, date_to, client_today(today))
    path = REPORTS_DIR / f"report_{date_from}_{date_to}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Дата", "Запланировано", "Выполнено"])
        w.writerows([d["date"], d["planned"], d["done"]] for d in st["days"])
        w.writerow([])
        w.writerow(["Категория", "Выполнено"])
        w.writerows([c["name"] or "Без категории", c["done"]] for c in st["categories"])
    return {"status": "ok", "path": str(path.resolve())}


# --- напоминания ---
async def check_reminders():
    now = now_local()
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
            day = planned_of(r) if one_off else today
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


# --- привычки ---
@app.get("/api/habits")
async def get_habits(today: str | None = None):
    async with aiosqlite.connect(DB_NAME) as db:
        return {"habits": await list_habits(db, client_today(today))}


@app.post("/api/habits")
async def add_habit_api(habit: HabitCreate):
    async with aiosqlite.connect(DB_NAME) as db:
        hid = await create_habit(db, habit.text)
    return {"status": "ok", "id": hid}


@app.post("/api/habits/{habit_id}/toggle")
async def toggle_habit(habit_id: int, day: str | None = None, today: str | None = None):
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        return {"status": "ok", "habit": await toggle_habit_db(db, habit_id, day or t, t)}


@app.delete("/api/habits/{habit_id}")
async def delete_habit(habit_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
        if cur.rowcount == 0: raise DomainError(404, "Привычка не найдена")
        await db.execute("DELETE FROM habit_logs WHERE habit_id = ?", (habit_id,))
        await db.commit()
    return {"status": "ok"}


# --- события ---
@app.get("/api/events")
async def get_events(month: str):
    if not re.fullmatch(r"\d{4}-\d{2}", month): raise DomainError(400, "Месяц: ожидается формат ГГГГ-ММ")
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT id, date, title, time, color, emoji FROM events WHERE date LIKE ? ORDER BY date, COALESCE(time, '99:99'), id",
                (month + "-%",)) as cursor:
            return {"events": [dict(row) for row in await cursor.fetchall()]}


@app.post("/api/events")
async def add_event_api(ev: EventCreate):
    title = ev.title.strip()
    if not title: raise DomainError(400, "Пустое название")
    day = parse_day(ev.date, "Дата").isoformat()
    time = parse_time(ev.time) if ev.time else None
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO events (date, title, time, color, emoji) VALUES (?, ?, ?, ?, ?)",
                         (day, title, time, ev.color, ev.emoji))
        await db.commit()
    return {"status": "ok"}


@app.delete("/api/events/{event_id}")
async def delete_event_api(event_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        if cur.rowcount == 0: raise DomainError(404, "Событие не найдено")
        await db.commit()
    return {"status": "ok"}


# --- цели ---
@app.get("/api/goals")
async def get_goals_api():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, title, emoji, unit, target, current FROM goals ORDER BY id DESC") as cursor: return {
            "goals": [dict(row) for row in await cursor.fetchall()]}


@app.post("/api/goals")
async def add_goal_api(goal: GoalCreate):
    title = goal.title.strip()
    if not title: raise DomainError(400, "Пустое название")
    target = check_positive(goal.target, "Значение цели")
    current = check_number(goal.current, "Начальное значение")
    if current < 0: raise DomainError(400, "Начальное значение не может быть отрицательным")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO goals (title, emoji, unit, target, current) VALUES (?, ?, ?, ?, ?)",
                         (title, goal.emoji, goal.unit.strip(), target, current))
        await db.commit()
    return {"status": "ok"}


@app.post("/api/goals/{goal_id}/progress")
async def update_goal_progress(goal_id: int, p: GoalProgress):
    async with aiosqlite.connect(DB_NAME) as db:
        goal = await apply_goal_progress(db, goal_id, p.mode, p.value)
    return {"status": "ok", "goal": goal}


@app.delete("/api/goals/{goal_id}")
async def delete_goal_api(goal_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        if cur.rowcount == 0: raise DomainError(404, "Цель не найдена")
        await db.commit()
    return {"status": "ok"}


@app.get("/")
async def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)