# Лёгкий официальный образ Python
FROM python:3.12-slim

# Логи сразу пишутся в stdout (видны в `docker compose logs`), .pyc не создаются
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Сначала только зависимости — этот слой кэшируется и не пересобирается при правке кода
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Папка для SQLite — к ней подключается volume из docker-compose.yml
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
