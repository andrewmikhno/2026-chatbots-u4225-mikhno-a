"""
Personal Assistant Bot — личный Telegram-бот-ассистент для задач.

Возможности:
  * любое текстовое сообщение (не команда) сохраняется как задача;
  * приоритеты задач: 🔴 высокий, 🟡 средний, 🟢 низкий;
  * напоминания через N минут/часов или в конкретное время;
  * список дел, отсортированный по приоритету;
  * простые заметки.

Все данные хранятся в SQLite (файл bot.db), напоминания переживают перезапуск.
"""

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

load_dotenv()  # читаем переменные из файла .env

BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx пишет в лог каждый запрос к Telegram — это слишком шумно
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("assistant_bot")

# Приоритеты: код в БД -> эмодзи и название
PRIORITIES = {
    1: ("🔴", "высокий"),
    2: ("🟡", "средний"),
    3: ("🟢", "низкий"),
}
DEFAULT_PRIORITY = 2


# ---------------------------------------------------------------------------
# Работа с базой данных (SQLite)
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Открывает соединение с БД. Строки возвращаются как словари (sqlite3.Row)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                text        TEXT    NOT NULL,
                priority    INTEGER NOT NULL DEFAULT 2,  -- 1 высокий, 2 средний, 3 низкий
                is_done     INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL,
                done_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                text        TEXT    NOT NULL,
                remind_at   TEXT    NOT NULL,            -- ISO-строка в UTC
                is_sent     INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS notes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                text        TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            );
            """
        )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_active_tasks(user_id: int) -> list[sqlite3.Row]:
    """Активные задачи пользователя: сначала высокий приоритет, внутри — по времени создания.

    Порядок важен: номера в /list, /done и /delete считаются по этому же списку.
    """
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND is_done = 0 "
            "ORDER BY priority, created_at",
            (user_id,),
        ).fetchall()


def add_task(user_id: int, text: str, priority: int) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (user_id, text, priority, created_at) VALUES (?, ?, ?, ?)",
            (user_id, text, priority, now_utc().isoformat()),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def priority_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Inline-кнопки для быстрой смены приоритета задачи."""
    buttons = [
        InlineKeyboardButton(f"{emoji} {name}", callback_data=f"prio:{task_id}:{code}")
        for code, (emoji, name) in PRIORITIES.items()
    ]
    return InlineKeyboardMarkup([buttons])


def parse_task_number(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Достаёт номер задачи из аргументов команды. Возвращает None, если номер некорректен."""
    if len(context.args) != 1 or not context.args[0].isdigit():
        return None
    return int(context.args[0])


def parse_remind_time(value: str) -> datetime | None:
    """Разбирает время напоминания и возвращает момент срабатывания (aware datetime).

    Поддерживаемые форматы:
      30m / 30м / 30min / 30мин  — через 30 минут
      2h / 2ч                     — через 2 часа
      18:30                       — сегодня в 18:30 (или завтра, если время уже прошло)
    """
    value = value.strip().lower()
    now = datetime.now(TIMEZONE)

    # Через N минут
    match = re.fullmatch(r"(\d+)\s*(m|min|м|мин)", value)
    if match:
        return now + timedelta(minutes=int(match.group(1)))

    # Через N часов
    match = re.fullmatch(r"(\d+)\s*(h|ч)", value)
    if match:
        return now + timedelta(hours=int(match.group(1)))

    # Конкретное время ЧЧ:ММ
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        remind_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if remind_at <= now:
            remind_at += timedelta(days=1)  # время сегодня уже прошло — переносим на завтра
        return remind_at

    return None


def format_local(dt_iso: str) -> str:
    """ISO-строку (UTC) из БД показывает в локальном часовом поясе."""
    return datetime.fromisoformat(dt_iso).astimezone(TIMEZONE).strftime("%d.%m %H:%M")


# ---------------------------------------------------------------------------
# Напоминания (JobQueue)
# ---------------------------------------------------------------------------

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает в назначенное время: отправляет напоминание и помечает его отправленным."""
    reminder_id = context.job.data
    with get_db() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        if row is None or row["is_sent"]:
            return
        await context.bot.send_message(chat_id=row["chat_id"], text=f"⏰ Напоминание: {row['text']}")
        conn.execute("UPDATE reminders SET is_sent = 1 WHERE id = ?", (reminder_id,))


def schedule_reminder(app: Application, reminder_id: int, remind_at: datetime) -> None:
    """Ставит напоминание в очередь JobQueue. Просроченные отправляются сразу."""
    delay = max((remind_at - now_utc()).total_seconds(), 1)
    app.job_queue.run_once(send_reminder, when=delay, data=reminder_id, name=f"reminder-{reminder_id}")


async def restore_reminders(app: Application) -> None:
    """Вызывается при старте бота: заново ставит в очередь все неотправленные напоминания."""
    with get_db() as conn:
        rows = conn.execute("SELECT id, remind_at FROM reminders WHERE is_sent = 0").fetchall()
    for row in rows:
        schedule_reminder(app, row["id"], datetime.fromisoformat(row["remind_at"]))
    logger.info("Восстановлено напоминаний: %d", len(rows))


# ---------------------------------------------------------------------------
# Обработчики команд
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📋 Задачи\n"
    "Просто напиши текст — я сохраню его как задачу.\n"
    "Начни с «!», чтобы сразу поставить высокий приоритет: !Сдать отчёт\n"
    "/list — список дел по приоритетам\n"
    "/done 2 — отметить задачу №2 выполненной\n"
    "/delete 2 — удалить задачу №2\n\n"
    "⏰ Напоминания\n"
    "/remind 30m Позвонить маме — через 30 минут\n"
    "/remind 2h Проверить почту — через 2 часа\n"
    "/remind 18:30 Тренировка — сегодня в 18:30 (или завтра)\n"
    "/reminders — будущие напоминания\n\n"
    "📝 Заметки\n"
    "/note Идея для проекта — сохранить заметку\n"
    "/notes — последние 10 заметок"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"Привет, {name}! 👋 Я твой личный ассистент: помогу не забыть дела.\n\n{HELP_TEXT}"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любой текст без команды — новая задача."""
    text = update.message.text.strip()
    priority = DEFAULT_PRIORITY

    # «!» в начале — высокий приоритет
    if text.startswith("!"):
        priority = 1
        text = text.lstrip("!").strip()

    if not text:
        await update.message.reply_text("Пустую задачу сохранить не могу 🙂 Напиши, что нужно сделать.")
        return

    task_id = add_task(update.effective_user.id, text, priority)
    emoji, name = PRIORITIES[priority]
    await update.message.reply_text(
        f"✅ Задача добавлена: {text}\nПриоритет: {emoji} {name}. Можно поменять кнопкой ниже.",
        reply_markup=priority_keyboard(task_id),
    )


async def change_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатия на inline-кнопку приоритета."""
    query = update.callback_query
    _, task_id, priority = query.data.split(":")
    task_id, priority = int(task_id), int(priority)

    with get_db() as conn:
        # user_id в условии — чтобы нельзя было изменить чужую задачу
        cur = conn.execute(
            "UPDATE tasks SET priority = ? WHERE id = ? AND user_id = ?",
            (priority, task_id, query.from_user.id),
        )
        task = conn.execute("SELECT text FROM tasks WHERE id = ?", (task_id,)).fetchone()

    if cur.rowcount == 0 or task is None:
        await query.answer("Задача не найдена", show_alert=True)
        return

    emoji, name = PRIORITIES[priority]
    await query.answer(f"Приоритет: {name}")
    await query.edit_message_text(
        f"✅ Задача: {task['text']}\nПриоритет: {emoji} {name}",
        reply_markup=priority_keyboard(task_id),
    )


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = get_active_tasks(update.effective_user.id)
    if not tasks:
        await update.message.reply_text("🎉 Список дел пуст! Напиши задачу, чтобы добавить.")
        return

    lines = ["📋 Твой список дел:\n"]
    for number, task in enumerate(tasks, start=1):
        emoji, _ = PRIORITIES[task["priority"]]
        lines.append(f"{number}. {emoji} {task['text']}")
    lines.append("\nГотово? /done <номер>")
    await update.message.reply_text("\n".join(lines))


async def done_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _finish_task(update, context, delete=False)


async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _finish_task(update, context, delete=True)


async def _finish_task(update: Update, context: ContextTypes.DEFAULT_TYPE, delete: bool) -> None:
    """Общая логика для /done и /delete: находит задачу по номеру из /list."""
    command = "/delete" if delete else "/done"
    number = parse_task_number(context)
    if number is None:
        await update.message.reply_text(f"Укажи номер задачи из /list, например: {command} 1")
        return

    tasks = get_active_tasks(update.effective_user.id)
    if not 1 <= number <= len(tasks):
        await update.message.reply_text(f"Задачи №{number} нет. Посмотри актуальные номера в /list")
        return

    task = tasks[number - 1]
    with get_db() as conn:
        if delete:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
        else:
            conn.execute(
                "UPDATE tasks SET is_done = 1, done_at = ? WHERE id = ?",
                (now_utc().isoformat(), task["id"]),
            )

    if delete:
        await update.message.reply_text(f"🗑 Удалено: {task['text']}")
    else:
        await update.message.reply_text(f"✅ Отлично, выполнено: {task['text']}")


async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    usage = "Формат: /remind <время> <текст>\nНапример: /remind 30m Позвонить маме или /remind 18:30 Тренировка"
    if len(context.args) < 2:
        await update.message.reply_text(usage)
        return

    remind_at = parse_remind_time(context.args[0])
    if remind_at is None:
        await update.message.reply_text(f"Не понял время «{context.args[0]}» 🤔\n{usage}")
        return

    text = " ".join(context.args[1:])
    remind_at_utc = remind_at.astimezone(timezone.utc)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (user_id, chat_id, text, remind_at) VALUES (?, ?, ?, ?)",
            (update.effective_user.id, update.effective_chat.id, text, remind_at_utc.isoformat()),
        )
        reminder_id = cur.lastrowid

    schedule_reminder(context.application, reminder_id, remind_at_utc)
    # Часовой пояс показываем явно, чтобы сразу было видно, если он настроен не тот
    await update.message.reply_text(
        f"⏰ Напомню {remind_at.strftime('%d.%m в %H:%M')} ({TIMEZONE.key}): {text}"
    )


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT text, remind_at FROM reminders WHERE user_id = ? AND is_sent = 0 ORDER BY remind_at",
            (update.effective_user.id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("Будущих напоминаний нет. Создай: /remind 30m Текст")
        return

    lines = ["⏰ Будущие напоминания:\n"]
    lines += [f"• {format_local(row['remind_at'])} — {row['text']}" for row in rows]
    await update.message.reply_text("\n".join(lines))


async def add_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Напиши текст заметки после команды, например: /note Идея для проекта")
        return

    with get_db() as conn:
        conn.execute(
            "INSERT INTO notes (user_id, text, created_at) VALUES (?, ?, ?)",
            (update.effective_user.id, text, now_utc().isoformat()),
        )
    await update.message.reply_text(f"📝 Заметка сохранена: {text}")


async def list_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT text, created_at FROM notes WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
            (update.effective_user.id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("Заметок пока нет. Создай: /note Текст заметки")
        return

    lines = ["📝 Последние заметки:\n"]
    lines += [f"• {format_local(row['created_at'])} — {row['text']}" for row in rows]
    await update.message.reply_text("\n".join(lines))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок: логирует и вежливо сообщает пользователю, бот не падает."""
    logger.error("Ошибка при обработке апдейта", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("😔 Что-то пошло не так. Попробуй ещё раз чуть позже.")


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен от @BotFather.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(restore_reminders).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("done", done_task))
    app.add_handler(CommandHandler("delete", delete_task))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CommandHandler("reminders", list_reminders))
    app.add_handler(CommandHandler("note", add_note))
    app.add_handler(CommandHandler("notes", list_notes))
    app.add_handler(CallbackQueryHandler(change_priority, pattern=r"^prio:"))
    # Обычный текст (не команда) — новая задача
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    logger.info("Бот запущен. Часовой пояс: %s", TIMEZONE)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
