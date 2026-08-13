"""Конфигурация из переменных окружения: логирование, авторизация, подключение к Postgres."""

from __future__ import annotations

import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# Время в строке — это время приёма, единственное, которое мы вообще знаем: время события
# на машине разработчика больше не разбирается и не хранится. Печатает его сам логгер,
# в поясе TZ (asctime идёт через localtime), а не код обработчика.
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cc-metrics")

# Уровень регулирует только наш логгер, а не asyncpg/uvicorn — иначе DEBUG выведет
# внутренние детали пула соединений на каждый запрос. DEBUG добавляет строки
# "принято записей: N, сохранено вызовов скиллов: N" и "получен сигнал /v1/metrics".
log.setLevel(getattr(logging, os.environ.get("CC_METRICS_LOG_LEVEL", "INFO").upper(), logging.INFO))

# Access-лог uvicorn пишет строку на каждый батч ("POST /v1/logs 200 OK"), а батчи
# приходят постоянно. Ради читаемости глушим его; стартовый баннер и ошибки остаются.
# Гасим в двух местах: здесь — для запуска через `uvicorn main:app`, и параметром
# access_log в main.py — для `python main.py`, где uvicorn.run переконфигурирует логирование.
ACCESS_LOG = os.environ.get("CC_METRICS_ACCESS_LOG", "0") not in ("0", "false", "")
logging.getLogger("uvicorn.access").disabled = not ACCESS_LOG

# Если задан, значение должно совпадать с токеном из OTEL_EXPORTER_OTLP_HEADERS.
# Пусто — проверка выключена.
AUTH_TOKEN = os.environ.get("CC_METRICS_TOKEN", "")

# HTTP Basic для браузерной /ui: Bearer-токен выше не годится для веб-страницы —
# его неоткуда взять при обычном заходе по ссылке. Обе переменные нужны одновременно,
# иначе страница остаётся открытой (тот же принцип, что и у CC_METRICS_TOKEN).
UI_USER = os.environ.get("CC_METRICS_UI_USER", "")
UI_PASSWORD = os.environ.get("CC_METRICS_UI_PASSWORD", "")
basic_auth = HTTPBasic(auto_error=False)

# Печатать ли все прочие события, кроме skill_activated. По умолчанию выключено:
# на 40 разработчиках поток api_request забивает лог, а хранилища с ротацией пока нет.
LOG_ALL_EVENTS = os.environ.get("CC_METRICS_LOG_ALL_EVENTS", "0") not in ("0", "false", "")

# Потолки на размер запроса. Батч OTLP от одного клиента — десятки килобайт, так что
# запас тут огромный. Нужны они против gzip-бомбы: сжатое тело в мегабайт
# разворачивается в гигабайты и убивает контейнер по памяти.
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024

# Пусто — работаем как раньше, только в лог. Так сервис остаётся живым, если база
# ещё не подключена или её временно убрали.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Уникальность по (session_id, event_sequence) делает вставку идемпотентной: OTLP-экспортёр
# повторяет батч при сбое доставки, и без этого ключа в базе появились бы дубли.
DDL = """
CREATE TABLE IF NOT EXISTS skill_usage (
    id              bigserial PRIMARY KEY,
    received_at     timestamptz NOT NULL DEFAULT now(),
    skill           text        NOT NULL,
    trigger         text,
    source          text,
    plugin          text,
    marketplace     text,
    skill_kind      text,
    session_id      text,
    event_sequence  bigint,
    terminal_type   text,
    UNIQUE (session_id, event_sequence)
);
CREATE INDEX IF NOT EXISTS skill_usage_received_at_idx ON skill_usage (received_at DESC);
CREATE INDEX IF NOT EXISTS skill_usage_skill_idx       ON skill_usage (skill);
"""

# received_at в списке колонок нет намеренно: его проставляет DEFAULT now() на стороне
# Postgres. Это единственное время, которое мы храним, и оно заведомо настоящее —
# часы клиента на него не влияют.
INSERT_SQL = """
INSERT INTO skill_usage (
    skill, trigger, source, plugin, marketplace,
    skill_kind, session_id, event_sequence, terminal_type
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (session_id, event_sequence) DO NOTHING
"""

# Мутируется в lifespan(); api.py и ui.py читают через `config.pool`, а не через
# `from config import pool` — иначе они увидели бы значение на момент импорта (None).
pool: asyncpg.Pool | None = None


def normalize_dsn(url: str) -> str:
    """asyncpg не понимает SQLAlchemy-схемы вида postgresql+asyncpg://."""
    for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool
    if DATABASE_URL:
        pool = await asyncpg.create_pool(normalize_dsn(DATABASE_URL), min_size=1, max_size=5)
        async with pool.acquire() as conn:
            await conn.execute(DDL)
        log.info("Postgres подключён, таблица skill_usage готова")
    else:
        log.warning("DATABASE_URL не задан — пишу только в лог, в базу ничего не сохраняю")
    yield
    if pool is not None:
        await pool.close()


def require_token(request: Request) -> None:
    """Проверка Bearer-токена. Пустой CC_METRICS_TOKEN — проверка выключена."""
    if not AUTH_TOKEN:
        return
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    # Сравниваем байты, а не строки: compare_digest на str с не-ASCII бросает TypeError,
    # а заголовок приходит из сети и может содержать что угодно.
    if not secrets.compare_digest(supplied.encode("utf-8", "surrogateescape"), AUTH_TOKEN.encode()):
        log.warning("отклонён запрос с неверным токеном от %s", request.client)
        raise HTTPException(status_code=401, detail="unauthorized")


def require_ui_auth(credentials: HTTPBasicCredentials | None = Depends(basic_auth)) -> None:
    """HTTP Basic для /ui. Обе переменные пустые — страница открыта без пароля."""
    if not (UI_USER and UI_PASSWORD):
        return
    ok = credentials is not None and secrets.compare_digest(
        credentials.username.encode("utf-8", "surrogateescape"), UI_USER.encode()
    ) and secrets.compare_digest(
        credentials.password.encode("utf-8", "surrogateescape"), UI_PASSWORD.encode()
    )
    if not ok:
        raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Basic"})
