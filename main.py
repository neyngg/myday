import asyncio
import csv
import difflib
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import shutil
import time as _time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import aiosqlite
import uvicorn
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Токен берётся из переменной окружения BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN", "8241348815:AAH3AVgUQqpZCrPpXrJbaO5ddakAwNtheLQ")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://mydailyday.duckdns.org")
# Часовой пояс пользователя: «сегодня» для бота и напоминаний считается в нём, а не по часам сервера (на хостинге это UTC)
TZ_NAME = os.getenv("TZ_NAME", "Asia/Yekaterinburg")
if not BOT_TOKEN:
    raise SystemExit("Задайте переменную окружения BOT_TOKEN (токен из @BotFather)")
# База и отчёты: по умолчанию рядом с main.py, путь можно задать переменными DB_PATH и REPORTS_PATH
REPORTS_DIR = Path(os.getenv("REPORTS_PATH", "reports"))
DB_NAME = os.getenv("DB_PATH", "daily.db")

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
    habit_time = State()  # время напоминания
    habit_freq = State()  # частота: каждый день / дни недели


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
    r_date = State()  # перенос задачи из напоминания на выбранную дату
    n_time = State()  # время утреннего плана / вечернего итога / сводки просроченных
    n_tz = State()  # часовой пояс
    n_offs = State()  # за сколько минут до задачи напоминать


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
        await migrate_notifications(db)
        await claim_legacy_data(db)


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
    """Просрочена: разовая, не выполнена, плановый день прошёл И (если задан срок) срок тоже истёк.
    Задача с периодом «с 27 по 29» в середине периода не просрочена. Только такие переносит «на сегодня»."""
    if r["repeat_type"] != "none" or r["done_on"] is not None or planned_of(r) >= today: return False
    return not r["due_date"] or r["due_date"] < today


def is_past_due(r, today: str) -> bool:
    """Срок исполнения истёк (не зависит от переносов плана)."""
    return (r["repeat_type"] == "none" and r["done_on"] is None and bool(r["due_date"]) and r["due_date"] < today
            and not is_overdue(r, today))  # если задача уже «просрочена», второй пометкой не дублируем


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
    # remind_min: NULL — напоминать по настройкам пользователя (за 30 и 10 минут), -1 — не напоминать об этой задаче.
    # Прежние значения (0/10/60/1440) больше не используются и при сохранении сбрасываются в NULL.
    remind = -1 if t.remind == -1 else None
    return dict(text=text, priority=int(level == 3), level=level, repeat=t.repeat, planned=planned, due=due,
                time=time, remind=remind, category=t.category.strip(), note=t.note.strip())


def check_series(repeat: str, planned: str, due: str | None):
    if repeat != "none" and due and due < planned:
        raise DomainError(400, "Дата окончания повтора раньше даты начала")


async def fetch_task(db, task_id: int, uid: int):
    """Задача пользователя uid. Чужая или несуществующая — одинаково «не найдена» (не раскрываем, что она есть)."""
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid)) as cur:
        r = await cur.fetchone()
    if not r: raise DomainError(404, "Задача не найдена")
    return r


async def list_tasks(db, today: str, scope: str, uid: int) -> list[dict]:
    if scope not in ("today", "week", "all", "done"): raise DomainError(400, "Неизвестный период")
    d0 = date.fromisoformat(today)
    week = [(d0 + timedelta(days=i)).isoformat() for i in range(7)]
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM tasks WHERE user_id = ? ORDER BY level DESC, id DESC", (uid,)) as cur: rows = await cur.fetchall()
    async with db.execute("SELECT id, task_id, text, done FROM subtasks WHERE task_id IN (SELECT id FROM tasks WHERE user_id = ?) ORDER BY id",
                          (uid,)) as cur: subs = await cur.fetchall()
    by_task: dict[int, list] = {}
    for s in subs:
        by_task.setdefault(s["task_id"], []).append({"id": s["id"], "text": s["text"], "done": bool(s["done"])})
    async with db.execute("SELECT gt.task_id AS task_id, g.id AS id, g.title AS title FROM goal_tasks gt "
                          "JOIN life_goals g ON g.id = gt.goal_id WHERE g.user_id = ? ORDER BY g.id", (uid,)) as cur:
        goal_links = await cur.fetchall()
    goals_of: dict[int, list] = {}
    for l in goal_links: goals_of.setdefault(l["task_id"], []).append({"id": l["id"], "title": l["title"]})
    tasks = []
    for r in rows:
        if scope == "week": show = visible_today(r, today) or any(occurs(r, d) for d in week)
        elif scope == "all": show = True
        elif scope == "done": show = is_done(r, today)  # раздел «Выполненные»
        else: show = visible_today(r, today)
        if not show: continue
        tasks.append({
            "id": r["id"], "text": r["text"], "priority": r["level"] == 3, "level": r["level"],
            "repeat": r["repeat_type"], "created": r["created_on"], "planned": planned_of(r), "due": r["due_date"],
            "time": r["due_time"], "remind": r["remind_min"], "category": r["category"], "note": r["note"],
            "completed": is_done(r, today),
            "done_on": r["done_on"] if r["done_on"] and r["done_on"] != "2000-01-01" else None,  # 2000-01-01 — метка старых записей без даты
            "overdue": is_overdue(r, today),
            "past_due": is_past_due(r, today),
            "subs": by_task.get(r["id"], []),
            "goals": goals_of.get(r["id"], []),  # жизненные цели, к которым привязана задача
        })
    # Порядок для дневного планировщика: невыполненные вперёд; по дню плана (просроченные раньше сегодняшних);
    # внутри дня — сначала задачи со временем по возрастанию, затем без времени по важности
    def order(t):
        day = t["planned"] if (t["repeat"] == "none" and t["planned"] < today) else today
        return (t["completed"], day, t["time"] is None, t["time"] or "", -t["level"], -t["id"])
    if scope == "done": tasks.sort(key=lambda t: (t["done_on"] or "", t["id"]), reverse=True)  # недавно выполненные — выше
    else: tasks.sort(key=order)
    return tasks


async def create_task(db, task: TaskCreate, uid: int) -> int:
    v = clean_task(task)
    today = client_today(task.today)
    planned = v["planned"] or today
    check_series(v["repeat"], planned, v["due"])
    cur = await db.execute(
        "INSERT INTO tasks (text, completed, priority, level, repeat_type, planned_date, due_date, due_time, remind_min, category, note, created_on, user_id) "
        "VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (v["text"], v["priority"], v["level"], v["repeat"], planned, v["due"], v["time"], v["remind"],
         v["category"], v["note"], today, uid))
    for s in task.subtasks:
        if s.strip(): await db.execute("INSERT INTO subtasks (task_id, text) VALUES (?, ?)", (cur.lastrowid, s.strip()))
    await db.commit()
    return cur.lastrowid


async def update_task(db, task_id: int, task: TaskCreate, uid: int):
    v = clean_task(task)
    old = await fetch_task(db, task_id, uid)
    planned = v["planned"] or planned_of(old)  # пустая плановая дата в форме = оставить как была
    check_series(v["repeat"], planned, v["due"])
    # created_on, done_on и журнал выполнения здесь не трогаем
    await db.execute(
        "UPDATE tasks SET text=?, priority=?, level=?, repeat_type=?, planned_date=?, due_date=?, due_time=?, remind_min=?, category=?, note=?, reminded_on=NULL WHERE id=?",
        (v["text"], v["priority"], v["level"], v["repeat"], planned, v["due"], v["time"], v["remind"],
         v["category"], v["note"], task_id))
    await db.commit()
    await task_changed(task_id, uid)  # время/повтор/дата могли измениться — пересчитать расписание напоминаний


async def toggle_task_db(db, task_id: int, today: str, uid: int):
    row = await fetch_task(db, task_id, uid)
    rep, done_on, cat = row["repeat_type"], row["done_on"], row["category"]
    was_done = (done_on == today) if rep != "none" else (done_on is not None)
    if was_done:
        await db.execute("DELETE FROM completions WHERE task_id = ? AND day = ?", (task_id, done_on))
        await db.execute("UPDATE tasks SET done_on = NULL WHERE id = ?", (task_id,))
    else:
        await db.execute("INSERT OR IGNORE INTO completions (task_id, day, category, user_id) VALUES (?, ?, ?, ?)",
                         (task_id, today, cat, uid))
        await db.execute("UPDATE tasks SET done_on = ? WHERE id = ?", (today, task_id))
    await db.commit()
    await task_changed(task_id, uid)


async def complete_task_db(db, task_id: int, today: str, uid: int) -> bool:
    """Идемпотентное «Выполнено» (в отличие от toggle не снимает отметку). True — отметили сейчас, False — уже была."""
    row = await fetch_task(db, task_id, uid)
    if is_done(row, today): return False
    await db.execute("INSERT OR IGNORE INTO completions (task_id, day, category, user_id) VALUES (?, ?, ?, ?)",
                     (task_id, today, row["category"], uid))
    await db.execute("UPDATE tasks SET done_on = ? WHERE id = ?", (today, task_id))
    await db.commit()
    await task_changed(task_id, uid)
    return True


async def move_task_db(db, task_id: int, to: str, today: str, uid: int) -> str:
    """Перенос = явная смена плановой даты одной разовой невыполненной задачи.
    Срок, дата создания и история выполнения (done_on, completions) не меняются."""
    row = await fetch_task(db, task_id, uid)
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
        await task_changed(task_id, uid)
    return iso


async def shift_task_time_db(db, task_id: int, minutes: int, now: datetime, uid: int) -> tuple[str, str]:
    """Сдвиг разовой задачи со временем на N минут. Отсчёт от большего из «время задачи» и «сейчас» (пользовательское
    локальное время), чтобы просроченную задачу не переносить в прошлое. Меняет плановую дату и время."""
    row = await fetch_task(db, task_id, uid)
    if row["repeat_type"] != "none":
        raise DomainError(400, "Повторяющуюся задачу перенести нельзя — она появляется по расписанию")
    if row["done_on"] is not None:
        raise DomainError(409, "Задача уже выполнена — переносить нечего")
    if not row["due_time"]:
        raise DomainError(400, "У задачи нет времени — перенесите её на другую дату")
    if not 0 < minutes <= 24 * 60:
        raise DomainError(400, "Некорректный сдвиг времени")
    start = datetime.fromisoformat(f"{planned_of(row)}T{row['due_time']}")
    new = max(start, now.replace(second=0, microsecond=0)) + timedelta(minutes=minutes)
    if (new.date() - now.date()).days > MAX_PLAN_AHEAD_DAYS: raise DomainError(400, "Слишком далёкая дата переноса")
    await db.execute("UPDATE tasks SET planned_date = ?, due_time = ?, reminded_on = NULL WHERE id = ?",
                     (new.date().isoformat(), new.strftime("%H:%M"), task_id))
    await db.commit()
    await task_changed(task_id, uid)
    return new.date().isoformat(), new.strftime("%H:%M")


async def carry_over_db(db, today: str, uid: int) -> int:
    """Переносит на сегодня только действительно просроченные невыполненные разовые задачи пользователя."""
    cur = await db.execute(
        "UPDATE tasks SET planned_date = ?, reminded_on = NULL WHERE repeat_type = 'none' AND done_on IS NULL AND user_id = ? "
        "AND COALESCE(planned_date, created_on, '2000-01-01') < ? AND (due_date IS NULL OR due_date < ?)",
        (today, uid, today, today))
    await db.commit()
    if cur.rowcount: await task_changed(None, uid)
    return cur.rowcount


async def set_task_due(db, task_id: int, value: str | None, uid: int):
    row = await fetch_task(db, task_id, uid)
    due = parse_day(value, "Срок").isoformat() if value else None
    check_series(row["repeat_type"], planned_of(row), due)
    await db.execute("UPDATE tasks SET due_date = ?, reminded_on = NULL WHERE id = ?", (due, task_id))
    await db.commit()
    await task_changed(task_id, uid)


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


HABIT_COLS = "id, text, created_on, remind_time, remind_days, remind_type"
HABIT_REMIND_TYPES = {"normal": "Обычное"}  # типы напоминаний; сейчас только обычное — сообщение «📚 Время "…"!»


def habit_days_list(raw) -> list[int]:
    """«0,2,4» -> [0, 2, 4] (0 — понедельник). Пустая строка — каждый день."""
    out = set()
    for x in str(raw or "").split(","):
        x = x.strip()
        if x.isdigit() and 0 <= int(x) <= 6: out.add(int(x))
    return sorted(out)


def habit_remind_view(row) -> dict | None:
    if not row["remind_time"]: return None
    days = habit_days_list(row["remind_days"])
    return {"time": row["remind_time"], "repeat": "days" if days else "daily", "days": days,
            "type": row["remind_type"] or "normal"}


def clean_habit_text(text) -> str:
    text = (text or "").strip()
    if not text: raise DomainError(400, "Пустое название")
    if len(text) > 120: raise DomainError(400, "Название слишком длинное")
    return text


def clean_habit_reminder(time, repeat: str, days, remind_type: str) -> tuple[str | None, str, str]:
    """(время ЧЧ:ММ или None, дни «0,2,4» или '' = каждый день, тип). Без времени напоминания нет."""
    if time is None or (isinstance(time, str) and not time.strip()):
        return None, "", "normal"
    t = parse_time(time, "Время напоминания")
    if remind_type not in HABIT_REMIND_TYPES: raise DomainError(400, "Неизвестный тип напоминания")
    if repeat not in ("daily", "days"): raise DomainError(400, "Частота: каждый день или по дням недели")
    if repeat == "daily": return t, "", remind_type
    picked = sorted({int(d) for d in days})
    if any(not 0 <= d <= 6 for d in picked): raise DomainError(400, "Дни недели: числа от 0 (понедельник) до 6 (воскресенье)")
    if not picked: raise DomainError(400, "Выберите хотя бы один день недели")
    return t, ("" if len(picked) == 7 else ",".join(map(str, picked))), remind_type


def habit_view(row, days: set[str], today: str) -> dict:
    t = date.fromisoformat(today)
    cur, best = habit_streaks(days, today)
    return {
        "id": row["id"], "text": row["text"], "created": row["created_on"], "remind": habit_remind_view(row),
        "done_today": today in days, "streak": cur, "best": best, "total": len(days),
        "week": [{"date": (t - timedelta(days=i)).isoformat(), "done": (t - timedelta(days=i)).isoformat() in days}
                 for i in range(6, -1, -1)],
    }


async def list_habits(db, today: str, uid: int) -> list[dict]:
    db.row_factory = aiosqlite.Row
    async with db.execute(f"SELECT {HABIT_COLS} FROM habits WHERE user_id = ? ORDER BY id DESC", (uid,)) as cur: rows = await cur.fetchall()
    async with db.execute("SELECT habit_id, day FROM habit_logs WHERE habit_id IN (SELECT id FROM habits WHERE user_id = ?)",
                          (uid,)) as cur: logs = await cur.fetchall()
    by: dict[int, set] = {}
    for l in logs: by.setdefault(l["habit_id"], set()).add(l["day"])
    async with db.execute("SELECT gh.habit_id AS habit_id, g.id AS id, g.title AS title FROM goal_habits gh "
                          "JOIN life_goals g ON g.id = gh.goal_id WHERE g.user_id = ? ORDER BY g.id", (uid,)) as cur:
        links = await cur.fetchall()
    goals_of: dict[int, list] = {}
    for l in links: goals_of.setdefault(l["habit_id"], []).append({"id": l["id"], "title": l["title"]})
    return [{**habit_view(r, by.get(r["id"], set()), today), "goals": goals_of.get(r["id"], [])} for r in rows]


async def toggle_habit_db(db, habit_id: int, day: str, today: str, uid: int) -> dict:
    db.row_factory = aiosqlite.Row
    async with db.execute(f"SELECT {HABIT_COLS} FROM habits WHERE id = ? AND user_id = ?", (habit_id, uid)) as cur:
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


async def create_habit(db, text: str, uid: int, today: str | None = None, time: str | None = None,
                       repeat: str = "daily", days=(), remind_type: str = "normal") -> int:
    text = clean_habit_text(text)
    r_time, r_days, r_type = clean_habit_reminder(time, repeat, list(days), remind_type)
    # remind_set_at: напоминание не отправляется «задним числом» за время, которое на сегодня уже прошло
    cur = await db.execute(
        "INSERT INTO habits (text, created_on, user_id, remind_time, remind_days, remind_type, remind_set_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (text, today or today_iso(), uid, r_time, r_days, r_type, iso(utcnow()) if r_time else None))
    await db.commit()
    return cur.lastrowid


async def update_habit(db, habit_id: int, text: str, uid: int, time: str | None = None,
                       repeat: str = "daily", days=(), remind_type: str = "normal"):
    text = clean_habit_text(text)
    r_time, r_days, r_type = clean_habit_reminder(time, repeat, list(days), remind_type)
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT remind_time, remind_days, remind_type, remind_set_at FROM habits WHERE id = ? AND user_id = ?",
                          (habit_id, uid)) as cur:
        old = await cur.fetchone()
    if not old: raise DomainError(404, "Привычка не найдена")
    same = (old["remind_time"], old["remind_days"] or "", old["remind_type"] or "normal") == (r_time, r_days, r_type)
    set_at = old["remind_set_at"] if same else (iso(utcnow()) if r_time else None)
    await db.execute("UPDATE habits SET text = ?, remind_time = ?, remind_days = ?, remind_type = ?, remind_set_at = ? WHERE id = ?",
                     (text, r_time, r_days, r_type, set_at, habit_id))
    await db.commit()
    await habit_changed(uid)  # время/дни изменились — снять устаревшие ожидающие отправки


# =====================================================================
#  ЦЕЛИ: операции
# =====================================================================
async def fetch_goal(db, goal_id: int, uid: int):
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT id, title, emoji, unit, target, current FROM goals WHERE id = ? AND user_id = ?",
                          (goal_id, uid)) as cur:
        r = await cur.fetchone()
    if not r: raise DomainError(404, "Цель не найдена")
    return r


async def set_goal_target(db, goal_id: int, target, uid: int) -> float:
    await fetch_goal(db, goal_id, uid)
    v = check_positive(target, "Значение цели")
    await db.execute("UPDATE goals SET target = ? WHERE id = ?", (v, goal_id))
    await db.commit()
    return v


async def apply_goal_progress(db, goal_id: int, mode: str, value, uid: int) -> dict:
    if mode not in ("add", "set"): raise DomainError(400, "Режим прогресса: add или set")
    await fetch_goal(db, goal_id, uid)
    v = check_number(value, "Значение")
    if mode == "set":
        if v < 0: raise DomainError(400, "Прогресс не может быть отрицательным")
        await db.execute("UPDATE goals SET current = ? WHERE id = ?", (v, goal_id))
    else:
        await db.execute("UPDATE goals SET current = MAX(current + ?, 0) WHERE id = ?", (v, goal_id))
    await db.commit()
    return dict(await fetch_goal(db, goal_id, uid))


# =====================================================================
#  ЖИЗНЕННЫЕ ЦЕЛИ: операции
#  «Накопления» — это прежние цели с числовым прогрессом (таблица goals), они работают как раньше.
#  Жизненная цель — отдельная сущность: результат, срок, статус, этапы, связи с задачами и привычками.
#  Прогресс жизненной цели не хранится, а считается при каждом чтении, поэтому всегда соответствует
#  текущему состоянию этапов и задач.
# =====================================================================
GOAL_CATEGORIES = {"work": "Работа", "study": "Учёба", "growth": "Развитие", "health": "Здоровье",
                   "finance": "Финансы", "personal": "Личное", "other": "Другое"}
GOAL_STATUSES = {"active": "Активная", "done": "Выполнена", "paused": "Отложена"}
MILESTONE_STATUSES = {"todo": "Не начат", "active": "В работе", "done": "Выполнен"}
MAX_MILESTONES = 50  # этапов в одной цели
MAX_LIFE_GOALS = 200  # жизненных целей у одного пользователя


def clean_goal_text(value, field: str, limit: int, required: bool = False) -> str:
    text = (value or "").strip()
    if required and not text: raise DomainError(400, f"{field}: не может быть пустым")
    if len(text) > limit: raise DomainError(400, f"{field}: слишком длинное (максимум {limit} символов)")
    return text


def clean_choice(value, allowed: dict, field: str) -> str:
    if value not in allowed: raise DomainError(400, f"{field}: допустимо только {', '.join(allowed)}")
    return value


def clean_goal_day(value, field: str = "Срок") -> str | None:
    """Пустое значение — срока нет."""
    if value is None or (isinstance(value, str) and not value.strip()): return None
    d = parse_day(value, field)
    if not 2000 <= d.year <= 2100: raise DomainError(400, f"{field}: год должен быть от 2000 до 2100")
    return d.isoformat()


def clean_milestone(title, deadline, status) -> dict:
    return dict(title=clean_goal_text(title, "Название этапа", 200, True), deadline=clean_goal_day(deadline, "Срок этапа"),
                status=clean_choice(status, MILESTONE_STATUSES, "Статус этапа"))


def provided(body) -> dict:
    """Только поля, которые клиент явно прислал (в том числе null — «очистить»)."""
    return body.model_dump(exclude_unset=True) if hasattr(body, "model_dump") else body.dict(exclude_unset=True)


def pct(frac: float) -> int:
    """Доля -> проценты с округлением «половина вверх» (как в JavaScript, чтобы цифры в интерфейсе и на сервере совпадали)."""
    return int(frac * 100 + 0.5)


def stage_fraction(status: str, tasks_total: int, tasks_done: int) -> float:
    """Доля готовности этапа. Выполненный этап — 1; иначе доля выполненных связанных задач.
    Задачи сами этап не завершают: когда все задачи сделаны, этап остаётся «в работе», пока пользователь его не закроет."""
    if status == "done": return 1.0
    return tasks_done / tasks_total if tasks_total else 0.0


def calc_goal_progress(stages: list[float], loose_total: int, loose_done: int) -> int:
    """Прогресс цели, %: среднее по этапам. Задачи, не отнесённые ни к одному этапу, считаются ещё одним этапом.
    100 только когда готово всё; иначе не больше 99."""
    parts = list(stages)
    if loose_total: parts.append(loose_done / loose_total)
    if not parts: return 0
    if all(p >= 1 for p in parts): return 100
    return min(99, pct(sum(parts) / len(parts)))


def _marks(ids: list) -> str:
    return ",".join("?" * len(ids))


def _goal_task_item(r, today: str) -> dict:
    return {"id": r["id"], "text": r["text"], "completed": is_done(r, today), "repeat": r["repeat_type"],
            "planned": planned_of(r), "due": r["due_date"], "time": r["due_time"], "level": r["level"],
            "overdue": is_overdue(r, today), "past_due": is_past_due(r, today),
            "milestone_id": r["milestone_id"],
            "counts": r["repeat_type"] == "none"}  # повторяющиеся задачи в прогресс не входят


async def fetch_life_goal(db, goal_id: int, uid: int):
    """Цель пользователя uid. Чужая и несуществующая — одинаково «не найдена»."""
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM life_goals WHERE id = ? AND user_id = ?", (goal_id, uid)) as cur:
        r = await cur.fetchone()
    if not r: raise DomainError(404, "Цель не найдена")
    return r


async def fetch_milestone(db, goal_id: int, milestone_id: int):
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM goal_milestones WHERE id = ? AND goal_id = ?", (milestone_id, goal_id)) as cur:
        r = await cur.fetchone()
    if not r: raise DomainError(404, "Этап не найден")
    return r


async def build_life_goals(db, uid: int, today: str, rows: list, detail: bool = False) -> list[dict]:
    """Собирает цели вместе с этапами, задачами и привычками тремя запросами на весь список."""
    if not rows: return []
    ids = [r["id"] for r in rows]
    db.row_factory = aiosqlite.Row
    async with db.execute(f"SELECT * FROM goal_milestones WHERE goal_id IN ({_marks(ids)}) ORDER BY position, id", ids) as cur:
        ms_rows = await cur.fetchall()
    async with db.execute(
            f"SELECT t.*, gt.goal_id AS goal_id, gt.milestone_id AS milestone_id FROM goal_tasks gt "
            f"JOIN tasks t ON t.id = gt.task_id WHERE gt.goal_id IN ({_marks(ids)}) AND t.user_id = ? ORDER BY t.id",
            ids + [uid]) as cur:
        t_rows = await cur.fetchall()
    async with db.execute(
            f"SELECT gh.goal_id AS goal_id, gh.habit_id AS habit_id FROM goal_habits gh "
            f"JOIN habits h ON h.id = gh.habit_id WHERE gh.goal_id IN ({_marks(ids)}) AND h.user_id = ? ORDER BY gh.habit_id",
            ids + [uid]) as cur:
        h_rows = await cur.fetchall()
    habit_by_id = {h["id"]: h for h in await list_habits(db, today, uid)} if detail and h_rows else {}

    ms_by_goal: dict[int, list] = {}
    for m in ms_rows: ms_by_goal.setdefault(m["goal_id"], []).append(m)
    tasks_by_goal: dict[int, list] = {}
    for r in t_rows: tasks_by_goal.setdefault(r["goal_id"], []).append(r)
    habits_by_goal: dict[int, list] = {}
    for h in h_rows: habits_by_goal.setdefault(h["goal_id"], []).append(h["habit_id"])

    def counted(rs):  # (всего, выполнено) среди задач, учитываемых в прогрессе
        c = [r for r in rs if r["repeat_type"] == "none"]
        return len(c), sum(1 for r in c if is_done(r, today))

    t0 = date.fromisoformat(today)
    out = []
    for g in rows:
        gid = g["id"]
        ms_list = ms_by_goal.get(gid, [])
        known = {m["id"] for m in ms_list}
        by_ms: dict = {}
        for r in tasks_by_goal.get(gid, []):
            by_ms.setdefault(r["milestone_id"] if r["milestone_id"] in known else None, []).append(r)
        stages, ms_out = [], []
        for m in ms_list:
            tt, td = counted(by_ms.get(m["id"], []))
            frac = stage_fraction(m["status"], tt, td)
            stages.append(frac)
            item = {"id": m["id"], "title": m["title"], "deadline": m["deadline"], "status": m["status"],
                    "status_label": MILESTONE_STATUSES[m["status"]], "done_on": m["done_on"], "position": m["position"],
                    "overdue": m["status"] != "done" and bool(m["deadline"]) and m["deadline"] < today,
                    "progress": 100 if m["status"] == "done" else pct(frac),
                    "tasks_total": tt, "tasks_done": td}
            if detail: item["tasks"] = [_goal_task_item(r, today) for r in by_ms.get(m["id"], [])]
            ms_out.append(item)
        loose = by_ms.get(None, [])
        lt, ld = counted(loose)
        all_total, all_done = counted(tasks_by_goal.get(gid, []))
        progress = 100 if g["status"] == "done" else calc_goal_progress(stages, lt, ld)
        pending = [m for m in ms_out if m["status"] != "done"]
        nxt = min(pending, key=lambda m: (m["deadline"] is None, m["deadline"] or "", m["position"])) if pending else None
        active = g["status"] == "active"
        item = {
            "id": gid, "title": g["title"], "result": g["result"],
            "category": g["category"], "category_label": GOAL_CATEGORIES.get(g["category"], GOAL_CATEGORIES["other"]),
            "deadline": g["deadline"], "status": g["status"], "status_label": GOAL_STATUSES[g["status"]],
            "created": g["created_on"], "completed_on": g["completed_on"],
            "progress": progress,
            "ready_to_complete": active and progress == 100,  # всё сделано, но закрыть цель должен пользователь
            "days_left": (date.fromisoformat(g["deadline"]) - t0).days if g["deadline"] else None,
            "overdue": active and bool(g["deadline"]) and g["deadline"] < today,
            "milestones_total": len(ms_out), "milestones_done": sum(1 for m in ms_out if m["status"] == "done"),
            "tasks_total": all_total, "tasks_done": all_done, "tasks_linked": len(tasks_by_goal.get(gid, [])),
            "habits_total": len(habits_by_goal.get(gid, [])),
            "next_milestone": {"id": nxt["id"], "title": nxt["title"], "deadline": nxt["deadline"]} if nxt else None,
        }
        if detail:
            item["milestones"] = ms_out
            item["tasks"] = [_goal_task_item(r, today) for r in loose]  # задачи вне этапов
            item["habits"] = [habit_by_id[h] for h in habits_by_goal.get(gid, []) if h in habit_by_id]
        out.append(item)
    return out


async def list_life_goals(db, uid: int, today: str, status: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM life_goals WHERE user_id = ?", [uid]
    if status and status != "all":
        clean_choice(status, GOAL_STATUSES, "Статус")
        sql += " AND status = ?"
        args.append(status)
    db.row_factory = aiosqlite.Row
    async with db.execute(sql, args) as cur: rows = await cur.fetchall()
    rank = {"active": 0, "paused": 1, "done": 2}
    # активные первыми, внутри — по ближайшему сроку; цели без срока — в конце группы
    rows = sorted(rows, key=lambda r: (rank[r["status"]], r["deadline"] is None, r["deadline"] or "", -r["id"]))
    return await build_life_goals(db, uid, today, rows)


async def get_life_goal(db, goal_id: int, uid: int, today: str) -> dict:
    row = await fetch_life_goal(db, goal_id, uid)
    return (await build_life_goals(db, uid, today, [row], detail=True))[0]


async def create_life_goal(db, body, uid: int, today: str) -> int:
    title = clean_goal_text(body.title, "Название", 200, True)
    result = clean_goal_text(body.result, "Результат", 1000)
    category = clean_choice(body.category, GOAL_CATEGORIES, "Категория")
    deadline = clean_goal_day(body.deadline)
    status = clean_choice(body.status, GOAL_STATUSES, "Статус")
    if len(body.milestones) > MAX_MILESTONES: raise DomainError(400, f"В цели не больше {MAX_MILESTONES} этапов")
    stages = [clean_milestone(m.title, m.deadline, m.status) for m in body.milestones]
    async with db.execute("SELECT COUNT(*) FROM life_goals WHERE user_id = ?", (uid,)) as cur:
        if (await cur.fetchone())[0] >= MAX_LIFE_GOALS: raise DomainError(400, f"Не больше {MAX_LIFE_GOALS} жизненных целей")
    cur = await db.execute(
        "INSERT INTO life_goals (user_id, title, result, category, deadline, status, created_on, completed_on) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, title, result, category, deadline, status, today, today if status == "done" else None))
    gid = cur.lastrowid
    for pos, m in enumerate(stages, 1):
        await db.execute("INSERT INTO goal_milestones (goal_id, title, deadline, status, position, done_on) VALUES (?, ?, ?, ?, ?, ?)",
                         (gid, m["title"], m["deadline"], m["status"], pos, today if m["status"] == "done" else None))
    await db.commit()
    return gid


async def update_life_goal(db, goal_id: int, fields: dict, uid: int, today: str):
    g = await fetch_life_goal(db, goal_id, uid)
    sets: dict = {}
    if "title" in fields: sets["title"] = clean_goal_text(fields["title"], "Название", 200, True)
    if "result" in fields: sets["result"] = clean_goal_text(fields["result"], "Результат", 1000)
    if "category" in fields: sets["category"] = clean_choice(fields["category"], GOAL_CATEGORIES, "Категория")
    if "deadline" in fields: sets["deadline"] = clean_goal_day(fields["deadline"])
    if "status" in fields:
        st = clean_choice(fields["status"], GOAL_STATUSES, "Статус")
        sets["status"] = st
        if st != g["status"]: sets["completed_on"] = today if st == "done" else None
    if sets:
        cols = list(sets)  # имена колонок задаются только кодом выше, не клиентом
        await db.execute(f"UPDATE life_goals SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                         [sets[c] for c in cols] + [goal_id])
        await db.commit()


async def delete_life_goal(db, goal_id: int, uid: int):
    """Удаляет цель, её этапы и связи. Сами задачи и привычки остаются — это отдельные сущности."""
    await fetch_life_goal(db, goal_id, uid)
    for table in ("goal_milestones", "goal_tasks", "goal_habits"):
        await db.execute(f"DELETE FROM {table} WHERE goal_id = ?", (goal_id,))
    await db.execute("DELETE FROM life_goals WHERE id = ?", (goal_id,))
    await db.commit()


async def add_milestone(db, goal_id: int, title, deadline, status: str, uid: int, today: str) -> int:
    await fetch_life_goal(db, goal_id, uid)
    m = clean_milestone(title, deadline, status)
    async with db.execute("SELECT COUNT(*), COALESCE(MAX(position), 0) FROM goal_milestones WHERE goal_id = ?", (goal_id,)) as cur:
        count, last = await cur.fetchone()
    if count >= MAX_MILESTONES: raise DomainError(400, f"В цели не больше {MAX_MILESTONES} этапов")
    cur = await db.execute("INSERT INTO goal_milestones (goal_id, title, deadline, status, position, done_on) VALUES (?, ?, ?, ?, ?, ?)",
                           (goal_id, m["title"], m["deadline"], m["status"], last + 1, today if m["status"] == "done" else None))
    await db.commit()
    return cur.lastrowid


async def update_milestone(db, goal_id: int, milestone_id: int, fields: dict, uid: int, today: str):
    await fetch_life_goal(db, goal_id, uid)
    old = await fetch_milestone(db, goal_id, milestone_id)
    sets: dict = {}
    if "title" in fields: sets["title"] = clean_goal_text(fields["title"], "Название этапа", 200, True)
    if "deadline" in fields: sets["deadline"] = clean_goal_day(fields["deadline"], "Срок этапа")
    if "status" in fields:
        st = clean_choice(fields["status"], MILESTONE_STATUSES, "Статус этапа")
        sets["status"] = st
        if st != old["status"]: sets["done_on"] = today if st == "done" else None
    if sets:
        cols = list(sets)
        await db.execute(f"UPDATE goal_milestones SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                         [sets[c] for c in cols] + [milestone_id])
        await db.commit()


async def delete_milestone(db, goal_id: int, milestone_id: int, uid: int):
    """Задачи удалённого этапа остаются привязанными к цели — просто вне этапов."""
    await fetch_life_goal(db, goal_id, uid)
    await fetch_milestone(db, goal_id, milestone_id)
    await db.execute("UPDATE goal_tasks SET milestone_id = NULL WHERE goal_id = ? AND milestone_id = ?", (goal_id, milestone_id))
    await db.execute("DELETE FROM goal_milestones WHERE id = ?", (milestone_id,))
    await db.commit()


async def link_task_to_goal(db, goal_id: int, task_id: int, milestone_id: int | None, uid: int):
    """Привязывает существующую задачу к цели (и, при желании, к этапу). Повторный вызов переносит задачу на другой этап."""
    await fetch_life_goal(db, goal_id, uid)
    await fetch_task(db, task_id, uid)
    if milestone_id is not None: await fetch_milestone(db, goal_id, milestone_id)
    await db.execute("INSERT INTO goal_tasks (goal_id, task_id, milestone_id) VALUES (?, ?, ?) "
                     "ON CONFLICT (goal_id, task_id) DO UPDATE SET milestone_id = excluded.milestone_id",
                     (goal_id, task_id, milestone_id))
    await db.commit()


async def unlink_task_from_goal(db, goal_id: int, task_id: int, uid: int):
    await fetch_life_goal(db, goal_id, uid)
    cur = await db.execute("DELETE FROM goal_tasks WHERE goal_id = ? AND task_id = ?", (goal_id, task_id))
    if cur.rowcount == 0: raise DomainError(404, "Задача не привязана к этой цели")
    await db.commit()


async def create_goal_task(db, goal_id: int, task, uid: int, today: str) -> int:
    """Создаёт обычную задачу myday (той же функцией, что и в разделе задач) и сразу привязывает её к цели."""
    await fetch_life_goal(db, goal_id, uid)
    if task.milestone_id is not None: await fetch_milestone(db, goal_id, task.milestone_id)
    task.today = task.today or today
    task_id = await create_task(db, task, uid)
    await db.execute("INSERT INTO goal_tasks (goal_id, task_id, milestone_id) VALUES (?, ?, ?)", (goal_id, task_id, task.milestone_id))
    await db.commit()
    return task_id


async def link_habit_to_goal(db, goal_id: int, habit_id: int, uid: int):
    """Привычки остаются в своей системе — цель лишь ссылается на них."""
    await fetch_life_goal(db, goal_id, uid)
    async with db.execute("SELECT 1 FROM habits WHERE id = ? AND user_id = ?", (habit_id, uid)) as cur:
        if not await cur.fetchone(): raise DomainError(404, "Привычка не найдена")
    await db.execute("INSERT OR IGNORE INTO goal_habits (goal_id, habit_id) VALUES (?, ?)", (goal_id, habit_id))
    await db.commit()


async def unlink_habit_from_goal(db, goal_id: int, habit_id: int, uid: int):
    await fetch_life_goal(db, goal_id, uid)
    cur = await db.execute("DELETE FROM goal_habits WHERE goal_id = ? AND habit_id = ?", (goal_id, habit_id))
    if cur.rowcount == 0: raise DomainError(404, "Привычка не привязана к этой цели")
    await db.commit()


def savings_item(r) -> dict:
    d = dict(r)
    d["percent"] = min(100, pct(d["current"] / d["target"])) if d["target"] else 0
    return d


async def goals_overview(db, uid: int, today: str) -> dict:
    """Всё для раздела «Цели» и главной: активные жизненные цели (первыми) и накопления."""
    life = await list_life_goals(db, uid, today)
    counts = {s: 0 for s in GOAL_STATUSES}
    for g in life: counts[g["status"]] += 1
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT id, title, emoji, unit, target, current FROM goals WHERE user_id = ? ORDER BY id DESC", (uid,)) as cur:
        savings = [savings_item(r) for r in await cur.fetchall()]
    return {
        "life_goals": [g for g in life if g["status"] == "active"],
        "counts": {**counts, "total": len(life)},
        "savings": {"items": savings, "count": len(savings),
                    "completed": sum(1 for s in savings if s["current"] >= s["target"]),
                    "avg_percent": int(sum(s["percent"] for s in savings) / len(savings) + 0.5) if savings else 0},
        "categories": [{"key": k, "label": v} for k, v in GOAL_CATEGORIES.items()],
        "statuses": [{"key": k, "label": v} for k, v in GOAL_STATUSES.items()],
        "milestone_statuses": [{"key": k, "label": v} for k, v in MILESTONE_STATUSES.items()],
    }


async def claim_legacy_data(db):
    """Привычки, события и накопления, созданные до появления владельцев, не удаляются.
    Они достаются владельцу из LEGACY_OWNER_ID, а без этой переменной — единственному пользователю, если он один.
    Если пользователей несколько и переменная не задана, старые записи просто лежат в базе, никому не видимые."""
    owner = os.getenv("LEGACY_OWNER_ID", "").strip()
    if owner.isdigit():
        uid = int(owner)
    else:
        async with db.execute("SELECT user_id FROM users") as cur: users = await cur.fetchall()
        if len(users) != 1: return
        uid = users[0][0]
    for table in ("habits", "goals", "events"):
        await db.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (uid,))
    await db.commit()


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
            [KeyboardButton(text="📊 Отчет активности"), KeyboardButton(text="🔔 Уведомления")]
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
async def build_plan(today: str, uid: int):
    async with aiosqlite.connect(DB_NAME) as db:
        tasks = await list_tasks(db, today, "today", uid)
        habits = await list_habits(db, today, uid)
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
            bell = f" · 🔔 {h['remind']['time']}" if h.get("remind") else ""
            lines.append(f"{'✅' if h['done_today'] else '⬜'} {html.escape(h['text'])}{streak}{bell}")
            rows.append([InlineKeyboardButton(text=f"{'✅' if h['done_today'] else '⬜'} {h['text'][:28]}",
                                              callback_data=f"pl_hab:{h['id']}")])
    tail = []
    if slipped: tail.append(InlineKeyboardButton(text=f"⏩ Просроченные на сегодня ({slipped})", callback_data="pl_carry"))
    tail.append(InlineKeyboardButton(text="🔄 Обновить", callback_data="pl_ref"))
    rows.append(tail)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def refresh_plan(cq: CallbackQuery, note: str | None = None, alert: bool = False):
    text, kb = await build_plan(today_iso(), cq.from_user.id)
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
    await register_user(message.from_user.id if message.from_user else message.chat.id, message.chat.id)

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
    text, kb = await build_plan(today_iso(), message.from_user.id)
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
        task_id = await create_task(db, TaskCreate(text=text, repeat="none", today=today), message.from_user.id)
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
        cur = await db.execute("INSERT INTO goals (title, emoji, unit, target, current, user_id) VALUES (?, '🎯', '', 100, 0, ?)",
                               (title, message.from_user.id))
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
        cur = await db.execute("INSERT INTO events (date, title, color, emoji, user_id) VALUES (?, ?, '#3a86ff', '📌', ?)",
                               (today_iso(), title, message.from_user.id))
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
        clean_habit_text(text)
    except DomainError as e:
        await message.answer(f"⚠️ {e.message}. Попробуй ещё раз:")
        return
    await state.update_data(habit_text=text)
    await state.set_state(Creator.habit_time)
    await message.answer("⏰ Во сколько напоминать? Формат ЧЧ:ММ, например 08:30.\nНапиши «нет», если напоминание не нужно.")


MENU_TEXTS = {"📋 План", "📝 Задачи", "🎯 Цели", "📅 События", "🌱 Привычки", "📊 Отчет активности", "🔔 Уведомления"}
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def habit_freq_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Каждый день", callback_data="hf:daily"),
        InlineKeyboardButton(text="По дням недели", callback_data="hf:days")]])


def habit_days_kb(picked: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=("✅ " if i in picked else "") + WEEKDAYS_SHORT[i], callback_data=f"hd:{i}")
             for i in range(4)],
            [InlineKeyboardButton(text=("✅ " if i in picked else "") + WEEKDAYS_SHORT[i], callback_data=f"hd:{i}")
             for i in range(4, 7)],
            [InlineKeyboardButton(text="Готово", callback_data="hd:ok")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_text(days: list[int]) -> str:
    return ", ".join(WEEKDAYS_SHORT[d] for d in days) if days else "каждый день"


@dp.message(Creator.habit_time)
async def process_habit_time(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw in MENU_TEXTS:  # нажали кнопку меню посреди создания — выходим из режима ввода
        await state.clear()
        await message.answer("Создание привычки отменено. Нажми кнопку ещё раз.")
        return
    data = await state.get_data()
    if raw.lower() in ("нет", "-", "0"):
        await finish_habit(message, state, data["habit_text"], message.from_user.id, None, "daily", [])
        return
    try:
        t = parse_time(raw)
    except DomainError:
        await message.answer("⚠️ Не понял время. Напиши ЧЧ:ММ, например 08:30, или «нет»:")
        return
    await state.update_data(habit_remind=t, habit_days=[])
    await state.set_state(Creator.habit_freq)
    await message.answer("🔁 Как часто напоминать?", reply_markup=habit_freq_kb())


@dp.callback_query(Creator.habit_freq, F.data == "hf:daily")
async def cq_habit_daily(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await cq.answer()
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await finish_habit(cq.message, state, data["habit_text"], cq.from_user.id, data["habit_remind"], "daily", [])


@dp.callback_query(Creator.habit_freq, F.data == "hf:days")
async def cq_habit_days(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await cq.answer()
    try:
        await cq.message.edit_text("📆 В какие дни напоминать? Выбери дни и нажми «Готово».",
                                   reply_markup=habit_days_kb(data.get("habit_days", [])))
    except TelegramBadRequest:
        pass


@dp.callback_query(Creator.habit_freq, F.data.startswith("hd:"))
async def cq_habit_day(cq: CallbackQuery, state: FSMContext):
    arg = cq.data[3:]
    data = await state.get_data()
    days = set(data.get("habit_days", []))
    if arg == "ok":
        if not days: return await cq.answer("Выбери хотя бы один день", show_alert=True)
        await cq.answer()
        try:
            await cq.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await finish_habit(cq.message, state, data["habit_text"], cq.from_user.id, data["habit_remind"], "days", sorted(days))
        return
    if arg.isdigit() and 0 <= int(arg) <= 6:
        days ^= {int(arg)}
    await state.update_data(habit_days=sorted(days))
    try:
        await cq.message.edit_reply_markup(reply_markup=habit_days_kb(sorted(days)))
    except TelegramBadRequest:
        pass
    await cq.answer()


async def finish_habit(message: Message, state: FSMContext, text: str, uid: int, r_time: str | None, repeat: str, days: list[int]):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await create_habit(db, text, uid, time=r_time, repeat=repeat, days=days)
    except DomainError as e:
        await state.clear()
        await message.answer(f"⚠️ {html.escape(e.message)}")
        return
    await state.clear()
    lines = ["🌱 <b>Привычка добавлена</b>\n", "<blockquote>", f"<b>Название:</b> {html.escape(text)}"]
    if r_time:
        lines += [f"<b>Время:</b> {r_time}", f"<b>Частота:</b> {schedule_text(days)}", "<b>Напоминание:</b> обычное"]
    else:
        lines.append("<b>Напоминание:</b> нет")
    lines += ["</blockquote>", "\nОтмечать её можно в «📋 План»."]
    await message.answer("\n".join(lines), reply_markup=habit_kb(), parse_mode=ParseMode.HTML)


# 5. ОТЧЕТ АКТИВНОСТИ
@dp.message(F.text == "📊 Отчет активности")
async def btn_report(message: Message, state: FSMContext):
    today = today_iso()
    d_from = (date.fromisoformat(today) - timedelta(days=6)).isoformat()

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE user_id = ?", (message.from_user.id,)) as cur:
            rows = await cur.fetchall()
        async with db.execute("SELECT day FROM completions WHERE day BETWEEN ? AND ? AND user_id = ?",
                              (d_from, today, message.from_user.id)) as cur:
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
            await toggle_task_db(db, int(cq.data.split(":")[1]), today_iso(), cq.from_user.id)
    except DomainError as e:
        return await cq.answer(e.message, show_alert=True)
    await refresh_plan(cq)


@dp.callback_query(F.data.startswith("pl_tom:"))
async def cq_pl_tom(cq: CallbackQuery):
    today = today_iso()
    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await move_task_db(db, int(cq.data.split(":")[1]), tomorrow, today, cq.from_user.id)
    except DomainError as e:
        return await cq.answer(e.message, show_alert=True)
    await refresh_plan(cq, "Перенесено на завтра")


@dp.callback_query(F.data.startswith("pl_hab:"))
async def cq_pl_hab(cq: CallbackQuery):
    today = today_iso()
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await toggle_habit_db(db, int(cq.data.split(":")[1]), today, today, cq.from_user.id)
    except DomainError as e:
        return await cq.answer(e.message, show_alert=True)
    await refresh_plan(cq)


@dp.callback_query(F.data == "pl_carry")
async def cq_pl_carry(cq: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        n = await carry_over_db(db, today_iso(), cq.from_user.id)
    await refresh_plan(cq, f"Перенесено на сегодня: {n}" if n else "Просроченных задач нет")


@dp.callback_query(F.data == "pl_ref")
async def cq_pl_ref(cq: CallbackQuery):
    await refresh_plan(cq, "Обновлено")


@dp.callback_query(F.data.startswith("t_lvl:"))
async def cq_t_lvl(cq: CallbackQuery):
    task_id = int(cq.data.split(":")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT level FROM tasks WHERE id = ? AND user_id = ?", (task_id, cq.from_user.id)) as cur:
            r = await cur.fetchone()
            if not r: return await cq.answer("Задача не найдена", show_alert=True)
            lvl = r[0] % 3 + 1
            await db.execute("UPDATE tasks SET level = ?, priority = ? WHERE id = ?", (lvl, int(lvl == 3), task_id))
            await db.commit()
    await cq.message.edit_text(await get_task_text(task_id), reply_markup=task_kb(task_id), parse_mode=ParseMode.HTML)
    await cq.answer()


@dp.callback_query(F.data.startswith("t_rep:"))
async def cq_t_rep(cq: CallbackQuery):
    task_id = int(cq.data.split(":")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT repeat_type FROM tasks WHERE id = ? AND user_id = ?", (task_id, cq.from_user.id)) as cur:
            r = await cur.fetchone()
            if not r: return await cq.answer("Задача не найдена", show_alert=True)
            nxt = {"none": "daily", "daily": "weekdays", "weekdays": "none"}[r[0]]
            await db.execute("UPDATE tasks SET repeat_type = ? WHERE id = ?", (nxt, task_id))
            await db.commit()
    await task_changed(task_id, cq.from_user.id)
    await cq.message.edit_text(await get_task_text(task_id), reply_markup=task_kb(task_id), parse_mode=ParseMode.HTML)
    await cq.answer()


async def init_edit_state(cq: CallbackQuery, state: FSMContext, next_state, prompt_text: str):
    item_id = int(cq.data.split(":")[1])
    # править можно только свои задачи, цели и события
    table = "goals" if cq.data.startswith("g_") else "events" if cq.data.startswith("e_") else "tasks"
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(f"SELECT 1 FROM {table} WHERE id = ? AND user_id = ?", (item_id, cq.from_user.id)) as cur:
            if not await cur.fetchone(): return await cq.answer("Не найдено", show_alert=True)
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
        text, kb = await build_plan(today_iso(), message.from_user.id)
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
            await move_task_db(db, data['id'], to, today, message.from_user.id)
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
            await move_task_db(db, data['id'], to, today, message.from_user.id)
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
            await set_task_due(db, data['id'], value, message.from_user.id)
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
        await db.execute("UPDATE tasks SET due_time = ?, reminded_on = NULL WHERE id = ?", (t, data['id']))
        await db.commit()
    await task_changed(data['id'], message.from_user.id)
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
            await set_goal_target(db, data['id'], (message.text or "").strip().replace(",", "."), message.from_user.id)
    except DomainError as e:
        await message.answer(f"⚠️ {e.message}. Отмена.")
        await state.clear()
        return
    await finish_edit(message, state, 'goal', data['id'])


@dp.message(Editor.g_unt)
async def process_g_unt(message: Message, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE goals SET unit = ? WHERE id = ? AND user_id = ?",
                         ((message.text or "").strip(), data['id'], message.from_user.id))
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
        await db.execute("UPDATE events SET date = ? WHERE id = ? AND user_id = ?", (d, data['id'], message.from_user.id))
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
        await db.execute("UPDATE events SET time = ? WHERE id = ? AND user_id = ?", (t, data['id'], message.from_user.id))
        await db.commit()
    await finish_edit(message, state, 'event', data['id'])


# =====================================================================
#  TELEGRAM: кнопки напоминаний и настройки уведомлений
# =====================================================================
async def bot_user(from_user_id: int, chat_id: int) -> dict:
    async with notif_db() as db:
        return await load_user(db, from_user_id) or await ensure_user(db, from_user_id, chat_id)


async def log_row_for_message(chat_id: int, message_id: int):
    async with notif_db() as db:
        async with db.execute("SELECT * FROM notification_log WHERE chat_id = ? AND tg_message_id = ? AND status = 'sent'",
                              (chat_id, message_id)) as cur:
            return await cur.fetchone()


async def deactivate_kb(chat_id: int, message_id: int):
    try:
        async with notif_db() as db:
            await db.execute("UPDATE notification_log SET kb_active = 0, updated_at = ? WHERE chat_id = ? AND tg_message_id = ?",
                             (iso(utcnow()), chat_id, message_id))
            await db.commit()
    except Exception:
        log.exception("Не удалось снять отметку клавиатуры")


# --- любой пользователь, написавший боту, сразу получает владельца задач и настройки уведомлений ---
async def touch_user(user_id: int, chat_id: int):
    async with notif_db() as db:
        u = await load_user(db, user_id)
        if not u or not u["active"] or u["chat_id"] != chat_id:
            await ensure_user(db, user_id, chat_id, reactivate=True)  # заодно снимает блокировку после «бот остановлен»


class EnsureUserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        sender = getattr(event, "from_user", None)
        if sender is not None and not sender.is_bot:
            msg = event if isinstance(event, Message) else getattr(event, "message", None)
            try:
                await touch_user(sender.id, msg.chat.id if msg is not None else sender.id)
            except Exception:
                log.exception("Не удалось зарегистрировать пользователя %s", sender.id)  # обработчик всё равно выполнится
        return await handler(event, data)


dp.message.outer_middleware(EnsureUserMiddleware())
dp.callback_query.outer_middleware(EnsureUserMiddleware())


# --- кнопки под напоминаниями и сводками ---
@dp.callback_query(F.data.startswith("rm_"))
async def cq_reminder(cq: CallbackQuery, state: FSMContext):
    try:
        action, tid, day = cq.data[3:].split(":", 2)
        task_id = int(tid)
    except ValueError:
        return await cq.answer()
    chat_id, msg_id = cq.message.chat.id, cq.message.message_id
    try:
        u = await bot_user(cq.from_user.id, chat_id)
        today = utc_to_local(utcnow(), u["zone"]).date().isoformat()

        if action in ("mv", "bk"):
            async with notif_db() as db:
                task = await fetch_task_row(db, task_id, u["user_id"])
            if not task or (task["repeat_type"] == "none" and task["done_on"] is not None):
                await refresh_messages(task_id, u["user_id"])
                return await cq.answer("Задача не найдена или уже выполнена", show_alert=True)
            if action == "mv":
                await cq.message.edit_reply_markup(reply_markup=move_menu_kb(task, day, today))
            else:
                row = await log_row_for_message(chat_id, msg_id)
                if row and row["kind"] in ("remind", "snooze"):
                    await cq.message.edit_reply_markup(reply_markup=reminder_kb(task_id, day))
                else:
                    await refresh_messages(task_id, u["user_id"])  # сводку пересобираем целиком — вернётся список кнопок
            return await cq.answer()

        if action == "dt":
            await init_edit_state(cq, state, Editor.r_date, "На какую дату перенести? Например: 25.10, 2026-10-25, завтра")
            await state.update_data(day=day)
            return

        toast, alert = await reminder_action(u, action, task_id, day)
        await flush_background()  # изменения уже отправили обновление всех связанных сообщений
        if action in ("z30", "z60"):
            async with notif_db() as db:
                task = await fetch_task_row(db, task_id, u["user_id"])
            minutes = 30 if action == "z30" else 60
            await safe_edit(chat_id, msg_id, f"🔕 <b>{html.escape(task['text'])}</b> — напомню через {ru_minutes(minutes)}", None)
            await deactivate_kb(chat_id, msg_id)
        await cq.answer(toast, show_alert=alert)
    except DomainError as e:
        if e.status == 404: await refresh_messages(task_id, cq.from_user.id)
        await cq.answer(e.message, show_alert=True)
    except Exception:
        log.exception("Ошибка обработки кнопки напоминания %s", cq.data)
        try:
            await cq.answer("Не получилось, попробуйте ещё раз", show_alert=True)
        except Exception:
            pass


@dp.message(Editor.r_date)
async def process_r_date(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        u = await bot_user(message.from_user.id, message.chat.id)
        today = utc_to_local(utcnow(), u["zone"]).date().isoformat()
        to = parse_user_date(message.text, today)
        async with notif_db() as db:
            await move_task_db(db, data["id"], to, today, u["user_id"])
    except DomainError as e:
        await message.answer(f"⚠️ {e.message}. Введите дату ещё раз (например 25.10) или нажмите кнопку меню:")
        return
    except Exception:
        log.exception("Не удалось перенести задачу из напоминания")
        await message.answer("⚠️ Не получилось перенести задачу, попробуйте ещё раз позже.")
        await state.clear()
        return
    await flush_background()
    try:
        await message.delete()
        if "prompt_id" in data: await bot.delete_message(chat_id=message.chat.id, message_id=data["prompt_id"])
    except Exception:
        pass
    await state.clear()


# --- настройки уведомлений в боте (те же данные, что и в приложении) ---
def offset_label(m: int) -> str:
    if m % 1440 == 0: return f"{m // 1440} д"
    if m % 60 == 0: return f"{m // 60} ч"
    return f"{m} мин"


def offsets_label(offs: list[int]) -> str:
    return ", ".join(offset_label(m) for m in offs) if offs else "выключены"


def parse_offsets_text(raw: str) -> list[int]:
    """«60, 30, 10», «2ч, 15», «1д» -> минуты. «нет» — пустой список (напоминания выключены)."""
    t = (raw or "").strip().lower()
    if t in ("нет", "-", "0", ""): return []
    out = []
    for part in re.split(r"[,;]", t):
        part = part.strip()
        if not part: continue
        m = re.fullmatch(r"(\d+)\s*([а-яё]*)", part)
        if not m: raise DomainError(400, f"Не понял «{part}». Пример: 60, 30, 10 или 2ч")
        unit = m[2]
        mult = 60 if unit.startswith("ч") else 1440 if unit.startswith("д") else 1 if unit in ("", "м", "мин", "минут", "минуты") else None
        if mult is None: raise DomainError(400, f"Не понял единицу «{unit}». Используйте мин, ч или д")
        out.append(int(m[1]) * mult)
    return out


def settings_text(u: dict) -> str:
    s = u["s"]
    m = lambda v: "✅" if v else "⬜"
    return "\n".join([
        "🔔 <b>Уведомления</b>", "",
        f"🌍 Часовой пояс: <b>{html.escape(u['tz'])}</b>{' (определяется автоматически)' if u['tz_auto'] else ''}", "",
        f"{m(s['enabled'])} Все уведомления",
        f"⏰ Напоминать до задачи: {offsets_label(parse_offsets(s['remind_offsets']))}",
        f"{m(s['morning_on'])} Утренний план — {s['morning_time']}",
        f"{m(s['evening_on'])} Вечерний итог — {s['evening_time']}",
        f"{m(s['overdue_on'])} Сводка просроченных — {s['overdue_time']}", "",
        "<i>Те же настройки доступны в приложении, раздел «Уведомления».</i>"])


def settings_kb(u: dict) -> InlineKeyboardMarkup:
    s = u["s"]
    m = lambda v: "✅" if v else "⬜"
    B = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text=f"{m(s['enabled'])} Все", callback_data="nt:enabled")],
        [B(text="⏰ До задачи: " + offsets_label(parse_offsets(s['remind_offsets'])), callback_data="nto")],
        [B(text=f"{m(s['morning_on'])} Утро", callback_data="nt:morning_on"),
         B(text=f"🕔 {s['morning_time']}", callback_data="ntm:morning")],
        [B(text=f"{m(s['evening_on'])} Вечер", callback_data="nt:evening_on"),
         B(text=f"🕗 {s['evening_time']}", callback_data="ntm:evening")],
        [B(text=f"{m(s['overdue_on'])} Просрочено", callback_data="nt:overdue_on"),
         B(text=f"🕘 {s['overdue_time']}", callback_data="ntm:overdue")],
        [B(text="🌍 Часовой пояс", callback_data="ntz")],
        [B(text="Закрыть", callback_data="close_kb")],
    ])


async def show_settings(target_message: Message | None = None, chat_id: int | None = None, message_id: int | None = None,
                        user_id: int | None = None):
    u = await bot_user(user_id, chat_id or target_message.chat.id)
    if target_message is not None and message_id is None:
        await target_message.answer(settings_text(u), reply_markup=settings_kb(u), parse_mode=ParseMode.HTML)
    else:
        await safe_edit(chat_id, message_id, settings_text(u), settings_kb(u))


@dp.message(Command("notify"))
@dp.message(F.text == "🔔 Уведомления")
async def btn_notify(message: Message, state: FSMContext):
    await state.clear()
    try:
        await show_settings(target_message=message, user_id=message.from_user.id)
    except Exception:
        log.exception("Не удалось показать настройки уведомлений")
        await message.answer("⚠️ Не удалось загрузить настройки, попробуйте позже.")


@dp.callback_query(F.data.startswith("nt:"))
async def cq_nt_toggle(cq: CallbackQuery):
    key = cq.data[3:]
    try:
        u = await bot_user(cq.from_user.id, cq.message.chat.id)
        if key not in BOOL_KEYS and key != "tz_auto": return await cq.answer()
        cur = u["tz_auto"] if key == "tz_auto" else u["s"][key]
        await update_notification_settings(u["user_id"], {key: 0 if cur else 1})
        await show_settings(chat_id=cq.message.chat.id, message_id=cq.message.message_id, user_id=cq.from_user.id)
        await cq.answer("Сохранено")
    except DomainError as e:
        await cq.answer(e.message, show_alert=True)
    except Exception:
        log.exception("Не удалось сохранить настройку %s", key)
        await cq.answer("Не удалось сохранить, попробуйте ещё раз", show_alert=True)


NOTIFY_PROMPTS = {"morning": "утренний план", "evening": "вечерний итог", "overdue": "сводку просроченных"}


@dp.callback_query(F.data.startswith("ntm:"))
async def cq_nt_time(cq: CallbackQuery, state: FSMContext):
    kind = cq.data[4:]
    if kind not in DIGESTS: return await cq.answer()
    await state.set_state(Editor.n_time)
    msg = await cq.message.answer(f"<i>Во сколько присылать {NOTIFY_PROMPTS[kind]}? Формат ЧЧ:ММ, например 05:00</i>",
                                  parse_mode=ParseMode.HTML)
    await state.update_data(kind=kind, msg_id=cq.message.message_id, prompt_id=msg.message_id)
    await cq.answer()


@dp.callback_query(F.data == "ntz")
async def cq_nt_tz(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Editor.n_tz)
    msg = await cq.message.answer("<i>Введите часовой пояс, например Europe/Moscow или Asia/Yekaterinburg. "
                                  "«авто» — определять по устройству в приложении.</i>", parse_mode=ParseMode.HTML)
    await state.update_data(msg_id=cq.message.message_id, prompt_id=msg.message_id)
    await cq.answer()


@dp.callback_query(F.data == "nto")
async def cq_nt_offsets(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Editor.n_offs)
    msg = await cq.message.answer("<i>За сколько минут до задачи напоминать? Перечислите через запятую, например: 60, 30, 10. "
                                  "Можно писать «2ч» или «1д». «нет» — не напоминать.</i>", parse_mode=ParseMode.HTML)
    await state.update_data(msg_id=cq.message.message_id, prompt_id=msg.message_id)
    await cq.answer()


async def _finish_settings_input(message: Message, state: FSMContext, data: dict):
    try:
        await message.delete()
        if "prompt_id" in data: await bot.delete_message(chat_id=message.chat.id, message_id=data["prompt_id"])
    except Exception:
        pass
    await state.clear()
    await show_settings(chat_id=message.chat.id, message_id=data["msg_id"], user_id=message.from_user.id)


@dp.message(Editor.n_time)
async def process_n_time(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        u = await bot_user(message.from_user.id, message.chat.id)
        await update_notification_settings(u["user_id"], {f"{data['kind']}_time": (message.text or "").strip()})
    except DomainError as e:
        await message.answer(f"⚠️ {e.message}. Введите время ещё раз (например 05:00) или нажмите кнопку меню:")
        return
    except Exception:
        log.exception("Не удалось сохранить время уведомления")
        await message.answer("⚠️ Не удалось сохранить, попробуйте позже.")
        await state.clear()
        return
    await _finish_settings_input(message, state, data)


@dp.message(Editor.n_offs)
async def process_n_offs(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        offs = parse_offsets_text(message.text or "")
        u = await bot_user(message.from_user.id, message.chat.id)
        await update_notification_settings(u["user_id"], {"remind_offsets": offs})
    except DomainError as e:
        await message.answer(f"⚠️ {e.message}. Введите ещё раз (например 60, 30, 10) или нажмите кнопку меню:")
        return
    except Exception:
        log.exception("Не удалось сохранить напоминания до задачи")
        await message.answer("⚠️ Не удалось сохранить, попробуйте позже.")
        await state.clear()
        return
    await _finish_settings_input(message, state, data)


@dp.message(Editor.n_tz)
async def process_n_tz(message: Message, state: FSMContext):
    data = await state.get_data()
    raw = (message.text or "").strip()
    try:
        u = await bot_user(message.from_user.id, message.chat.id)
        patch = {"tz_auto": 1} if raw.lower() in ("авто", "auto") else {"tz": raw}
        await update_notification_settings(u["user_id"], patch)
    except DomainError as e:
        await message.answer(f"⚠️ {e.message}. Введите пояс ещё раз или нажмите кнопку меню:")
        return
    except Exception:
        log.exception("Не удалось сохранить часовой пояс")
        await message.answer("⚠️ Не удалось сохранить, попробуйте позже.")
        await state.clear()
        return
    await _finish_settings_input(message, state, data)


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
                db, TaskCreate(text=title, repeat="none", level=level or 2, planned=planned, today=today), message.from_user.id)
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
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
Path(DB_NAME).parent.mkdir(parents=True, exist_ok=True)
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


class HabitCreate(BaseModel):
    text: str
    time: str | None = None  # ЧЧ:ММ; пусто — без напоминания
    repeat: str = "daily"  # daily — каждый день, days — по выбранным дням недели
    days: list[int] = []  # для repeat=days: 0 — понедельник … 6 — воскресенье
    remind_type: str = "normal"


class EventCreate(BaseModel): date: str; title: str; time: str | None = None; color: str = "#3a86ff"; emoji: str = "📌"


class GoalCreate(BaseModel): title: str; target: float; unit: str = ""; emoji: str = "🎯"; current: float = 0


class GoalProgress(BaseModel): mode: str; value: float


class MilestoneIn(BaseModel): title: str; deadline: str | None = None; status: str = "todo"


class LifeGoalCreate(BaseModel):
    title: str
    result: str = ""
    category: str = "other"
    deadline: str | None = None
    status: str = "active"
    milestones: list[MilestoneIn] = []


class LifeGoalUpdate(BaseModel):  # меняются только присланные поля; deadline: null — убрать срок
    title: str | None = None
    result: str | None = None
    category: str | None = None
    deadline: str | None = None
    status: str | None = None


class MilestoneUpdate(BaseModel):
    title: str | None = None
    deadline: str | None = None
    status: str | None = None


class GoalTaskCreate(TaskCreate):
    repeat: str = "none"  # задача для цели по умолчанию разовая: повторяющиеся в прогресс не входят
    milestone_id: int | None = None


class GoalTaskLink(BaseModel): task_id: int; milestone_id: int | None = None


class GoalHabitLink(BaseModel): habit_id: int


# --- задачи (у каждой есть владелец; пользователь определяется по Telegram initData, см. api_user) ---
async def api_uid(request: Request) -> int:
    return (await api_user(request))["user_id"]


@app.get("/api/tasks")
async def get_tasks(request: Request, today: str | None = None, scope: str = "today"):
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        return {"tasks": await list_tasks(db, t, scope, uid)}


@app.post("/api/tasks")
async def add_task(task: TaskCreate, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        return {"status": "ok", "id": await create_task(db, task, uid)}


@app.put("/api/tasks/{task_id}")
async def update_task_api(task_id: int, task: TaskCreate, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        await update_task(db, task_id, task, uid)
    return {"status": "ok"}


@app.post("/api/tasks/{task_id}/toggle")
async def toggle_task(task_id: int, request: Request, today: str | None = None):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        await toggle_task_db(db, task_id, client_today(today), uid)
    return {"status": "ok"}


@app.post("/api/tasks/{task_id}/duplicate")
async def duplicate_task(task_id: int, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        r = await fetch_task(db, task_id, uid)
        today = today_iso()
        planned = planned_of(r)
        if r["repeat_type"] == "none" and planned < today: planned = today  # копия не должна рождаться просроченной
        cur = await db.execute(
            "INSERT INTO tasks (text, completed, priority, level, repeat_type, planned_date, due_date, due_time, remind_min, category, note, created_on, user_id) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["text"] + " (копия)", r["priority"], r["level"], r["repeat_type"], planned, r["due_date"],
             r["due_time"], r["remind_min"], r["category"], r["note"], today, uid))
        await db.execute("INSERT INTO subtasks (task_id, text) SELECT ?, text FROM subtasks WHERE task_id = ?",
                         (cur.lastrowid, task_id))
        await db.commit()
    return {"status": "ok", "id": cur.lastrowid}


@app.post("/api/tasks/{task_id}/move")
async def move_task(task_id: int, body: MoveBody, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        planned = await move_task_db(db, task_id, body.to, client_today(body.today), uid)
    return {"status": "ok", "planned": planned}


@app.post("/api/tasks/carry-over")
async def carry_over(request: Request, today: str | None = None):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        moved = await carry_over_db(db, client_today(today), uid)
    return {"status": "ok", "moved": moved}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        await fetch_task(db, task_id, uid)
        await db.execute("DELETE FROM subtasks WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM goal_tasks WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))  # история в completions остаётся для отчётов
        await db.commit()
    await task_changed(task_id, uid)
    return {"status": "ok"}


@app.post("/api/tasks/{task_id}/subtasks")
async def add_subtask(task_id: int, s: SubCreate, request: Request):
    uid = await api_uid(request)
    text = s.text.strip()
    if not text: raise DomainError(400, "Пустое название")
    async with aiosqlite.connect(DB_NAME) as db:
        await fetch_task(db, task_id, uid)
        await db.execute("INSERT INTO subtasks (task_id, text) VALUES (?, ?)", (task_id, text))
        await db.commit()
    return {"status": "ok"}


@app.post("/api/subtasks/{sub_id}/toggle")
async def toggle_subtask(sub_id: int, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("UPDATE subtasks SET done = 1 - done WHERE id = ? AND task_id IN (SELECT id FROM tasks WHERE user_id = ?)",
                               (sub_id, uid))
        if cur.rowcount == 0: raise DomainError(404, "Подзадача не найдена")
        await db.commit()
    return {"status": "ok"}


@app.delete("/api/subtasks/{sub_id}")
async def delete_subtask(sub_id: int, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("DELETE FROM subtasks WHERE id = ? AND task_id IN (SELECT id FROM tasks WHERE user_id = ?)",
                               (sub_id, uid))
        if cur.rowcount == 0: raise DomainError(404, "Подзадача не найдена")
        await db.commit()
    return {"status": "ok"}


# --- отчёты ---
async def build_stats(d_from: str, d_to: str, today: str, uid: int):
    start, end = parse_day(d_from, "Начало периода"), parse_day(d_to, "Конец периода")
    if end < start or (end - start).days > 400: raise DomainError(400, "Период от 1 до 400 дней")
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE user_id = ?", (uid,)) as cur: rows = await cur.fetchall()
        async with db.execute("SELECT day, category FROM completions WHERE day BETWEEN ? AND ? AND user_id = ?",
                              (d_from, d_to, uid)) as cur: comps = await cur.fetchall()
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
async def get_stats(request: Request, today: str | None = None, date_from: str = Query(alias="from"), date_to: str = Query(alias="to")):
    return await build_stats(date_from, date_to, client_today(today), await api_uid(request))


@app.get("/api/stats/export")
async def export_stats(request: Request, today: str | None = None, date_from: str = Query(alias="from"), date_to: str = Query(alias="to")):
    uid = await api_uid(request)
    st = await build_stats(date_from, date_to, client_today(today), uid)
    path = REPORTS_DIR / f"report_{uid}_{date_from}_{date_to}.csv"  # у каждого пользователя свой файл
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Дата", "Запланировано", "Выполнено"])
        w.writerows([d["date"], d["planned"], d["done"]] for d in st["days"])
        w.writerow([])
        w.writerow(["Категория", "Выполнено"])
        w.writerows([c["name"] or "Без категории", c["done"]] for c in st["categories"])
    return {"status": "ok", "path": str(path.resolve())}


# =====================================================================
#  УВЕДОМЛЕНИЯ: настройки, расписание, доставка, синхронизация сообщений
# =====================================================================
# Как это устроено:
#  * Расписание не хранится «вперёд» — оно ВЫЧИСЛЯЕТСЯ из текущего состояния задач и настроек каждые
#    NOTIFY_TICK_SECONDS. Поэтому перенос/правка времени/смена часового пояса применяются сами, а после
#    перезапуска ничего «восстанавливать» отдельно не нужно: состояние целиком лежит в SQLite.
#  * notification_log — журнал отправок и одновременно защита от дублей: dedup_key UNIQUE.
#    Ключ включает пользователя, задачу, ДАТУ ПОВТОРА, ВРЕМЯ задачи и отступ (30/10), поэтому после переноса
#    ключ новый и старая отправка ничего не блокирует; а повторную отправку того же самого ключ исключает.
#  * Перед КАЖДОЙ отправкой (в том числе повторной) вызывается check_row — одна функция проверки актуальности
#    (дата, время, статус задачи, настройки пользователя). Её же использует генератор кандидатов.
log = logging.getLogger("myday.notify")

NOTIFY_TICK_SECONDS = 20  # шаг планировщика: напоминание приходит с задержкой не больше этого значения
MAX_REMIND_MINUTES = 24 * 60  # самое раннее напоминание — за сутки до начала
MAX_REMIND_OFFSETS = 8  # сколько напоминаний можно настроить


def parse_offsets(raw) -> list[int]:
    """«60,30,10» или список -> [60, 30, 10] (по убыванию, без дублей и мусора)."""
    items = raw.replace(";", ",").split(",") if isinstance(raw, str) else list(raw or [])
    out = set()
    for x in items:
        try:
            v = int(str(x).strip())
        except ValueError:
            continue
        if 1 <= v <= MAX_REMIND_MINUTES: out.add(v)
    return sorted(out, reverse=True)


def offsets_of(u: dict) -> list[int]:
    """За сколько минут до начала задачи пользователь хочет получать напоминания."""
    return parse_offsets(u["s"].get("remind_offsets"))
DIGEST_GRACE = timedelta(hours=3)  # сколько после назначенного времени ещё можно отправить сводку (после простоя)
HABIT_GRACE = timedelta(minutes=30)  # напоминание о привычке позже чем через 30 минут после времени уже не нужно
MAX_SEND_ATTEMPTS = 5
RETRY_DELAYS = (30, 60, 120, 300)  # секунды до следующей попытки; последняя повторяется
STUCK_SENDING_AFTER = timedelta(minutes=5)
HISTORY_KEEP_DAYS = 90
KB_EDIT_WINDOW = timedelta(hours=46)  # Telegram не позволяет править сообщения старше 48 часов
INIT_DATA_MAX_AGE = 7 * 24 * 3600
# 1 — без initData API работает с «основным» пользователем (как раньше, без авторизации); 0 — только из Telegram
ALLOW_NO_AUTH = os.getenv("ALLOW_NO_AUTH", "0") == "1"

DEFAULT_SETTINGS = {
    "enabled": 1, "remind_offsets": "30,10",
    "morning_on": 1, "morning_time": "05:00",
    "evening_on": 1, "evening_time": "20:00",
    "overdue_on": 1, "overdue_time": "09:00",
}
BOOL_KEYS = ("enabled", "morning_on", "evening_on", "overdue_on")
TIME_KEYS = ("morning_time", "evening_time", "overdue_time")
SETTING_KEYS = tuple(DEFAULT_SETTINGS)
DIGESTS = {"morning": ("morning_on", "morning_time"),
           "evening": ("evening_on", "evening_time"),
           "overdue": ("overdue_on", "overdue_time")}
KIND_LABELS = {"remind": "Напоминание", "snooze": "Отложенное напоминание", "morning": "Утренний план",
               "evening": "Вечерний итог", "overdue": "Просроченные", "habit": "Привычка", "test": "Тест"}

_tick_state: dict = {"loop": None, "lock": None}
_bg: set = set()


def _tick_lock_get() -> asyncio.Lock:
    """Один тик за раз. Замок создаётся под текущий event loop (безопасно при перезапуске цикла)."""
    loop = asyncio.get_running_loop()
    if _tick_state["loop"] is not loop:
        _tick_state.update(loop=loop, lock=asyncio.Lock())
    return _tick_state["lock"]


# --- время: все моменты в журнале — UTC без tzinfo, строкой ISO; «локальное» — по поясу пользователя ---
def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def zone(name: str | None):
    """ZoneInfo по имени; если базы часовых поясов нет — как и везде в проекте, время сервера (None)."""
    try:
        return ZoneInfo(name) if name else LOCAL_TZ
    except Exception:
        return LOCAL_TZ


def utc_to_local(u: datetime, tz) -> datetime:
    aware = u.replace(tzinfo=timezone.utc)
    return (aware.astimezone(tz) if tz else aware.astimezone()).replace(tzinfo=None)


def local_to_utc(l: datetime, tz) -> datetime:
    aware = l.replace(tzinfo=tz) if tz else l  # naive трактуется как локальное время сервера
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def at_clock(day: date, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime.combine(day, dtime(int(h), int(m)))


def ru_minutes(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11: return f"{n} минуту"
    if n % 10 in (2, 3, 4) and not 11 <= n % 100 <= 14: return f"{n} минуты"
    return f"{n} минут"


def _spawn(coro):
    """Фоновая задача с удержанием ссылки и логированием ошибок (ничто не должно падать молча или рушить приложение)."""
    t = asyncio.ensure_future(coro)
    _bg.add(t)

    def _done(f):
        _bg.discard(f)
        if not f.cancelled() and f.exception():
            log.error("Фоновая задача уведомлений упала: %r", f.exception())

    t.add_done_callback(_done)
    return t


async def flush_background():
    if _bg: await asyncio.gather(*list(_bg), return_exceptions=True)


@asynccontextmanager
async def notif_db():
    async with aiosqlite.connect(DB_NAME, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout = 15000")
        yield db


# =====================================================================
#  Миграции (идемпотентные, без потери данных)
# =====================================================================
NOTIFY_MIGRATIONS = [
    ("notifications_001_users_settings_log", [
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            tz TEXT NOT NULL,
            tz_auto INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS notification_settings (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            remind_30 INTEGER NOT NULL DEFAULT 1,
            remind_10 INTEGER NOT NULL DEFAULT 1,
            morning_on INTEGER NOT NULL DEFAULT 1,
            morning_time TEXT NOT NULL DEFAULT '05:00',
            evening_on INTEGER NOT NULL DEFAULT 1,
            evening_time TEXT NOT NULL DEFAULT '20:00',
            overdue_on INTEGER NOT NULL DEFAULT 1,
            overdue_time TEXT NOT NULL DEFAULT '09:00',
            updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS notification_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            chat_id INTEGER,
            kind TEXT NOT NULL,
            task_id INTEGER,
            occ_day TEXT,
            task_time TEXT,
            offset_min INTEGER,
            title TEXT,
            fire_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            tg_message_id INTEGER,
            kb_active INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            updated_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_nlog_status_fire ON notification_log (status, fire_at)",
        "CREATE INDEX IF NOT EXISTS idx_nlog_task ON notification_log (task_id)",
        "CREATE INDEX IF NOT EXISTS idx_nlog_user_created ON notification_log (user_id, created_at)",
    ]),
    ("notifications_002_task_owner", [
        ("ADD_COLUMN", "tasks", "user_id", "INTEGER"),
        ("ADD_COLUMN", "completions", "user_id", "INTEGER"),
        "CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_completions_user_day ON completions (user_id, day)",
    ]),
    # Одноразовый сброс: удаляет все задачи, созданные до появления владельцев (user_id), чтобы новые
    # пользователи не наследовали чужие старые задачи. Выполняется один раз и больше не повторяется.
    ("notifications_003_reset_tasks", [
        "DELETE FROM subtasks",
        "DELETE FROM completions",
        "DELETE FROM tasks",
        "DELETE FROM notification_log WHERE task_id IS NOT NULL",
    ]),
    # Привычки, события и накопления тоже становятся личными. Старые записи без владельца НЕ удаляются:
    # их получает владелец (см. claim_legacy_data).
    ("notifications_004_personal_data", [
        ("ADD_COLUMN", "habits", "user_id", "INTEGER"),
        ("ADD_COLUMN", "goals", "user_id", "INTEGER"),
        ("ADD_COLUMN", "events", "user_id", "INTEGER"),
        "CREATE INDEX IF NOT EXISTS idx_habits_user ON habits (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_goals_user ON goals (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_user_date ON events (user_id, date)",
    ]),
    # Жизненные цели с этапами и связями с задачами и привычками. Таблица goals («накопления») не меняется.
    ("goals_005_life_goals", [
        "CREATE TABLE IF NOT EXISTS life_goals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
        "title TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'other', deadline TEXT, "
        "status TEXT NOT NULL DEFAULT 'active', created_on TEXT NOT NULL, completed_on TEXT)",
        "CREATE TABLE IF NOT EXISTS goal_milestones (id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id INTEGER NOT NULL, "
        "title TEXT NOT NULL, deadline TEXT, status TEXT NOT NULL DEFAULT 'todo', position INTEGER NOT NULL DEFAULT 0, done_on TEXT)",
        "CREATE TABLE IF NOT EXISTS goal_tasks (goal_id INTEGER NOT NULL, task_id INTEGER NOT NULL, milestone_id INTEGER, "
        "PRIMARY KEY (goal_id, task_id))",
        "CREATE TABLE IF NOT EXISTS goal_habits (goal_id INTEGER NOT NULL, habit_id INTEGER NOT NULL, "
        "PRIMARY KEY (goal_id, habit_id))",
        "CREATE INDEX IF NOT EXISTS idx_life_goals_user ON life_goals (user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_goal_milestones_goal ON goal_milestones (goal_id)",
        "CREATE INDEX IF NOT EXISTS idx_goal_tasks_task ON goal_tasks (task_id)",
        "CREATE INDEX IF NOT EXISTS idx_goal_habits_habit ON goal_habits (habit_id)",
    ]),
    # Напоминания о привычках: время, дни недели, тип. remind_days: '' — каждый день, иначе «0,2,4» (0 — понедельник).
    ("habits_006_reminders", [
        ("ADD_COLUMN", "habits", "remind_time", "TEXT"),
        ("ADD_COLUMN", "habits", "remind_days", "TEXT NOT NULL DEFAULT ''"),
        ("ADD_COLUMN", "habits", "remind_type", "TEXT NOT NULL DEFAULT 'normal'"),
        ("ADD_COLUMN", "habits", "remind_set_at", "TEXT"),
        ("ADD_COLUMN", "notification_log", "habit_id", "INTEGER"),
        "CREATE INDEX IF NOT EXISTS idx_nlog_habit ON notification_log (habit_id)",
    ]),
    # Свои напоминания до задачи: список минут вместо двух фиксированных переключателей (30 и 10).
    # Прежний выбор пользователя переносится: включённые 30/10 становятся элементами списка.
    ("notifications_007_remind_offsets", [
        ("ADD_COLUMN", "notification_settings", "remind_offsets", "TEXT NOT NULL DEFAULT '30,10'"),
        "UPDATE notification_settings SET remind_offsets = CASE WHEN remind_30 AND remind_10 THEN '30,10' "
        "WHEN remind_30 THEN '30' WHEN remind_10 THEN '10' ELSE '' END",
    ]),
]


async def migrate_notifications(db):
    """Применяет только ещё не применённые миграции, каждую в одной транзакции. Перед первой — копия файла БД."""
    db.row_factory = aiosqlite.Row
    await db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    async with db.execute("SELECT id FROM schema_migrations") as cur:
        applied = {r[0] for r in await cur.fetchall()}
    pending = [m for m in NOTIFY_MIGRATIONS if m[0] not in applied]
    if not pending:
        await backfill_completion_owners(db)
        return
    try:
        src = Path(DB_NAME)
        bak = Path(str(DB_NAME) + ".before-notifications.bak")
        if src.exists() and src.stat().st_size > 0 and not bak.exists():
            shutil.copy2(src, bak)
    except Exception as e:  # копия — страховка, её отсутствие не повод ронять запуск
        log.warning("Не удалось сделать резервную копию БД перед миграцией: %r", e)
    if any(m[0].startswith("goals_") for m in pending):
        try:
            src_db, bak_goals = Path(DB_NAME), Path(str(DB_NAME) + ".before-goals.bak")
            if src_db.exists() and src_db.stat().st_size > 0 and not bak_goals.exists():
                shutil.copy2(src_db, bak_goals)
        except Exception as e:
            log.warning("Не удалось сделать резервную копию БД перед миграцией целей: %r", e)
    for mid, statements in pending:
        try:
            await db.execute("BEGIN")
            for st in statements:
                if isinstance(st, tuple):  # ("ADD_COLUMN", таблица, колонка, тип) — только если колонки ещё нет
                    _, table, col, ddl = st
                    async with db.execute(f"PRAGMA table_info({table})") as cur:
                        have = {r[1] for r in await cur.fetchall()}
                    if col not in have: await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                else:
                    await db.execute(st)
            await db.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)", (mid, iso(utcnow())))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    # Прежняя схема хранила единственный chat_id в settings — делаем из него первого пользователя
    async with db.execute("SELECT value FROM settings WHERE key = 'chat_id'") as cur:
        row = await cur.fetchone()
    if row and str(row[0]).lstrip("-").isdigit():
        chat = int(row[0])
        now = iso(utcnow())
        await db.execute("INSERT OR IGNORE INTO users (user_id, chat_id, tz, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                         (chat, chat, TZ_NAME, now, now))
        await db.execute("INSERT OR IGNORE INTO notification_settings (user_id, updated_at) VALUES (?, ?)", (chat, now))
        await db.commit()
        u = await load_user(db, chat)
        if u: await skip_past_digests(db, u, list(DIGESTS), utcnow())
    await backfill_completion_owners(db)


async def adopt_orphans(db, user_id: int):
    """Задачи, созданные до появления владельцев, достаются первому пользователю — раньше бот был одиночным."""
    await db.execute("UPDATE tasks SET user_id = ? WHERE user_id IS NULL", (user_id,))
    await db.execute("UPDATE completions SET user_id = ? WHERE user_id IS NULL "
                     "AND task_id IN (SELECT id FROM tasks WHERE user_id = ?)", (user_id, user_id))
    await db.commit()


async def backfill_completion_owners(db):
    """Отметки выполнения наследуют владельца задачи (записи удалённых задач остаются без владельца и в отчётах не видны)."""
    await db.execute("UPDATE completions SET user_id = (SELECT t.user_id FROM tasks t WHERE t.id = completions.task_id) "
                     "WHERE user_id IS NULL AND EXISTS (SELECT 1 FROM tasks t WHERE t.id = completions.task_id AND t.user_id IS NOT NULL)")
    await db.commit()


# =====================================================================
#  Пользователи и настройки (общие для приложения и бота)
# =====================================================================
async def load_user(db, user_id: int) -> dict | None:
    cols = ", ".join(f"s.{k}" for k in SETTING_KEYS)
    async with db.execute(
            f"SELECT u.user_id, u.chat_id, u.tz, u.tz_auto, u.active, {cols} FROM users u "
            "JOIN notification_settings s ON s.user_id = u.user_id WHERE u.user_id = ?", (user_id,)) as cur:
        r = await cur.fetchone()
    return _user_from_row(r) if r else None


def _user_from_row(r) -> dict:
    d = dict(r)
    return {"user_id": d["user_id"], "chat_id": d["chat_id"], "tz": d["tz"], "tz_auto": d["tz_auto"],
            "active": d["active"], "zone": zone(d["tz"]), "s": {k: d[k] for k in SETTING_KEYS}}


async def load_active_users(db) -> list[dict]:
    cols = ", ".join(f"s.{k}" for k in SETTING_KEYS)
    async with db.execute(
            f"SELECT u.user_id, u.chat_id, u.tz, u.tz_auto, u.active, {cols} FROM users u "
            "JOIN notification_settings s ON s.user_id = u.user_id WHERE u.active = 1") as cur:
        return [_user_from_row(r) for r in await cur.fetchall()]


async def ensure_user(db, user_id: int, chat_id: int, reactivate: bool = False) -> dict:
    now = iso(utcnow())
    cur = await db.execute("INSERT OR IGNORE INTO users (user_id, chat_id, tz, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (user_id, chat_id, TZ_NAME, now, now))
    created = cur.rowcount == 1
    if not created and (reactivate or chat_id):
        await db.execute("UPDATE users SET chat_id = ?, active = CASE WHEN ? THEN 1 ELSE active END, updated_at = ? "
                         "WHERE user_id = ?", (chat_id, int(reactivate), now, user_id))
    await db.execute("INSERT OR IGNORE INTO notification_settings (user_id, updated_at) VALUES (?, ?)", (user_id, now))
    await db.commit()
    u = await load_user(db, user_id)
    if created:
        await skip_past_digests(db, u, list(DIGESTS), utcnow())
    return u


async def register_user(user_id: int, chat_id: int):
    """Вызывается из /start. Ошибки БД не должны мешать боту отвечать."""
    try:
        async with notif_db() as db:
            await ensure_user(db, user_id, chat_id, reactivate=True)
    except Exception:
        log.exception("Не удалось зарегистрировать пользователя %s для уведомлений", user_id)


def settings_view(u: dict) -> dict:
    out = {k: bool(u["s"][k]) for k in BOOL_KEYS}
    out.update({k: u["s"][k] for k in TIME_KEYS})
    out["remind_offsets"] = parse_offsets(u["s"]["remind_offsets"])
    out.update({"tz": u["tz"], "tz_auto": bool(u["tz_auto"])})
    return out


def validate_settings_patch(patch: dict) -> dict:
    clean = {}
    for k, v in patch.items():
        if k in BOOL_KEYS or k == "tz_auto":
            if not isinstance(v, (bool, int)) or v not in (0, 1, True, False):
                raise DomainError(400, f"{k}: ожидается да/нет")
            clean[k] = int(bool(v))
        elif k in TIME_KEYS:
            clean[k] = parse_time(v, "Время")
        elif k == "remind_offsets":
            if not isinstance(v, list) or any(isinstance(x, bool) or not isinstance(x, int) for x in v):
                raise DomainError(400, "Напоминания: ожидается список минут")
            if any(not 1 <= x <= MAX_REMIND_MINUTES for x in v):
                raise DomainError(400, "Напоминание: от 1 минуты до 24 часов до начала задачи")
            uniq = sorted(set(v), reverse=True)
            if len(uniq) > MAX_REMIND_OFFSETS:
                raise DomainError(400, f"Не больше {MAX_REMIND_OFFSETS} напоминаний")
            clean[k] = ",".join(str(x) for x in uniq)
        elif k == "tz":
            name = str(v).strip()
            try:
                ZoneInfo(name)
            except Exception:
                raise DomainError(400, "Неизвестный часовой пояс. Пример: Europe/Moscow, Asia/Yekaterinburg")
            clean[k] = name
        else:
            raise DomainError(400, f"Неизвестная настройка: {k}")
    if "tz" in clean and "tz_auto" not in clean:
        clean["tz_auto"] = 0  # выбрал вручную — больше не подменяем поясом устройства
    return clean


async def update_notification_settings(user_id: int, patch: dict) -> dict:
    clean = validate_settings_patch(patch)
    async with notif_db() as db:
        old = await load_user(db, user_id)
        if not old: raise DomainError(404, "Пользователь не найден — нажмите /start в боте")
        now = iso(utcnow())
        sets = {k: v for k, v in clean.items() if k in SETTING_KEYS}
        if sets:
            await db.execute("UPDATE notification_settings SET " + ", ".join(f"{k} = ?" for k in sets) + ", updated_at = ? "
                             "WHERE user_id = ?", (*sets.values(), now, user_id))
        usets = {k: v for k, v in clean.items() if k in ("tz", "tz_auto")}
        if usets:
            await db.execute("UPDATE users SET " + ", ".join(f"{k} = ?" for k in usets) + ", updated_at = ? WHERE user_id = ?",
                             (*usets.values(), now, user_id))
        await db.commit()
        new = await load_user(db, user_id)
        await reschedule_user(db, old, new)
    return settings_view(new)


async def reschedule_user(db, old: dict, new: dict):
    """Пересчёт после смены настроек: отменяем ставшие неактуальными ожидающие отправки и заново оцениваем
    сегодняшние сводки (чтобы ни старая отправка, ни «пропуск» не блокировали новое время)."""
    changed = {k for k in SETTING_KEYS if old["s"][k] != new["s"][k]}
    if old["tz"] != new["tz"]: changed.add("tz")
    await cancel_invalid_pending(db, user_id=new["user_id"])
    now = utcnow()
    today = utc_to_local(now, new["zone"]).date()
    touched = [k for k, (on_k, time_k) in DIGESTS.items() if changed & {on_k, time_k, "tz", "enabled"}]
    for kind in touched:
        await db.execute("DELETE FROM notification_log WHERE dedup_key = ? AND status IN ('skipped', 'cancelled')",
                         (digest_key(kind, new["user_id"], today.isoformat()),))
    await db.commit()
    if touched: await skip_past_digests(db, new, touched, now)


def digest_key(kind: str, user_id: int, day: str) -> str:
    return f"{kind}:{user_id}:{day}"


async def skip_past_digests(db, u: dict, kinds: list, now: datetime):
    """Сводка, время которой на сегодня уже прошло к моменту включения/смены настройки, не отправляется «задним числом»."""
    if not u: return
    local = utc_to_local(now, u["zone"])
    for kind in kinds:
        on_k, time_k = DIGESTS[kind]
        if not (u["s"]["enabled"] and u["s"][on_k]): continue
        fire = at_clock(local.date(), u["s"][time_k])
        if fire <= local:
            ts = iso(now)
            await db.execute(
                "INSERT OR IGNORE INTO notification_log (dedup_key, user_id, chat_id, kind, occ_day, fire_at, status, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'skipped', ?, ?, ?)",
                (digest_key(kind, u["user_id"], local.date().isoformat()), u["user_id"], u["chat_id"], kind,
                 local.date().isoformat(), iso(local_to_utc(fire, u["zone"])),
                 "время на сегодня уже прошло к моменту включения или смены настройки", ts, ts))
    await db.commit()


# --- определение пользователя в API ---
def verify_init_data(init_data: str) -> dict:
    """Проверка подписи Telegram WebApp initData (HMAC-SHA256 от токена бота). Возвращает объект user."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
            raise ValueError("bad hash")
        age = _time.time() - int(pairs["auth_date"])
        user = json.loads(pairs["user"])
        int(user["id"])
    except Exception:
        raise DomainError(401, "Не удалось подтвердить пользователя Telegram — откройте приложение из бота")
    if age > INIT_DATA_MAX_AGE:
        raise DomainError(401, "Сессия устарела — закройте и откройте приложение заново")
    return user


async def api_user(request: Request) -> dict:
    init = request.headers.get("X-Telegram-Init-Data")
    async with notif_db() as db:
        if init:
            tg = verify_init_data(init)
            uid = int(tg["id"])
            u = await load_user(db, uid) or await ensure_user(db, uid, uid)  # в личном чате chat_id == user_id
        else:
            if not ALLOW_NO_AUTH: raise DomainError(401, "Откройте приложение из Telegram")
            async with db.execute("SELECT user_id FROM users ORDER BY created_at, user_id LIMIT 1") as cur:
                r = await cur.fetchone()
            if not r: raise DomainError(409, "Сначала нажмите /start в Telegram-боте — тогда можно настроить уведомления")
            u = await load_user(db, r[0])
        # пояс устройства подхватываем автоматически, пока пользователь не выбрал свой вручную
        tz_hdr = (request.headers.get("X-Timezone") or "").strip()
        if u["tz_auto"] and tz_hdr and tz_hdr != u["tz"]:
            try:
                ZoneInfo(tz_hdr)
                await db.execute("UPDATE users SET tz = ?, updated_at = ? WHERE user_id = ?", (tz_hdr, iso(utcnow()), u["user_id"]))
                await db.commit()
                new = await load_user(db, u["user_id"])
                await reschedule_user(db, u, new)
                u = new
            except Exception:
                pass
        return u


# =====================================================================
#  Актуальность отправки — единая проверка
# =====================================================================
def check_row(row, u: dict | None, task, now: datetime) -> tuple[bool, str]:
    """Можно ли отправить это уведомление ПРЯМО СЕЙЧАС. row — запись журнала (или кандидат с теми же ключами),
    u — пользователь с настройками, task — строка tasks или None, now — UTC. Возвращает (ok, причина отказа)."""
    kind = row["kind"]
    if kind == "test": return True, ""
    if not u or not u["active"]: return False, "пользователь недоступен (бот остановлен или не запущен)"
    s = u["s"]
    if not s["enabled"]: return False, "уведомления выключены"
    local = utc_to_local(now, u["zone"])
    day = row["occ_day"]

    if kind == "habit":  # task здесь — строка привычки (+ done_today)
        habit = task
        if not habit: return False, "привычка удалена"
        if habit["user_id"] != u["user_id"]: return False, "привычка принадлежит другому пользователю"
        if not habit["remind_time"]: return False, "напоминание выключено"
        if habit["remind_time"] != row["task_time"]: return False, "время напоминания изменено"
        if local.date().isoformat() != day: return False, "день уже закончился"
        d = date.fromisoformat(day)
        days = habit_days_list(habit["remind_days"])
        if days and d.weekday() not in days: return False, "привычка не запланирована на этот день недели"
        fire = local_to_utc(at_clock(d, habit["remind_time"]), u["zone"])
        if iso(fire) != row["fire_at"]: return False, "время отправки изменено"
        if now >= fire + HABIT_GRACE: return False, "время отправки прошло"
        if habit.get("done_today"): return False, "привычка уже отмечена сегодня"
        return True, ""

    if kind in DIGESTS:
        on_k, time_k = DIGESTS[kind]
        if not s[on_k]: return False, "этот вид уведомлений выключен"
        if local.date().isoformat() != day: return False, "день уже закончился"
        fire = local_to_utc(at_clock(date.fromisoformat(day), s[time_k]), u["zone"])
        if iso(fire) != row["fire_at"]: return False, "время отправки изменено"
        if now >= fire + DIGEST_GRACE: return False, "время отправки прошло"
        return True, ""

    if kind in ("remind", "snooze"):
        off = row["offset_min"]
        offs = offsets_of(u)
        if not task: return False, "задача удалена"
        if task["user_id"] != u["user_id"]: return False, "задача принадлежит другому пользователю"
        if task["remind_min"] == -1: return False, "для задачи отключены напоминания"
        if not task["due_time"]: return False, "у задачи нет времени"
        if kind == "remind":
            if off not in offs: return False, f"напоминание за {off} мин выключено"
            if task["due_time"] != row["task_time"]: return False, "время задачи изменено"
        if not occurs(task, day): return False, "задача перенесена или не запланирована на эту дату"
        if is_done(task, day): return False, "задача уже выполнена"
        if kind == "snooze":
            return (True, "") if local.date().isoformat() == day else (False, "день уже закончился")
        start = at_clock(date.fromisoformat(day), task["due_time"])
        if local >= start: return False, "время начала задачи уже наступило"
        for smaller in offs:  # более позднее напоминание уже открылось — это устарело
            if smaller < off and start - timedelta(minutes=smaller) <= local:
                return False, "уже наступило время более позднего напоминания"
        return True, ""
    return False, "неизвестный тип уведомления"


def reminder_candidates(u: dict, tasks, now: datetime):
    """Напоминания, окно которых уже открыто: [старт − отступ, старт). Сегодняшние и завтрашние повторы
    (напоминание за 30 минут до 00:10 наступает ещё «вчера»)."""
    s = u["s"]
    if not s["enabled"]: return
    offsets = offsets_of(u)
    if not offsets: return
    tz = u["zone"]
    local = utc_to_local(now, tz)
    for r in tasks:
        if r["user_id"] != u["user_id"] or not r["due_time"] or r["remind_min"] == -1: continue
        for d in (local.date(), local.date() + timedelta(days=1)):
            day = d.isoformat()
            try:
                if not occurs(r, day) or is_done(r, day): continue
                start = at_clock(d, r["due_time"])
            except (ValueError, TypeError):
                continue
            if local >= start: continue
            for off in offsets:
                fire_local = start - timedelta(minutes=off)
                if fire_local > local: continue
                yield {"kind": "remind", "user_id": u["user_id"], "task_id": r["id"], "occ_day": day,
                       "task_time": r["due_time"], "offset_min": off, "title": r["text"],
                       "fire_at": iso(local_to_utc(fire_local, tz)),
                       "dedup_key": f"remind:{u['user_id']}:{r['id']}:{day}:{r['due_time']}:{off}"}


def habit_candidates(u: dict, habits, done: set, now: datetime):
    """Привычки, время напоминания которых наступило сегодня (по часам пользователя): [время, время + 30 мин).
    done — множество (habit_id, день) уже отмеченных; отмеченную привычку не напоминаем."""
    s = u["s"]
    if not s["enabled"]: return
    local = utc_to_local(now, u["zone"])
    d = local.date()
    day = d.isoformat()
    for h in habits:
        if h["user_id"] != u["user_id"] or not h["remind_time"]: continue
        days = habit_days_list(h["remind_days"])
        if days and d.weekday() not in days: continue
        try:
            fire = local_to_utc(at_clock(d, h["remind_time"]), u["zone"])
        except (ValueError, TypeError):
            continue
        if not (fire <= now < fire + HABIT_GRACE): continue
        if h["remind_set_at"] and iso(fire) <= h["remind_set_at"]: continue  # время настроено уже после него
        if (h["id"], day) in done: continue
        yield {"kind": "habit", "user_id": u["user_id"], "task_id": None, "habit_id": h["id"], "occ_day": day,
               "task_time": h["remind_time"], "offset_min": None, "title": h["text"], "fire_at": iso(fire),
               "dedup_key": f"habit:{u['user_id']}:{h['id']}:{day}:{h['remind_time']}"}


def digest_candidates(u: dict, now: datetime):
    s = u["s"]
    if not s["enabled"]: return
    local = utc_to_local(now, u["zone"])
    for kind, (on_k, time_k) in DIGESTS.items():
        if not s[on_k]: continue
        fire = local_to_utc(at_clock(local.date(), s[time_k]), u["zone"])
        if fire <= now < fire + DIGEST_GRACE:
            day = local.date().isoformat()
            yield {"kind": kind, "user_id": u["user_id"], "task_id": None, "occ_day": day, "task_time": None,
                   "offset_min": None, "title": None, "fire_at": iso(fire), "dedup_key": digest_key(kind, u["user_id"], day)}


# =====================================================================
#  Журнал: захват, доставка, ошибки
# =====================================================================
async def claim(db, cand: dict, chat_id: int) -> int | None:
    """Резервирует отправку. Возвращает id, если ЭТОТ вызов получил право отправить (первый раз или после
    пересчёта), иначе None — такая отправка уже есть в журнале (отправлена, ждёт, провалена или пропущена)."""
    now = iso(utcnow())
    cur = await db.execute(
        "INSERT OR IGNORE INTO notification_log (dedup_key, user_id, chat_id, kind, task_id, habit_id, occ_day, task_time, offset_min, title, "
        "fire_at, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (cand["dedup_key"], cand["user_id"], chat_id, cand["kind"], cand["task_id"], cand.get("habit_id"), cand["occ_day"],
         cand["task_time"], cand["offset_min"], cand["title"], cand["fire_at"], now, now))
    if cur.rowcount == 1:
        await db.commit()
        return cur.lastrowid
    # запись была отменена при пересчёте расписания, а теперь снова актуальна — оживляем
    cur = await db.execute(
        "UPDATE notification_log SET status = 'pending', attempts = 0, next_attempt_at = NULL, detail = NULL, chat_id = ?, "
        "fire_at = ?, title = ?, updated_at = ? WHERE dedup_key = ? AND status = 'cancelled'",
        (chat_id, cand["fire_at"], cand["title"], now, cand["dedup_key"]))
    await db.commit()
    if cur.rowcount == 0: return None
    async with db.execute("SELECT id FROM notification_log WHERE dedup_key = ?", (cand["dedup_key"],)) as c2:
        return (await c2.fetchone())[0]


async def _finish(db, log_id: int, status: str, detail: str | None = None, **extra):
    sets = {"status": status, "detail": detail, "updated_at": iso(utcnow()), **extra}
    await db.execute("UPDATE notification_log SET " + ", ".join(f"{k} = ?" for k in sets) + " WHERE id = ?",
                     (*sets.values(), log_id))
    await db.commit()


async def _retry_or_fail(db, log_id: int, attempts: int, err: str, delay: int | None = None):
    if attempts >= MAX_SEND_ATTEMPTS:
        return await _finish(db, log_id, "failed", f"исчерпаны попытки ({attempts}): {err}")
    wait = delay if delay is not None else RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
    await _finish(db, log_id, "pending", f"попытка {attempts} не удалась: {err}",
                  next_attempt_at=iso(utcnow() + timedelta(seconds=wait)))


async def fetch_task_row(db, task_id, uid: int | None = None):
    """Строка задачи или None. uid задан — только если задача этого пользователя (для кнопок и API)."""
    if task_id is None: return None
    if uid is None:  # внутренние проверки доставки: владельца сверяет check_row
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
            return await cur.fetchone()
    async with db.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid)) as cur:
        return await cur.fetchone()


async def fetch_habit_row(db, habit_id, day: str | None = None):
    """Привычка (dict) с флагом done_today — отмечена ли она в день day. None, если удалена."""
    if habit_id is None: return None
    async with db.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)) as cur:
        r = await cur.fetchone()
    if not r: return None
    h = dict(r)
    async with db.execute("SELECT 1 FROM habit_logs WHERE habit_id = ? AND day = ?", (habit_id, day or "")) as cur:
        h["done_today"] = await cur.fetchone() is not None
    return h


async def fetch_subject(db, row):
    """К чему относится запись журнала: привычка для kind='habit', иначе задача (или None для сводок)."""
    if row["kind"] == "habit": return await fetch_habit_row(db, row["habit_id"], row["occ_day"])
    return await fetch_task_row(db, row["task_id"])


async def habit_changed(uid: int):
    """Хук для мест, где меняется или удаляется привычка. Никогда не бросает исключений."""
    try:
        async with notif_db() as db:
            await cancel_invalid_pending(db, user_id=uid)
    except Exception:
        log.exception("Не удалось пересчитать расписание напоминаний о привычках")


async def deliver(db, log_id: int) -> str:
    """Одна попытка отправки записи журнала. Возвращает итоговый статус записи."""
    now = utcnow()
    cur = await db.execute("UPDATE notification_log SET status = 'sending', attempts = attempts + 1, updated_at = ? "
                           "WHERE id = ? AND status = 'pending'", (iso(now), log_id))
    await db.commit()
    if cur.rowcount == 0: return "busy"  # уже обрабатывается или завершена — повторно не шлём
    async with db.execute("SELECT * FROM notification_log WHERE id = ?", (log_id,)) as c:
        row = await c.fetchone()
    attempts = row["attempts"]
    try:
        u = await load_user(db, row["user_id"])
        task = await fetch_subject(db, row)
        ok, reason = check_row(row, u, task, now)  # актуальные дата, время и статус — перед КАЖДОЙ отправкой
        if not ok:
            await _finish(db, log_id, "cancelled", reason)
            return "cancelled"
        built = await build_message(db, row, u, task, now)
    except Exception as e:
        log.exception("Не удалось подготовить уведомление %s", log_id)
        await _retry_or_fail(db, log_id, attempts, f"ошибка подготовки: {e!r}")
        return "pending"
    if built is None:
        await _finish(db, log_id, "skipped", "нечего отправлять")
        return "skipped"
    text, kb = built
    try:
        msg = await bot.send_message(u["chat_id"] if u else row["chat_id"], text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramRetryAfter as e:
        await _retry_or_fail(db, log_id, attempts, f"Telegram просит подождать {e.retry_after} с", delay=int(e.retry_after) + 1)
        return "pending"
    except TelegramForbiddenError as e:
        await _finish(db, log_id, "failed", f"Telegram: доступ запрещён ({e.message}). Уведомления отключены до нового /start")
        await db.execute("UPDATE users SET active = 0, updated_at = ? WHERE user_id = ?", (iso(utcnow()), row["user_id"]))
        await db.commit()
        return "failed"
    except TelegramBadRequest as e:
        await _finish(db, log_id, "failed", f"Telegram отклонил сообщение: {e.message}")
        return "failed"
    except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError) as e:
        await _retry_or_fail(db, log_id, attempts, f"сеть/сервер Telegram: {e!s}"[:300])
        return "pending"
    except TelegramAPIError as e:
        await _finish(db, log_id, "failed", f"Ошибка Telegram API: {e.message}"[:300])
        return "failed"
    except Exception as e:  # неизвестная ошибка клиента — пробуем ещё, но в пределах лимита попыток
        log.exception("Неожиданная ошибка отправки %s", log_id)
        await _retry_or_fail(db, log_id, attempts, f"неожиданная ошибка: {e!r}"[:300])
        return "pending"
    await _finish(db, log_id, "sent", None, sent_at=iso(utcnow()), tg_message_id=msg.message_id, kb_active=1 if kb else 0)
    return "sent"


async def cancel_invalid_pending(db, user_id: int | None = None, task_id: int | None = None) -> int:
    """Пересчёт расписания: ожидающие отправки, которые больше не актуальны (задачу перенесли, выполнили, удалили,
    поменяли время/настройки), помечаются «cancelled». Их ключи останутся в журнале, но новые ключи не блокируют."""
    q, args = "SELECT * FROM notification_log WHERE status = 'pending'", []
    if user_id is not None: q += " AND user_id = ?"; args.append(user_id)
    if task_id is not None: q += " AND task_id = ?"; args.append(task_id)
    async with db.execute(q, args) as cur:
        rows = await cur.fetchall()
    now, users, n = utcnow(), {}, 0
    for row in rows:
        if row["user_id"] not in users: users[row["user_id"]] = await load_user(db, row["user_id"])
        ok, reason = check_row(row, users[row["user_id"]], await fetch_subject(db, row), now)
        if not ok:
            await db.execute("UPDATE notification_log SET status = 'cancelled', detail = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                             (f"расписание пересчитано: {reason}", iso(now), row["id"]))
            n += 1
    await db.commit()
    return n


# =====================================================================
#  Тексты и клавиатуры
# =====================================================================
def reminder_kb(task_id: int, day: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"rm_ok:{task_id}:{day}"),
        InlineKeyboardButton(text="➡️ Перенести", callback_data=f"rm_mv:{task_id}:{day}")]])


def move_menu_kb(task, day: str, today: str) -> InlineKeyboardMarkup:
    """Что предлагает «Перенести» — зависит от типа задачи (разовую переносим, повторяющуюся можно только отложить)."""
    t, d = task["id"], day
    rows = []
    if task["repeat_type"] != "none":
        rows.append([InlineKeyboardButton(text="🔕 Через 30 мин", callback_data=f"rm_z30:{t}:{d}"),
                     InlineKeyboardButton(text="🔕 Через 1 час", callback_data=f"rm_z60:{t}:{d}")])
    else:
        late = planned_of(task) < today
        if task["due_time"] and not late:
            rows.append([InlineKeyboardButton(text="⏱ +30 мин", callback_data=f"rm_p30:{t}:{d}"),
                         InlineKeyboardButton(text="⏱ +1 час", callback_data=f"rm_p60:{t}:{d}")])
        row = []
        if late: row.append(InlineKeyboardButton(text="Сегодня", callback_data=f"rm_td:{t}:{d}"))
        row += [InlineKeyboardButton(text="Завтра", callback_data=f"rm_tom:{t}:{d}"),
                InlineKeyboardButton(text="📅 Дата…", callback_data=f"rm_dt:{t}:{d}")]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="‹ Назад", callback_data=f"rm_bk:{t}:{d}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


LEVEL_MARK = {3: "🔴 ", 2: "", 1: ""}


async def build_message(db, row, u: dict, task, now: datetime):
    """(текст, клавиатура) для отправки либо None, если слать нечего. Строится из АКТУАЛЬНОГО состояния БД."""
    kind = row["kind"]
    local = utc_to_local(now, u["zone"]) if u else utc_to_local(now, LOCAL_TZ)
    if kind == "test":
        return "🔔 <b>Тест уведомлений</b>\nЕсли вы это видите — доставка работает.", None
    if kind == "habit":  # тип «обычное»
        return f'📚 Время "{html.escape(task["text"])}"!', None
    if kind in ("remind", "snooze"):
        day = row["occ_day"]
        start = at_clock(date.fromisoformat(day), task["due_time"])
        mins = max(1, math.ceil((start - local).total_seconds() / 60))
        when = task["due_time"] + ("" if day == local.date().isoformat() else " · завтра")
        head = f"⏰ <b>Через {ru_minutes(mins)}</b>" if kind == "remind" else f"🔕 <b>Напоминание</b> · начало через {ru_minutes(mins)}"
        text = f"{head}\n{LEVEL_MARK.get(task['level'], '')}<b>{html.escape(task['text'])}</b>\n🕒 {when}"
        return text, reminder_kb(task["id"], day)
    return await build_digest(db, kind, u, local.date().isoformat())


def _task_line(i: int, t: dict, with_plan_note: bool = False) -> str:
    name = html.escape(t["text"])
    bits = []
    if t["time"]: bits.append(t["time"])
    if with_plan_note and t["overdue"]: bits.append(f"план был {short_date(t['planned'])}")
    if t["past_due"] and t["due"]: bits.append(f"срок {short_date(t['due'])} прошёл")
    if t["repeat"] != "none": bits.append("🔁")
    mark = "⚠️" if (t["overdue"] or t["past_due"]) else "▫️"
    return f"{mark} {i}. {LEVEL_MARK.get(t['level'], '')}{name}" + (f" — {' · '.join(bits)}" if bits else "")


def _digest_rows(tasks: list[dict], day: str) -> list:
    rows = []
    for i, t in enumerate(tasks, 1):
        row = [InlineKeyboardButton(text=f"✅ {i}", callback_data=f"rm_ok:{t['id']}:{day}")]
        if t["repeat"] == "none":
            row.append(InlineKeyboardButton(text=f"➡️ {i}", callback_data=f"rm_mv:{t['id']}:{day}"))
        rows.append(row)
    return rows


async def build_digest(db, kind: str, u: dict, today: str, refresh: bool = False):
    """Утренний план / вечерний итог / сводка просроченных. refresh=True — пересборка уже отправленного сообщения
    (вместо None возвращает «всё сделано»). Выполненные задачи в списках не показываются."""
    tasks = await list_tasks(db, today, "today", u["user_id"])
    d = date.fromisoformat(today)
    title_day = f"{d.day} {MONTHS_GEN[d.month - 1]}"
    open_all = [t for t in tasks if not t["completed"]]
    late = [t for t in open_all if t["overdue"] or t["past_due"]]
    shown_limit = PLAN_LIMIT

    if kind == "morning":
        items = sorted([t for t in open_all if not (t["overdue"] or t["past_due"])],
                       key=lambda t: (t["time"] is None, t["time"] or "", -t["level"]))
        async with db.execute("SELECT title, time, emoji FROM events WHERE date = ? AND user_id = ? ORDER BY COALESCE(time, '99:99'), id",
                              (today, u["user_id"])) as cur:
            events = await cur.fetchall()
        if not items and not events and not refresh: return None
        lines = [f"☀️ <b>Доброе утро! План на {title_day}</b>", ""]
        if items:
            lines += [_task_line(i, t) for i, t in enumerate(items[:shown_limit], 1)]
            if len(items) > shown_limit: lines.append(f"…и ещё {len(items) - shown_limit} в приложении")
        else:
            lines.append("Задач на сегодня нет 🌿" if not refresh else "Все задачи на сегодня выполнены 🎉")
        if events:
            lines += ["", "<b>События</b>"] + [f"{e['emoji']} {(e['time'] + ' — ') if e['time'] else ''}{html.escape(e['title'])}" for e in events]
        if late and u["s"]["overdue_on"]:
            lines += ["", f"⚠️ Просроченных задач: {len(late)} — сводка придёт в {u['s']['overdue_time']}"]
        rows = _digest_rows(items[:shown_limit], today)
        return "\n".join(lines), (InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

    if kind == "overdue":
        items = sorted(late, key=lambda t: (t["planned"], t["time"] or "", -t["level"]))
        if not items and not refresh: return None
        if not items: return "✅ <b>Просроченных задач больше нет</b>", None
        lines = [f"⚠️ <b>Просроченные задачи ({len(items)})</b>", ""]
        lines += [_task_line(i, t, with_plan_note=True) for i, t in enumerate(items[:shown_limit], 1)]
        if len(items) > shown_limit: lines.append(f"…и ещё {len(items) - shown_limit} в приложении")
        rows = _digest_rows(items[:shown_limit], today)
        n_carry = sum(1 for t in items if t["overdue"])
        if n_carry: rows.append([InlineKeyboardButton(text=f"⏩ Всё на сегодня ({n_carry})", callback_data=f"rm_carry:0:{today}")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    if kind == "evening":
        async with db.execute("SELECT * FROM tasks WHERE user_id = ?", (u["user_id"],)) as cur: all_rows = await cur.fetchall()
        planned = sum(1 for r in all_rows if occurs(r, today))
        async with db.execute("SELECT COUNT(*) FROM completions WHERE day = ? AND user_id = ?", (today, u["user_id"])) as cur:
            done = (await cur.fetchone())[0]
        total = max(planned, done)
        if total == 0 and not open_all and not refresh: return None
        pct = round(done / total * 100) if total else 0
        lines = [f"🌙 <b>Итог дня — {title_day}</b>", "", f"<blockquote>Выполнено: {done} из {total} ({pct}%)</blockquote>"]
        items = sorted(open_all, key=lambda t: (t["time"] is None, t["time"] or "", -t["level"]))
        rows = []
        if items:
            lines += ["", "<b>Не завершено:</b>"] + [_task_line(i, t, with_plan_note=True) for i, t in enumerate(items[:shown_limit], 1)]
            if len(items) > shown_limit: lines.append(f"…и ещё {len(items) - shown_limit} в приложении")
            rows = _digest_rows(items[:shown_limit], today)
            n_push = sum(1 for t in items if t["repeat"] == "none")
            if n_push: rows.append([InlineKeyboardButton(text=f"➡️ Всё на завтра ({n_push})", callback_data=f"rm_alltom:0:{today}")])
        else:
            lines += ["", "🎉 Все задачи на сегодня выполнены!"]
        return "\n".join(lines), (InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    return None


# =====================================================================
#  Действия из кнопок (общая логика для бота; приложение работает через обычные API задач)
# =====================================================================
async def push_unfinished_to_tomorrow_db(db, today: str, tomorrow: str, uid: int) -> int:
    cur = await db.execute(
        "UPDATE tasks SET planned_date = ?, reminded_on = NULL WHERE repeat_type = 'none' AND done_on IS NULL AND user_id = ? "
        "AND COALESCE(planned_date, created_on, '2000-01-01') <= ?", (tomorrow, uid, today))
    await db.commit()
    if cur.rowcount: await task_changed(None, uid)
    return cur.rowcount


async def create_snooze(db, u: dict, task_id: int, day: str, minutes: int) -> None:
    now = utcnow()
    fire = now + timedelta(minutes=minutes)
    ts = iso(now)
    row = await fetch_task(db, task_id, u["user_id"])
    await db.execute(
        "INSERT OR IGNORE INTO notification_log (dedup_key, user_id, chat_id, kind, task_id, occ_day, task_time, title, fire_at, "
        "status, created_at, updated_at) VALUES (?, ?, ?, 'snooze', ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (f"snooze:{u['user_id']}:{task_id}:{day}:{int(fire.replace(tzinfo=timezone.utc).timestamp())}", u["user_id"], u["chat_id"],
         task_id, day, row["due_time"], row["text"], iso(fire), ts, ts))
    await db.commit()


async def reminder_action(u: dict, action: str, task_id: int, day: str) -> tuple[str, bool]:
    """Выполняет действие кнопки. Возвращает (текст всплывающего ответа, показывать ли как предупреждение).
    Бросает DomainError для понятных отказов."""
    now = utcnow()
    local = utc_to_local(now, u["zone"])
    today = local.date().isoformat()
    tomorrow = (local.date() + timedelta(days=1)).isoformat()
    uid = u["user_id"]
    async with notif_db() as db:
        if action == "carry":
            n = await carry_over_db(db, today, uid)
            return (f"Перенесено на сегодня: {n}" if n else "Просроченных задач нет"), False
        if action == "alltom":
            n = await push_unfinished_to_tomorrow_db(db, today, tomorrow, uid)
            return (f"Перенесено на завтра: {n}" if n else "Переносить нечего"), False
        row = await fetch_task(db, task_id, uid)  # 404 → «Задача не найдена» (в том числе для чужой)
        if action == "ok":
            if row["repeat_type"] != "none" and day != today:
                raise DomainError(400, f"Повтор запланирован на {short_date(day)} — отметить можно в этот день")
            return ("Выполнено ✅", False) if await complete_task_db(db, task_id, today, uid) else ("Уже выполнено ✅", False)
        if action in ("p30", "p60"):
            d, t = await shift_task_time_db(db, task_id, 30 if action == "p30" else 60, local, uid)
            return f"Перенесено на {t}" + ("" if d == today else f" · {short_date(d)}"), False
        if action in ("tom", "td"):
            to = tomorrow if action == "tom" else today
            if row["done_on"] is not None and row["repeat_type"] == "none": raise DomainError(409, "Задача уже выполнена")
            if planned_of(row) == to: return ("Уже на завтра" if action == "tom" else "Уже на сегодня"), False
            await move_task_db(db, task_id, to, today, uid)
            return ("Перенесено на завтра" if action == "tom" else "Перенесено на сегодня"), False
        if action in ("z30", "z60"):
            if row["repeat_type"] == "none": raise DomainError(400, "Для разовой задачи используйте перенос")
            if not row["due_time"] or not occurs(row, today) or is_done(row, today):
                raise DomainError(409, "Откладывать нечего — задача на сегодня не активна")
            m = 30 if action == "z30" else 60
            await create_snooze(db, u, task_id, today, m)
            return f"Напомню через {m} мин", False
    raise DomainError(400, "Неизвестное действие")


# =====================================================================
#  Синхронизация уже отправленных сообщений с состоянием задач
# =====================================================================
async def safe_edit(chat_id, message_id, text: str, kb):
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=kb, parse_mode=ParseMode.HTML)
        return True
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            log.info("Не удалось обновить сообщение %s: %s", message_id, e)
    except TelegramRetryAfter:
        log.info("Telegram просит подождать — сообщение %s обновится при следующем изменении", message_id)
    except (TelegramAPIError, asyncio.TimeoutError) as e:
        log.warning("Ошибка обновления сообщения %s: %r", message_id, e)
    return False


async def reminder_view(task, row, today: str):
    """Актуальный вид сообщения-напоминания: (текст, клавиатура, состояние ok|done|moved|changed|deleted)."""
    if not task: return "🗑 <b>Задача удалена</b>", None, "deleted"
    name, day = html.escape(task["text"]), row["occ_day"]
    if is_done(task, day) or (task["repeat_type"] != "none" and task["done_on"] == today):
        return f"✅ <s>{name}</s> — выполнено", None, "done"
    if task["repeat_type"] == "none" and (planned_of(task) != day or task["due_time"] != row["task_time"]):
        when = short_date(planned_of(task)) + (f", {task['due_time']}" if task["due_time"] else "")
        return f"➡️ <b>{name}</b> — перенесено: {when}", None, "moved"
    if not occurs(task, day) or task["due_time"] != row["task_time"]:
        return f"🔄 <b>{name}</b> — расписание изменилось", None, "changed"
    return None, None, "ok"


async def refresh_messages(task_id: int | None, uid: int):
    """Приводит отправленные сообщения в соответствие с БД после изменения задачи из приложения или бота:
    убирает кнопки у устаревших напоминаний и пересобирает сводки. Все ошибки гасятся — это best-effort."""
    try:
        async with notif_db() as db:
            cutoff = iso(utcnow() - KB_EDIT_WINDOW)
            q = "SELECT * FROM notification_log WHERE status = 'sent' AND kb_active = 1 AND sent_at >= ? AND tg_message_id IS NOT NULL AND user_id = ?"
            args = [cutoff, uid]
            if task_id is not None:
                q += " AND (task_id = ? OR task_id IS NULL)"
                args.append(task_id)
            async with db.execute(q, args) as cur: rows = await cur.fetchall()
            users = {}
            for row in rows:
                if row["user_id"] not in users: users[row["user_id"]] = await load_user(db, row["user_id"])
                u = users[row["user_id"]]
                if not u: continue
                try:
                    today = utc_to_local(utcnow(), u["zone"]).date().isoformat()
                    if row["kind"] in ("remind", "snooze"):
                        text, kb, state = await reminder_view(await fetch_task_row(db, row["task_id"]), row, today)
                        if state == "ok": continue
                    elif row["kind"] in DIGESTS:
                        if row["occ_day"] != today: continue
                        text, kb = await build_digest(db, row["kind"], u, today, refresh=True)
                    else:
                        continue
                    await safe_edit(row["chat_id"], row["tg_message_id"], text, kb)
                    if kb is None:
                        await db.execute("UPDATE notification_log SET kb_active = 0, updated_at = ? WHERE id = ?", (iso(utcnow()), row["id"]))
                        await db.commit()
                except Exception:
                    log.exception("Не удалось обновить сообщение уведомления %s", row["id"])
    except Exception:
        log.exception("Не удалось синхронизировать сообщения уведомлений")


async def task_changed(task_id: int | None, uid: int):
    """Хук для всех мест, где меняется задача (API и бот). Никогда не бросает исключений.
    task_id=None — изменилось много задач сразу. uid — владелец: чужие сообщения не трогаем."""
    try:
        async with notif_db() as db:
            await cancel_invalid_pending(db, user_id=uid, task_id=task_id)
    except Exception:
        log.exception("Не удалось пересчитать расписание уведомлений")
    _spawn(refresh_messages(task_id, uid))


# =====================================================================
#  Планировщик
# =====================================================================
async def recover_stuck(db, now: datetime, all_: bool = False):
    """Записи «sending» остаются после падения посреди отправки. Неизвестно, дошло ли сообщение, поэтому, чтобы не
    дублировать, повторно не шлём: помечаем как failed с понятной причиной."""
    cutoff = iso(now) if all_ else iso(now - STUCK_SENDING_AFTER)
    cur = await db.execute("UPDATE notification_log SET status = 'failed', detail = ?, updated_at = ? "
                           "WHERE status = 'sending' AND updated_at <= ?",
                           ("отправка прервана перезапуском или сбоем; повторно не отправляется, чтобы не дублировать", iso(now), cutoff))
    await db.commit()
    return cur.rowcount


async def process_pending(db, now: datetime):
    async with db.execute("SELECT id FROM notification_log WHERE status = 'pending' AND fire_at <= ? "
                          "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY fire_at LIMIT 50",
                          (iso(now), iso(now))) as cur:
        ids = [r[0] for r in await cur.fetchall()]
    for i in ids:
        try:
            await deliver(db, i)
        except Exception:
            log.exception("Сбой доставки записи %s", i)


async def reminder_tick(now: datetime | None = None):
    async with _tick_lock_get():
        now = now or utcnow()
        async with notif_db() as db:
            await recover_stuck(db, now)
            users = await load_active_users(db)
            if users:
                async with db.execute("SELECT * FROM tasks") as cur: tasks = await cur.fetchall()
                async with db.execute("SELECT * FROM habits WHERE remind_time IS NOT NULL") as cur:
                    habits = [dict(r) for r in await cur.fetchall()]
                since = (now.date() - timedelta(days=2)).isoformat()
                async with db.execute("SELECT habit_id, day FROM habit_logs WHERE day >= ?", (since,)) as cur:
                    done = {(r["habit_id"], r["day"]) for r in await cur.fetchall()}
                hmap = {h["id"]: h for h in habits}
                tmap = {r["id"]: r for r in tasks}

                def subject(c):
                    if c["kind"] != "habit": return tmap.get(c["task_id"])
                    h = hmap.get(c["habit_id"])
                    return None if h is None else {**h, "done_today": (h["id"], c["occ_day"]) in done}

                for u in users:
                    try:
                        cands = (list(reminder_candidates(u, tasks, now)) + list(digest_candidates(u, now))
                                 + list(habit_candidates(u, habits, done, now)))
                        for c in cands:
                            ok, _ = check_row(c, u, subject(c), now)
                            if not ok: continue
                            log_id = await claim(db, c, u["chat_id"])
                            if log_id: await deliver(db, log_id)
                    except Exception:
                        log.exception("Сбой обработки уведомлений пользователя %s", u["user_id"])
                await process_pending(db, now)


async def recover_after_restart():
    """После перезапуска расписание выводится из БД само; здесь только убираем хвосты прерванных отправок
    и отменяем то, что успело устареть, пока приложение было выключено."""
    async with notif_db() as db:
        n = await recover_stuck(db, utcnow(), all_=True)
        c = await cancel_invalid_pending(db)
        if n or c: log.info("Восстановление уведомлений: прервано %s, отменено устаревших %s", n, c)


async def purge_history():
    async with notif_db() as db:
        await db.execute("DELETE FROM notification_log WHERE status != 'pending' AND created_at < ?",
                         (iso(utcnow() - timedelta(days=HISTORY_KEEP_DAYS)),))
        await db.commit()


async def reminder_loop():
    """Бесконечный цикл планировщика: любая ошибка БД/Telegram логируется, цикл продолжает работу."""
    try:
        await recover_after_restart()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ошибка восстановления расписания уведомлений")
    last_purge = 0.0
    while True:
        try:
            await reminder_tick()
            if _time.monotonic() - last_purge > 3600:
                last_purge = _time.monotonic()
                await purge_history()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка планировщика уведомлений")
        await asyncio.sleep(NOTIFY_TICK_SECONDS)


# =====================================================================
#  API уведомлений (приложение)
# =====================================================================
class NotifySettingsBody(BaseModel):
    enabled: bool | None = None
    remind_offsets: list[int] | None = None
    morning_on: bool | None = None
    morning_time: str | None = None
    evening_on: bool | None = None
    evening_time: str | None = None
    overdue_on: bool | None = None
    overdue_time: str | None = None
    tz: str | None = None
    tz_auto: bool | None = None


@app.get("/api/notifications/settings")
async def get_notification_settings(request: Request):
    return {"settings": settings_view(await api_user(request))}


@app.put("/api/notifications/settings")
async def put_notification_settings(body: NotifySettingsBody, request: Request):
    u = await api_user(request)
    patch = body.model_dump(exclude_none=True)
    return {"status": "ok", "settings": await update_notification_settings(u["user_id"], patch)}


@app.get("/api/notifications/history")
async def get_notification_history(request: Request, limit: int = 40):
    u = await api_user(request)
    limit = min(max(limit, 1), 200)
    async with notif_db() as db:
        async with db.execute(
                "SELECT kind, title, status, attempts, detail, fire_at, sent_at, occ_day, task_time, offset_min FROM notification_log "
                "WHERE user_id = ? AND (status != 'pending' OR attempts > 0) "
                "ORDER BY id DESC LIMIT ?", (u["user_id"], limit)) as cur:
            rows = await cur.fetchall()
    out = []
    for r in rows:
        stamp = utc_to_local(datetime.fromisoformat(r["sent_at"] or r["fire_at"]), u["zone"])
        out.append({"kind": r["kind"], "label": KIND_LABELS.get(r["kind"], r["kind"]), "title": r["title"], "status": r["status"],
                    "attempts": r["attempts"], "detail": r["detail"], "when": stamp.strftime("%d.%m %H:%M"),
                    "offset": r["offset_min"], "task_time": r["task_time"]})
    return {"history": out}


@app.post("/api/notifications/test")
async def send_test_notification(request: Request):
    u = await api_user(request)
    now = utcnow()
    cand = {"kind": "test", "user_id": u["user_id"], "task_id": None, "occ_day": None, "task_time": None, "offset_min": None,
            "title": "Проверка доставки", "fire_at": iso(now), "dedup_key": f"test:{u['user_id']}:{_time.time_ns()}"}
    async with notif_db() as db:
        log_id = await claim(db, cand, u["chat_id"])
        status = await deliver(db, log_id)
        async with db.execute("SELECT status, detail FROM notification_log WHERE id = ?", (log_id,)) as cur:
            r = await cur.fetchone()
    if r["status"] != "sent":
        raise DomainError(502, f"Не удалось отправить: {r['detail'] or r['status']}")
    return {"status": "ok"}


# --- привычки ---
@app.get("/api/habits")
async def get_habits(request: Request, today: str | None = None):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        return {"habits": await list_habits(db, client_today(today), uid)}


@app.post("/api/habits")
async def add_habit_api(habit: HabitCreate, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        hid = await create_habit(db, habit.text, uid, time=habit.time, repeat=habit.repeat, days=habit.days,
                                 remind_type=habit.remind_type)
    return {"status": "ok", "id": hid}


@app.put("/api/habits/{habit_id}")
async def update_habit_api(habit_id: int, habit: HabitCreate, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        await update_habit(db, habit_id, habit.text, uid, time=habit.time, repeat=habit.repeat, days=habit.days,
                           remind_type=habit.remind_type)
    return {"status": "ok"}


@app.post("/api/habits/{habit_id}/toggle")
async def toggle_habit(habit_id: int, request: Request, day: str | None = None, today: str | None = None):
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        return {"status": "ok", "habit": await toggle_habit_db(db, habit_id, day or t, t, uid)}


@app.delete("/api/habits/{habit_id}")
async def delete_habit(habit_id: int, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("DELETE FROM habits WHERE id = ? AND user_id = ?", (habit_id, uid))
        if cur.rowcount: await db.execute("DELETE FROM goal_habits WHERE habit_id = ?", (habit_id,))
        if cur.rowcount == 0: raise DomainError(404, "Привычка не найдена")
        await db.execute("DELETE FROM habit_logs WHERE habit_id = ?", (habit_id,))
        await db.commit()
    await habit_changed(uid)
    return {"status": "ok"}


# --- события ---
@app.get("/api/events")
async def get_events(month: str, request: Request):
    uid = await api_uid(request)
    if not re.fullmatch(r"\d{4}-\d{2}", month): raise DomainError(400, "Месяц: ожидается формат ГГГГ-ММ")
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT id, date, title, time, color, emoji FROM events WHERE user_id = ? AND date LIKE ? "
                "ORDER BY date, COALESCE(time, '99:99'), id",
                (uid, month + "-%")) as cursor:
            return {"events": [dict(row) for row in await cursor.fetchall()]}


@app.post("/api/events")
async def add_event_api(ev: EventCreate, request: Request):
    uid = await api_uid(request)
    title = ev.title.strip()
    if not title: raise DomainError(400, "Пустое название")
    day = parse_day(ev.date, "Дата").isoformat()
    time = parse_time(ev.time) if ev.time else None
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO events (date, title, time, color, emoji, user_id) VALUES (?, ?, ?, ?, ?, ?)",
                         (day, title, time, ev.color, ev.emoji, uid))
        await db.commit()
    return {"status": "ok"}


@app.delete("/api/events/{event_id}")
async def delete_event_api(event_id: int, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("DELETE FROM events WHERE id = ? AND user_id = ?", (event_id, uid))
        if cur.rowcount == 0: raise DomainError(404, "Событие не найдено")
        await db.commit()
    return {"status": "ok"}


# --- цели ---
@app.get("/api/goals")
async def get_goals_api(request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, title, emoji, unit, target, current FROM goals WHERE user_id = ? ORDER BY id DESC", (uid,)) as cursor: return {
            "goals": [dict(row) for row in await cursor.fetchall()]}


@app.post("/api/goals")
async def add_goal_api(goal: GoalCreate, request: Request):
    uid = await api_uid(request)
    title = goal.title.strip()
    if not title: raise DomainError(400, "Пустое название")
    target = check_positive(goal.target, "Значение цели")
    current = check_number(goal.current, "Начальное значение")
    if current < 0: raise DomainError(400, "Начальное значение не может быть отрицательным")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO goals (title, emoji, unit, target, current, user_id) VALUES (?, ?, ?, ?, ?, ?)",
                         (title, goal.emoji, goal.unit.strip(), target, current, uid))
        await db.commit()
    return {"status": "ok"}


@app.post("/api/goals/{goal_id}/progress")
async def update_goal_progress(goal_id: int, p: GoalProgress, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        goal = await apply_goal_progress(db, goal_id, p.mode, p.value, uid)
    return {"status": "ok", "goal": goal}


@app.delete("/api/goals/{goal_id}")
async def delete_goal_api(goal_id: int, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("DELETE FROM goals WHERE id = ? AND user_id = ?", (goal_id, uid))
        if cur.rowcount == 0: raise DomainError(404, "Цель не найдена")
        await db.commit()
    return {"status": "ok"}


# --- раздел «Цели»: жизненные цели + накопления (накопления — прежние /api/goals, без изменений) ---
@app.get("/api/goals/overview")
async def goals_overview_api(request: Request, today: str | None = None):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        return await goals_overview(db, uid, client_today(today))


@app.get("/api/life-goals")
async def get_life_goals_api(request: Request, status: str = "all", today: str | None = None):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        return {"goals": await list_life_goals(db, uid, client_today(today), status)}


@app.post("/api/life-goals")
async def add_life_goal_api(goal: LifeGoalCreate, request: Request, today: str | None = None):
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        gid = await create_life_goal(db, goal, uid, t)
        return {"status": "ok", "id": gid, "goal": await get_life_goal(db, gid, uid, t)}


@app.get("/api/life-goals/{goal_id}")
async def get_life_goal_api(goal_id: int, request: Request, today: str | None = None):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        return {"goal": await get_life_goal(db, goal_id, uid, client_today(today))}


@app.put("/api/life-goals/{goal_id}")
async def update_life_goal_api(goal_id: int, body: LifeGoalUpdate, request: Request, today: str | None = None):
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        await update_life_goal(db, goal_id, provided(body), uid, t)
        return {"status": "ok", "goal": await get_life_goal(db, goal_id, uid, t)}


@app.delete("/api/life-goals/{goal_id}")
async def delete_life_goal_api(goal_id: int, request: Request):
    uid = await api_uid(request)
    async with aiosqlite.connect(DB_NAME) as db:
        await delete_life_goal(db, goal_id, uid)
    return {"status": "ok"}


@app.post("/api/life-goals/{goal_id}/milestones")
async def add_milestone_api(goal_id: int, m: MilestoneIn, request: Request, today: str | None = None):
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        mid = await add_milestone(db, goal_id, m.title, m.deadline, m.status, uid, t)
        return {"status": "ok", "id": mid, "goal": await get_life_goal(db, goal_id, uid, t)}


@app.put("/api/life-goals/{goal_id}/milestones/{milestone_id}")
async def update_milestone_api(goal_id: int, milestone_id: int, m: MilestoneUpdate, request: Request, today: str | None = None):
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        await update_milestone(db, goal_id, milestone_id, provided(m), uid, t)
        return {"status": "ok", "goal": await get_life_goal(db, goal_id, uid, t)}


@app.delete("/api/life-goals/{goal_id}/milestones/{milestone_id}")
async def delete_milestone_api(goal_id: int, milestone_id: int, request: Request, today: str | None = None):
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        await delete_milestone(db, goal_id, milestone_id, uid)
        return {"status": "ok", "goal": await get_life_goal(db, goal_id, uid, t)}


@app.post("/api/life-goals/{goal_id}/tasks")
async def add_goal_task_api(goal_id: int, task: GoalTaskCreate, request: Request, today: str | None = None):
    """Создать новую задачу прямо из цели."""
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        tid = await create_goal_task(db, goal_id, task, uid, t)
        return {"status": "ok", "task_id": tid, "goal": await get_life_goal(db, goal_id, uid, t)}


@app.post("/api/life-goals/{goal_id}/tasks/link")
async def link_goal_task_api(goal_id: int, body: GoalTaskLink, request: Request, today: str | None = None):
    """Привязать уже существующую задачу (или перенести привязанную на другой этап)."""
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        await link_task_to_goal(db, goal_id, body.task_id, body.milestone_id, uid)
        return {"status": "ok", "goal": await get_life_goal(db, goal_id, uid, t)}


@app.delete("/api/life-goals/{goal_id}/tasks/{task_id}")
async def unlink_goal_task_api(goal_id: int, task_id: int, request: Request, today: str | None = None):
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        await unlink_task_from_goal(db, goal_id, task_id, uid)
        return {"status": "ok", "goal": await get_life_goal(db, goal_id, uid, t)}


@app.post("/api/life-goals/{goal_id}/habits")
async def link_goal_habit_api(goal_id: int, body: GoalHabitLink, request: Request, today: str | None = None):
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        await link_habit_to_goal(db, goal_id, body.habit_id, uid)
        return {"status": "ok", "goal": await get_life_goal(db, goal_id, uid, t)}


@app.delete("/api/life-goals/{goal_id}/habits/{habit_id}")
async def unlink_goal_habit_api(goal_id: int, habit_id: int, request: Request, today: str | None = None):
    uid = await api_uid(request)
    t = client_today(today)
    async with aiosqlite.connect(DB_NAME) as db:
        await unlink_habit_from_goal(db, goal_id, habit_id, uid)
        return {"status": "ok", "goal": await get_life_goal(db, goal_id, uid, t)}


@app.get("/")
async def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)