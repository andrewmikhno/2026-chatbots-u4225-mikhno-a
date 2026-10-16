# Personal Assistant Bot — Lab3

Telegram-бот-ассистент для задач: принимает задачи текстом, сортирует по приоритетам,
присылает напоминания и хранит заметки. Данные — в SQLite (`bot.db`).

**Новое в Lab3:** деплой в Docker на собственный VPS; Google Calendar и `/stats` доступны
только владельцу бота (`OWNER_ID`), остальные пользователи пользуются задачами, заметками
и напоминаниями; журнал действий для статистики.

**Lab2:** голосовые сообщения. Речь распознаётся через OpenAI, затем модель решает,
что это: событие в **Google Calendar**, заметка или задача. Команда `/events` показывает
ближайшие события календаря.

Бот: [@itmoftmi_am_bot](https://t.me/itmoftmi_am_bot)

## Установка и запуск

Требуется Python 3.10+.

```bash
cd lab3
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # затем заполните .env (см. ниже)
python bot.py
```

Без `OPENAI_API_KEY` и Google-настроек бот запускается и работает как в Lab1,
а на голосовые и `/events` отвечает, что интеграция не настроена.

## Настройка OpenAI

1. Создайте API-ключ на https://platform.openai.com/api-keys.
2. Впишите его в `.env`: `OPENAI_API_KEY=sk-...`.
3. При желании смените модели: `OPENAI_MODEL` (разбор команд) и `OPENAI_STT_MODEL` (распознавание речи).

## Настройка Google Calendar (сервисный аккаунт)

1. В [Google Cloud Console](https://console.cloud.google.com/) создайте проект.
2. **APIs & Services → Library** → найдите **Google Calendar API** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account**: задайте имя, остальное пропустите.
4. Откройте созданный сервисный аккаунт → вкладка **Keys → Add key → Create new key → JSON**.
   Скачанный файл положите в папку `lab3/` под именем `credentials.json` (он в `.gitignore`).
5. Скопируйте email сервисного аккаунта (вида `...@...iam.gserviceaccount.com`).
6. В [Google Calendar](https://calendar.google.com/) → настройки своего календаря →
   **Предоставить доступ пользователям** → добавьте email сервисного аккаунта
   с правом **«Внесение изменений в мероприятия»**.
7. В `.env` укажите `GOOGLE_CALENDAR_ID` — для основного календаря это ваш gmail.

## Команды

| Команда | Описание |
|---|---|
| _любой текст_ | добавить задачу (средний приоритет) |
| `!текст` | добавить задачу с высоким приоритетом |
| `/list` | список активных задач по приоритетам |
| `/done <N>` | отметить задачу №N выполненной |
| `/delete <N>` | удалить задачу №N |
| `/remind <время> <текст>` | напоминание: `30m`, `2h` или `18:30` |
| `/reminders` | будущие напоминания |
| `/note <текст>` | сохранить заметку |
| `/notes` | последние 10 заметок |
| _голосовое сообщение_ | событие в календаре, заметка, задача или «покажи …» — решает модель |
| `/events` | 5 ближайших событий из Google Calendar |
| `/stats` | статистика использования (только владелец) |
| `/help` | справка |

После добавления задачи под сообщением появляются кнопки 🔴 / 🟡 / 🟢 для смены приоритета.

## Конфигурация (`.env`)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `BOT_TOKEN` | — | токен от @BotFather (обязательно) |
| `TIMEZONE` | `Europe/Moscow` | часовой пояс для напоминаний |
| `DB_PATH` | `bot.db` | путь к файлу SQLite |
| `OPENAI_API_KEY` | — | ключ OpenAI (для голосовых) |
| `OPENAI_MODEL` | `gpt-4o-mini` | модель для разбора команд |
| `OPENAI_STT_MODEL` | `gpt-4o-mini-transcribe` | модель распознавания речи |
| `GOOGLE_CREDENTIALS_FILE` | `credentials.json` | JSON-ключ сервисного аккаунта |
| `GOOGLE_CALENDAR_ID` | — | ID календаря (ваш gmail) |
| `OWNER_ID` | — | Telegram id владельца: ему доступны календарь и `/stats` |

## Как работает голосовое

1. Бот скачивает голосовое (ogg) и отправляет его в OpenAI Audio API → получает текст.
2. Текст + текущие дата/время/часовой пояс отправляются в OpenAI Chat Completions, модель
   возвращает JSON: `{"action": "event" | "note" | "task" | "show", ...}`.
3. `event` → Google Calendar API (`events.insert`), `note`/`task` → SQLite,
   `show` («покажи мои заметки») → то же, что `/list`, `/notes` или `/events`.
4. Если что-то пошло не так (нет настроек, ошибка API, модель вернула мусор) —
   бот сообщает об этом и сохраняет фразу как задачу, чтобы ничего не потерялось.

## Структура БД

- `tasks` — задачи: текст, приоритет (1/2/3), статус выполнения, даты
- `reminders` — напоминания: текст, время срабатывания (UTC), флаг отправки
- `notes` — заметки

Неотправленные напоминания при старте бота загружаются из БД и снова ставятся в очередь,
поэтому перезапуск бота их не теряет.

## Деплой на VPS (Docker)

Бот работает через long polling, поэтому входящие порты, домен и webhook не нужны.

```bash
# 1. На сервере: установить Docker (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sh

# 2. Склонировать репозиторий и перейти в папку
git clone https://github.com/andrewmikhno/2026-chatbots-u4225-mikhno-a.git /opt/assistant-bot
cd /opt/assistant-bot && git checkout lab3 && cd lab3

# 3. Скопировать секреты с локальной машины (выполняется локально)
scp lab3/.env lab3/credentials.json root@<server>:/opt/assistant-bot/lab3/

# 4. Собрать и запустить
docker compose up -d --build

# Логи / состояние / потребление памяти
docker compose logs -f
docker compose ps
docker stats --no-stream assistant-bot
```

**Обновление после изменений в репозитории:**

```bash
cd /opt/assistant-bot && git pull && cd lab3 && docker compose up -d --build
```

База SQLite хранится в Docker volume `bot-data` и переживает пересборку контейнера.
