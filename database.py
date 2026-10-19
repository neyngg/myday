import aiosqlite

DB_NAME = "daily.db"

async def init_db():
    """Создает таблицы при первом запуске, если их еще нет"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                completed INTEGER DEFAULT 0,
                created_date TEXT DEFAULT CURRENT_DATE
            )
        """)
        await db.commit()

async def get_today_tasks():
    """Получает список всех задач на сегодня"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, text, completed FROM tasks WHERE created_date = CURRENT_DATE") as cursor:
            rows = await cursor.fetchall()
            return [{"id": row["id"], "text": row["text"], "completed": bool(row["completed"])} for row in rows]

async def add_task(text: str):
    """Добавляет новую задачу на сегодня"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("INSERT INTO tasks (text) VALUES (?)", (text,))
        await db.commit()
        return cursor.lastrowid

async def toggle_task(task_id: int):
    """Переключает статус выполнения задачи (0 -> 1 или 1 -> 0)"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            UPDATE tasks 
            SET completed = CASE WHEN completed = 1 THEN 0 ELSE 1 END 
            WHERE id = ?
        """, (task_id,))
        await db.commit()