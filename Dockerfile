FROM python:3.12-slim

# Не пишем .pyc, не буферизуем логи (иначе docker logs пустой до краша)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow

WORKDIR /app

# Слой зависимостей отдельно — не пересобирается при правке кода
COPY requirements.txt .
# git нужен: playerokapi ставится из репозитория, а не из PyPI
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY . .

# Не root: если кто-то пролезет через бота, он не хозяин контейнера
RUN useradd -m -u 1000 app && mkdir -p /app/data && chown -R app:app /app
USER app

VOLUME ["/app/data"]

# Проверка живости: процесс есть и БД доступна
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import sqlite3,os; sqlite3.connect(os.getenv('DB_PATH','data/store.db')).execute('select 1')" || exit 1

CMD ["python", "bot.py"]
