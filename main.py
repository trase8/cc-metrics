"""Приёмник OTLP-логов Claude Code.

Принимает поток событий на /v1/logs, печатает в stdout и (если задан DATABASE_URL)
сохраняет вызовы скиллов в Postgres. Просмотр — на /ui.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import secrets
import sys
import zlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# Без asctime: в строке уже печатается время самого события, а время приёма
# добавит рантайм (docker logs -t, journald и т.п.).
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(levelname)-5s %(message)s",
)
log = logging.getLogger("cc-metrics")

# Уровень регулирует только наш логгер, а не asyncpg/uvicorn — иначе DEBUG выведет
# внутренние детали пула соединений на каждый запрос. DEBUG добавляет строки
# "принято записей: N, сохранено вызовов скиллов: N" и "получен сигнал /v1/metrics".
log.setLevel(getattr(logging, os.environ.get("CC_METRICS_LOG_LEVEL", "INFO").upper(), logging.INFO))

# Access-лог uvicorn пишет строку на каждый батч ("POST /v1/logs 200 OK"), а батчи
# приходят постоянно. Ради читаемости глушим его; стартовый баннер и ошибки остаются.
# Гасим в двух местах: здесь — для запуска через `uvicorn main:app`, и параметром
# access_log ниже — для `python main.py`, где uvicorn.run переконфигурирует логирование.
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
    occurred_at     timestamptz NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    user_email      text        NOT NULL,
    skill           text        NOT NULL,
    trigger         text,
    source          text,
    plugin          text,
    marketplace     text,
    skill_kind      text,
    session_id      text,
    event_sequence  bigint,
    department      text,
    team_id         text,
    terminal_type   text,
    raw             jsonb,
    UNIQUE (session_id, event_sequence)
);
CREATE INDEX IF NOT EXISTS skill_usage_occurred_at_idx ON skill_usage (occurred_at DESC);
CREATE INDEX IF NOT EXISTS skill_usage_user_skill_idx  ON skill_usage (user_email, skill);
"""

INSERT_SQL = """
INSERT INTO skill_usage (
    occurred_at, user_email, skill, trigger, source, plugin, marketplace,
    skill_kind, session_id, event_sequence, department, team_id, terminal_type, raw
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
ON CONFLICT (session_id, event_sequence) DO NOTHING
"""

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


app = FastAPI(title="cc-metrics", lifespan=lifespan)


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


async def read_body_capped(request: Request, limit: int) -> bytes | None:
    """Читает тело потоком и обрывает, если превысило лимит. None — превысило."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def gunzip_capped(data: bytes, limit: int) -> bytes | None:
    """Распаковывает gzip порциями и обрывает на лимите. None — превысило или битый архив."""
    out = bytearray()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as fh:
            while chunk := fh.read(64 * 1024):
                out += chunk
                if len(out) > limit:
                    return None
    except (OSError, EOFError, zlib.error):
        # zlib.error — не подкласс OSError: битая середина архива прилетает именно им.
        return None
    return bytes(out)


def decode_any_value(value: dict[str, Any]) -> Any:
    """Разворачивает OTLP AnyValue в обычный питоновский тип.

    В OTLP/JSON int64 приезжает строкой — приводим к int, иначе сравнения ломаются.
    """
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return value["intValue"]
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        return [decode_any_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return decode_attributes(value["kvlistValue"].get("values", []))
    if "bytesValue" in value:
        return value["bytesValue"]
    return None


def decode_attributes(attributes: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Превращает список OTLP-атрибутов в плоский словарь."""
    result: dict[str, Any] = {}
    for attr in attributes or []:
        key = attr.get("key")
        if isinstance(key, str):
            result[key] = decode_any_value(attr.get("value") or {})
    return result


def parse_occurred_at(record: dict[str, Any], attrs: dict[str, Any]) -> datetime:
    """Время самого события (не приёма): наносекунды из записи, затем ISO-атрибут, затем «сейчас».

    Всегда с таймзоной — колонка в базе timestamptz, наивный datetime туда класть нельзя.
    """
    for field in ("timeUnixNano", "observedTimeUnixNano"):
        raw = record.get(field)
        if isinstance(raw, (str, int)):
            try:
                return datetime.fromtimestamp(int(raw) / 1_000_000_000, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                pass
    iso = attrs.get("event.timestamp")
    if isinstance(iso, str):
        try:
            parsed = datetime.fromisoformat(iso)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def describe_skill(attrs: dict[str, Any]) -> str:
    skill = attrs.get("skill.name", "?")
    if skill == "custom_skill":
        # Имя заредактировано — значит, на клиентах не выставлен OTEL_LOG_TOOL_DETAILS=1.
        skill = "custom_skill (!! включи OTEL_LOG_TOOL_DETAILS=1)"
    parts = [f"skill={skill}"]
    for key, label in (
        ("invocation_trigger", "trigger"),
        ("skill.source", "source"),
        ("skill.kind", "kind"),
        ("plugin.name", "plugin"),
    ):
        if attrs.get(key):
            parts.append(f"{label}={attrs[key]}")
    return "  ".join(parts)


def handle_record(record: dict[str, Any], resource_attrs: dict[str, Any]) -> tuple | None:
    """Печатает запись в лог и возвращает строку для вставки, если это вызов скилла."""
    attrs = {**resource_attrs, **decode_attributes(record.get("attributes"))}
    event = attrs.get("event.name") or "?"
    who = attrs.get("user.email") or attrs.get("user.account_uuid") or attrs.get("user.id") or "?"
    occurred_at = parse_occurred_at(record, attrs)
    when = occurred_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    if event != "skill_activated":
        if LOG_ALL_EVENTS:
            log.info("event  %s  %s  %s", when, who, event)
        return None

    log.info("SKILL  %s  %s  %s", when, who, describe_skill(attrs))

    sequence = attrs.get("event.sequence")
    return (
        occurred_at,
        str(who),
        str(attrs.get("skill.name") or "?"),
        attrs.get("invocation_trigger"),
        attrs.get("skill.source"),
        attrs.get("plugin.name"),
        attrs.get("marketplace.name"),
        attrs.get("skill.kind"),
        attrs.get("session.id"),
        sequence if isinstance(sequence, int) else None,
        attrs.get("department"),
        attrs.get("team.id"),
        attrs.get("terminal.type"),
        json.dumps(attrs, ensure_ascii=False, default=str),
    )


@app.post("/v1/logs", dependencies=[Depends(require_token)])
async def receive_logs(request: Request) -> JSONResponse:
    body = await read_body_capped(request, MAX_BODY_BYTES)
    if body is None:
        log.error("тело запроса больше %d байт — отклоняю", MAX_BODY_BYTES)
        return JSONResponse({"error": "payload too large"}, status_code=413)

    if request.headers.get("content-encoding", "").lower() == "gzip":
        unpacked = gunzip_capped(body, MAX_DECOMPRESSED_BYTES)
        if unpacked is None:
            log.error("gzip не распаковался или разросся больше %d байт", MAX_DECOMPRESSED_BYTES)
            return JSONResponse({"error": "payload too large"}, status_code=413)
        body = unpacked

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        content_type = request.headers.get("content-type", "")
        if "protobuf" in content_type:
            # Самая вероятная ошибка настройки: клиент шлёт protobuf, а мы понимаем только JSON.
            log.error(
                "клиент прислал protobuf (%s) — приёмник понимает только JSON. "
                "Поставь OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
                content_type,
            )
        else:
            log.error("не разобрал тело запроса (content-type=%r): %s", content_type, exc)
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # Ошибка разбора одной записи не должна ронять весь батч: экспортёр начнёт его
    # переслать заново и завалит приёмник повторами.
    count = 0
    rows: list[tuple] = []
    for resource_log in payload.get("resourceLogs", []):
        resource_attrs = decode_attributes((resource_log.get("resource") or {}).get("attributes"))
        for scope_log in resource_log.get("scopeLogs", []):
            for record in scope_log.get("logRecords", []):
                count += 1
                try:
                    row = handle_record(record, resource_attrs)
                except Exception:
                    log.exception("не смог обработать запись: %s", json.dumps(record)[:500])
                    continue
                if row is not None:
                    rows.append(row)

    if rows and pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.executemany(INSERT_SQL, rows)
        except Exception:
            # 503, а не 200: экспортёр повторит батч, а уникальный ключ не даст задвоиться.
            # Молча ответить 200 значило бы потерять события при любом сбое базы.
            log.exception("не смог записать %d строк в базу", len(rows))
            return JSONResponse({"error": "storage unavailable"}, status_code=503)

    log.debug("принято записей: %d, сохранено вызовов скиллов: %d", count, len(rows))
    return JSONResponse({"partialSuccess": {}})


@app.post("/v1/metrics", dependencies=[Depends(require_token)])
@app.post("/v1/traces", dependencies=[Depends(require_token)])
async def receive_ignored(request: Request) -> JSONResponse:
    """Метрики и трейсы сейчас выключены на клиентах, но если их включат — не отдаём 404."""
    # debug, а не info: иначе каждый батч метрик добавлял бы строку в лог.
    log.debug("получен сигнал %s, игнорирую", request.url.path)
    return JSONResponse({"partialSuccess": {}})


UI_PAGE_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>cc-metrics</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 2rem; color: #1a1a1a; background: #fff; }}
  h1 {{ font-size: 1.2rem; }}
  form {{ display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: end; margin-bottom: 1rem; }}
  label {{ display: flex; flex-direction: column; font-size: 0.75rem; color: #555; }}
  input, select {{ padding: 0.35rem; font-size: 0.9rem; }}
  button {{ padding: 0.4rem 0.9rem; cursor: pointer; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
  th, td {{ border-bottom: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: left; white-space: nowrap; }}
  th {{ background: #f5f5f5; position: sticky; top: 0; }}
  .muted {{ color: #777; font-size: 0.85rem; }}
  .error {{ color: #b00020; }}
  .trigger-claude-proactive {{ color: #7a4de8; }}
</style></head>
<body>
<h1>Вызовы скиллов Claude Code</h1>
<form method="get" action="/ui">
  <label>Пользователь<input type="text" name="user" value="{user}" placeholder="vasya@01.tech"></label>
  <label>Скилл<input type="text" name="skill" value="{skill}" placeholder="cr"></label>
  <label>Триггер<select name="trigger">{trigger_options}</select></label>
  <label>С<input type="date" name="since" value="{since}"></label>
  <label>По<input type="date" name="until" value="{until}"></label>
  <label>Лимит<input type="number" name="limit" value="{limit}" min="1" max="1000"></label>
  <button type="submit">Фильтровать</button>
</form>
{body}
</body></html>"""

TRIGGER_LABELS = {
    "user-slash": "человек (/команда)",
    "claude-proactive": "модель сама",
    "nested-skill": "вложенный",
}


def render_ui_page(*, error: str = "", rows=None, total: int = 0, filters: dict[str, Any]) -> str:
    options = ['<option value="">любой</option>']
    for value, label in TRIGGER_LABELS.items():
        selected = " selected" if filters["trigger"] == value else ""
        options.append(f'<option value="{value}"{selected}>{escape(label)}</option>')

    if error:
        body = f'<p class="error">{escape(error)}</p>'
    elif not rows:
        body = '<p class="muted">Ничего не найдено.</p>'
    else:
        head = ("<tr><th>Когда</th><th>Пользователь</th><th>Скилл</th><th>Триггер</th>"
                "<th>Источник</th><th>Плагин</th><th>Команда</th><th>Терминал</th></tr>")
        lines = []
        for r in rows:
            trigger = r["trigger"] or ""
            trigger_class = f' class="trigger-{escape(trigger)}"' if trigger else ""
            lines.append(
                "<tr>"
                f"<td>{r['occurred_at'].astimezone().strftime('%Y-%m-%d %H:%M:%S')}</td>"
                f"<td>{escape(r['user_email'])}</td>"
                f"<td>{escape(r['skill'])}</td>"
                f"<td{trigger_class}>{escape(TRIGGER_LABELS.get(trigger, trigger))}</td>"
                f"<td>{escape(r['source'] or '')}</td>"
                f"<td>{escape(r['plugin'] or '')}</td>"
                f"<td>{escape(r['team_id'] or r['department'] or '')}</td>"
                f"<td>{escape(r['terminal_type'] or '')}</td>"
                "</tr>"
            )
        shown = f"показаны {len(rows)} из {total}" if total > len(rows) else f"всего {total}"
        body = f'<p class="muted">{shown}</p><table>{head}{"".join(lines)}</table>'

    return UI_PAGE_TEMPLATE.format(
        user=escape(filters["user"]),
        skill=escape(filters["skill"]),
        trigger_options="".join(options),
        since=escape(filters["since"]),
        until=escape(filters["until"]),
        limit=filters["limit"],
        body=body,
    )


def parse_day_param(value: str, *, end_of_day: bool) -> datetime | None:
    """Разбирает значение <input type="date"> (YYYY-MM-DD) в границу суток UTC."""
    if not value:
        return None
    try:
        day = datetime.fromisoformat(value)
    except ValueError:
        return None
    if end_of_day:
        day = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day.replace(tzinfo=timezone.utc)


@app.get("/ui", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
async def ui(
    user: str = Query(""),
    skill: str = Query(""),
    trigger: str = Query(""),
    since: str = Query(""),
    until: str = Query(""),
    limit: int = Query(200, ge=1, le=1000),
) -> HTMLResponse:
    filters = {"user": user, "skill": skill, "trigger": trigger, "since": since, "until": until, "limit": limit}

    if pool is None:
        return HTMLResponse(
            render_ui_page(error="DATABASE_URL не задан — приёмник пишет только в лог, смотреть нечего.",
                            filters=filters),
            status_code=503,
        )

    conditions = []
    params: list[Any] = []
    if user:
        params.append(f"%{user}%")
        conditions.append(f"user_email ILIKE ${len(params)}")
    if skill:
        params.append(f"%{skill}%")
        conditions.append(f"skill ILIKE ${len(params)}")
    if trigger:
        params.append(trigger)
        conditions.append(f"trigger = ${len(params)}")
    since_dt = parse_day_param(since, end_of_day=False)
    if since_dt:
        params.append(since_dt)
        conditions.append(f"occurred_at >= ${len(params)}")
    until_dt = parse_day_param(until, end_of_day=True)
    if until_dt:
        params.append(until_dt)
        conditions.append(f"occurred_at <= ${len(params)}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    sql = (
        "SELECT occurred_at, user_email, skill, trigger, source, plugin, department, team_id, terminal_type, "
        "count(*) OVER() AS total_count "
        f"FROM skill_usage {where} ORDER BY occurred_at DESC LIMIT ${len(params)}"
    )

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
    except Exception:
        log.exception("не смог прочитать skill_usage для /ui")
        return HTMLResponse(render_ui_page(error="Не смог прочитать данные из базы.", filters=filters),
                             status_code=503)

    total = rows[0]["total_count"] if rows else 0
    return HTMLResponse(render_ui_page(rows=rows, total=total, filters=filters))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "4318")),
        access_log=ACCESS_LOG,
    )
