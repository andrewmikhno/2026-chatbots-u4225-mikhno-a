"""
Personal Assistant Bot — личный Telegram-бот-ассистент для задач.

Возможности:
  * любое текстовое сообщение (не команда) сохраняется как задача;
  * приоритеты задач: 🔴 высокий, 🟡 средний, 🟢 низкий;
  * напоминания через N минут/часов или в конкретное время;
  * список дел, отсортированный по приоритету;
  * простые заметки;
  * голосовые сообщения: речь распознаётся через OpenAI, затем модель решает,
    что это — событие в Google Calendar, заметка или задача;
  * /events — ближайшие события из Google Calendar;
  * Google Calendar доступен только владельцу бота (OWNER_ID), остальное — всем;
  * /stats — статистика использования (только владельцу).

Все данные хранятся в SQLite (файл bot.db), напоминания переживают перезапуск.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from openai import APIError, AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, Update
from telegram.constants import ChatAction
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

# OpenAI: распознавание речи и разбор намерения
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")

# Google Calendar: сервисный аккаунт + id календаря (обычно ваш gmail)
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Владелец бота (Telegram user id): только ему доступны личный календарь и /stats
OWNER_ID = int(os.getenv("OWNER_ID") or 0)

# Клиент OpenAI создаём, только если задан ключ — без него бот работает как в Lab1
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx пишет в лог каждый запрос к Telegram — это слишком шумно
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)  # то же для HTTP-клиента OpenAI
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

            -- Журнал действий пользователей — для статистики /stats
            CREATE TABLE IF NOT EXISTS usage (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                action      TEXT    NOT NULL,            -- text_task, voice_event, note, ...
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


def log_usage(user_id: int, action: str) -> None:
    """Записывает действие пользователя в журнал usage (для /stats)."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO usage (user_id, action, created_at) VALUES (?, ?, ?)",
            (user_id, action, now_utc().isoformat()),
        )


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def save_note(user_id: int, text: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO notes (user_id, text, created_at) VALUES (?, ?, ?)",
            (user_id, text, now_utc().isoformat()),
        )


# ---------------------------------------------------------------------------
# OpenAI: распознавание речи и разбор намерения
# ---------------------------------------------------------------------------

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

INTENT_PROMPT = """Ты разбираешь голосовые команды для личного ассистента.
Сейчас: {now}, {weekday}. Часовой пояс пользователя: {tz}.

Определи, что хочет пользователь, и верни ТОЛЬКО JSON одного из четырёх видов:
{{"action": "event", "title": "...", "start": "YYYY-MM-DDTHH:MM:SS+HH:MM", "duration_minutes": 60}}
{{"action": "note", "text": "..."}}
{{"action": "task", "text": "...", "priority": 2}}
{{"action": "show", "what": "tasks" | "notes" | "events"}}

Правила:
- event — встреча, звонок, мероприятие или дело с конкретной датой/временем, либо явная просьба
  «добавь в календарь». Относительные даты («завтра», «в пятницу») переведи в точную дату.
  Если время не названо — 09:00.
  Длительность: «на 2 часа», «часик», «на полтора часа», «на 30 минут» без слова «в» перед
  числом — это duration_minutes (120, 60, 90, 30), а не время начала. Если длительность
  не названа — 60 минут.
- note — просьба записать мысль, идею, информацию («запиши», «заметка», «идея»).
- task — дело без конкретного времени. priority: 1 если «срочно»/«важно», 3 если «когда-нибудь», иначе 2.
- show — просьба показать/прочитать уже сохранённое: «какие у меня дела» → tasks,
  «покажи заметки» → notes, «что у меня в календаре» → events.
- Если не уверен — task.
- title и text формулируй кратко, без слов-команд («добавь», «запиши», «напомни»)."""


async def transcribe_voice(audio: bytes) -> str:
    """Отправляет голосовое (ogg/opus из Telegram) в OpenAI и возвращает распознанный текст."""
    result = await openai_client.audio.transcriptions.create(
        model=OPENAI_STT_MODEL,
        file=("voice.ogg", audio),
    )
    return result.text.strip()


async def detect_intent(text: str) -> dict:
    """Просит модель разобрать фразу и вернуть действие в виде JSON.

    Бросает ValueError, если модель вернула что-то, кроме корректного JSON нужного вида.
    """
    now = datetime.now(TIMEZONE)
    system_prompt = INTENT_PROMPT.format(
        now=now.strftime("%Y-%m-%d %H:%M"), weekday=WEEKDAYS[now.weekday()], tz=TIMEZONE.key
    )
    response = await openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
    )
    raw = response.choices[0].message.content
    logger.info("Ответ модели: %s", raw)

    try:
        intent = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as e:
        raise ValueError(f"Модель вернула не JSON: {raw!r}") from e

    if intent.get("action") not in ("event", "note", "task", "show"):
        raise ValueError(f"Неизвестное действие: {intent!r}")
    return intent


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------

def calendar_configured() -> bool:
    return bool(GOOGLE_CALENDAR_ID) and os.path.exists(GOOGLE_CREDENTIALS_FILE)


def get_calendar_service():
    """Создаёт клиент Google Calendar API по ключу сервисного аккаунта.

    Клиент создаётся на каждый вызов: это дёшево и безопасно при работе из разных потоков.
    """
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE, scopes=GOOGLE_SCOPES
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def insert_event(title: str, start: datetime, duration_minutes: int) -> dict:
    """Создаёт событие в календаре и возвращает его (в том числе ссылку htmlLink)."""
    end = start + timedelta(minutes=duration_minutes)
    body = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE.key},
        "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE.key},
    }
    return get_calendar_service().events().insert(calendarId=GOOGLE_CALENDAR_ID, body=body).execute()


def list_upcoming_events(limit: int = 5) -> list[dict]:
    """Ближайшие события календаря, начиная с текущего момента."""
    result = (
        get_calendar_service()
        .events()
        .list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=now_utc().isoformat(),
            maxResults=limit,
            singleEvents=True,  # разворачиваем повторяющиеся события в отдельные
            orderBy="startTime",
        )
        .execute()
    )
    return result.get("items", [])


def format_event_start(event: dict) -> str:
    """У обычного события есть start.dateTime, у события на весь день — только start.date."""
    start = event["start"]
    if "dateTime" in start:
        return datetime.fromisoformat(start["dateTime"]).astimezone(TIMEZONE).strftime("%d.%m %H:%M")
    return datetime.fromisoformat(start["date"]).strftime("%d.%m") + " весь день"


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
    "/notes — последние 10 заметок\n\n"
    "🎙 Голосовые\n"
    "Отправь голосовое — я пойму, что это: событие в календаре, заметка или задача.\n"
    "Например: «Завтра в 15:00 встреча с командой» или «Запиши идею: бот для покупок»\n"
    "/events — ближайшие события из Google Calendar (только для владельца бота)"
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

    await save_task_and_reply(update.message, update.effective_user.id, text, priority)
    log_usage(update.effective_user.id, "text_task")


async def save_task_and_reply(message: Message, user_id: int, text: str, priority: int) -> None:
    """Сохраняет задачу и отвечает с кнопками смены приоритета (общее для текста и голоса)."""
    task_id = add_task(user_id, text, priority)
    emoji, name = PRIORITIES[priority]
    await message.reply_text(
        f"✅ Задача добавлена: {text}\nПриоритет: {emoji} {name}. Можно поменять кнопкой ниже.",
        reply_markup=priority_keyboard(task_id),
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Голосовое: распознаём речь -> модель выбирает действие -> календарь / заметка / задача."""
    message = update.message
    user_id = update.effective_user.id

    if openai_client is None:
        await message.reply_text("🎙 Голосовые пока не настроены: добавь OPENAI_API_KEY в .env")
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    # 1. Скачиваем голосовое и распознаём речь
    try:
        voice_file = await message.voice.get_file()
        audio = bytes(await voice_file.download_as_bytearray())
        text = await transcribe_voice(audio)
    except APIError as e:
        logger.error("Ошибка распознавания речи: %s", e)
        await message.reply_text("😔 Не удалось распознать голосовое (ошибка OpenAI). Попробуй ещё раз.")
        return

    if not text:
        await message.reply_text("🤔 Не расслышал, что ты сказал. Попробуй записать ещё раз.")
        return

    logger.info("Распознано: %s", text)
    await message.reply_text(f"🎙 Распознал: {text}")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    # 2. Модель решает, что это: событие, заметка или задача
    try:
        intent = await detect_intent(text)
    except APIError as e:
        logger.error("Ошибка OpenAI при разборе намерения: %s", e)
        await message.reply_text("😔 Не получилось разобрать команду (ошибка OpenAI). Сохраню как задачу.")
        await save_task_and_reply(message, user_id, text, DEFAULT_PRIORITY)
        return
    except ValueError as e:
        logger.warning("Некорректный ответ модели: %s", e)
        await message.reply_text("🤔 Не понял, что сделать. Сохраню как задачу.")
        await save_task_and_reply(message, user_id, text, DEFAULT_PRIORITY)
        return

    logger.info("Действие: %s", intent)
    action = intent["action"]
    log_usage(user_id, f"voice_{action}")

    # 3. Выполняем действие
    if action == "note":
        note_text = intent.get("text") or text
        save_note(user_id, note_text)
        await message.reply_text(f"📝 Заметка сохранена: {note_text}")

    elif action == "task":
        priority = intent.get("priority")
        if priority not in PRIORITIES:
            priority = DEFAULT_PRIORITY
        await save_task_and_reply(message, user_id, intent.get("text") or text, priority)

    elif action == "event":
        await create_event_from_intent(message, user_id, intent, text)

    elif action == "show":
        # Просьбу «покажи …» выполняем теми же функциями, что и команды /list, /notes, /events
        handlers = {"tasks": list_tasks, "notes": list_notes, "events": events_command}
        await handlers.get(intent.get("what"), list_tasks)(update, context)


async def create_event_from_intent(message: Message, user_id: int, intent: dict, text: str) -> None:
    """Создаёт событие в Google Calendar по ответу модели. При любой проблеме — сохраняет задачу."""
    title = intent.get("title") or text

    # Календарь личный: события других пользователей сохраняем как задачи
    if not is_owner(user_id):
        await message.reply_text("📅 Календарь доступен только владельцу бота, сохранил как задачу.")
        await save_task_and_reply(message, user_id, title, DEFAULT_PRIORITY)
        return

    if not calendar_configured():
        await message.reply_text("📅 Google Calendar не настроен, поэтому сохраню это как задачу.")
        await save_task_and_reply(message, user_id, title, DEFAULT_PRIORITY)
        return

    # Проверяем, что модель вернула корректное время начала и длительность
    try:
        start = datetime.fromisoformat(intent["start"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=TIMEZONE)  # модель забыла пояс — считаем, что он локальный
        duration = int(intent.get("duration_minutes") or 60)
        if not 1 <= duration <= 24 * 60:
            duration = 60
    except (KeyError, TypeError, ValueError):
        logger.warning("Некорректное время события: %s", intent)
        await message.reply_text("🤔 Не понял дату и время события. Сохраню как задачу.")
        await save_task_and_reply(message, user_id, title, DEFAULT_PRIORITY)
        return

    try:
        # Google API синхронный — выполняем в отдельном потоке, чтобы не блокировать бота
        event = await asyncio.to_thread(insert_event, title, start, duration)
    except HttpError as e:
        logger.error("Ошибка Google Calendar: %s", e)
        await message.reply_text("😔 Google Calendar вернул ошибку, событие не создано. Сохраню как задачу.")
        await save_task_and_reply(message, user_id, title, DEFAULT_PRIORITY)
        return

    local_start = start.astimezone(TIMEZONE)
    await message.reply_text(
        f"📅 Добавил в календарь: {title}\n"
        f"🕒 {local_start.strftime('%d.%m.%Y %H:%M')}, {duration} мин\n"
        f"{event.get('htmlLink', '')}",
        # без превью: иначе Telegram добавляет под ссылкой большую карточку Google Workspace
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/events — ближайшие события из Google Calendar (только владельцу)."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("📅 Календарь доступен только владельцу бота.")
        return

    if not calendar_configured():
        await update.message.reply_text(
            "📅 Google Calendar не настроен: нужны credentials.json и GOOGLE_CALENDAR_ID в .env"
        )
        return

    try:
        events = await asyncio.to_thread(list_upcoming_events)
    except HttpError as e:
        logger.error("Ошибка Google Calendar: %s", e)
        await update.message.reply_text("😔 Не удалось получить события из Google Calendar. Попробуй позже.")
        return

    if not events:
        await update.message.reply_text("📅 Ближайших событий нет. Надиктуй голосовое, чтобы добавить!")
        return

    lines = ["📅 Ближайшие события:\n"]
    lines += [f"• {format_event_start(e)} — {e.get('summary', '(без названия)')}" for e in events]
    await update.message.reply_text("\n".join(lines))


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

    log_usage(update.effective_user.id, "delete" if delete else "done")
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
    log_usage(update.effective_user.id, "remind")
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

    save_note(update.effective_user.id, text)
    log_usage(update.effective_user.id, "note")
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


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — статистика использования бота (только владельцу)."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только владельцу бота.")
        return

    day_ago = (now_utc() - timedelta(days=1)).isoformat()
    with get_db() as conn:
        users = conn.execute(
            "SELECT COUNT(*) FROM (SELECT user_id FROM usage UNION SELECT user_id FROM tasks "
            "UNION SELECT user_id FROM notes)"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
        by_action = conn.execute(
            "SELECT action, COUNT(*) AS cnt FROM usage GROUP BY action ORDER BY cnt DESC"
        ).fetchall()
        day_actions, day_users = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM usage WHERE created_at >= ?", (day_ago,)
        ).fetchone()
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        reminders = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]

    lines = [
        "📊 Статистика бота\n",
        f"👥 Пользователей: {users}",
        f"⚡ Действий всего: {total}",
        f"🕐 За 24 часа: {day_actions} действий от {day_users} польз.\n",
        "По типам:",
    ]
    lines += [f"• {row['action']}: {row['cnt']}" for row in by_action] or ["• пока нет"]
    lines.append(f"\nВ базе: задач {tasks}, заметок {notes}, напоминаний {reminders}")
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
    app.add_handler(CommandHandler("events", events_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(change_priority, pattern=r"^prio:"))
    # Обычный текст (не команда) — новая задача
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # Голосовое — распознавание через OpenAI и выбор действия
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_error_handler(error_handler)

    logger.info("Бот запущен. Часовой пояс: %s", TIMEZONE)
    logger.info("OpenAI: %s", "подключён" if openai_client else "не настроен (нет OPENAI_API_KEY)")
    logger.info("Google Calendar: %s", "подключён" if calendar_configured() else "не настроен")
    logger.info("Владелец бота: %s", OWNER_ID or "не задан (календарь и /stats недоступны никому)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
