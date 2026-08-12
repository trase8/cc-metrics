"""Приёмник OTLP-логов Claude Code.

Принимает поток событий на /v1/logs и печатает его в stdout в читаемом виде.
Целевое событие — claude_code.skill_activated, оно выделяется отдельной строкой.
Хранилища пока нет: всё уходит только в лог.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cc-metrics")

# Если задан, значение должно совпадать с токеном из OTEL_EXPORTER_OTLP_HEADERS.
# Пусто — проверка выключена.
AUTH_TOKEN = os.environ.get("CC_METRICS_TOKEN", "")

# Печатать ли все прочие события, кроме skill_activated.
LOG_ALL_EVENTS = os.environ.get("CC_METRICS_LOG_ALL_EVENTS", "1") not in ("0", "false", "")

app = FastAPI(title="cc-metrics")


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


def format_time(record: dict[str, Any], attrs: dict[str, Any]) -> str:
    """Время события: сначала наносекунды из записи, затем ISO-атрибут, затем «сейчас»."""
    for field in ("timeUnixNano", "observedTimeUnixNano"):
        raw = record.get(field)
        if isinstance(raw, (str, int)):
            try:
                return datetime.fromtimestamp(int(raw) / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError):
                pass
    iso = attrs.get("event.timestamp")
    if isinstance(iso, str):
        return iso
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def handle_record(record: dict[str, Any], resource_attrs: dict[str, Any]) -> None:
    attrs = {**resource_attrs, **decode_attributes(record.get("attributes"))}
    event = attrs.get("event.name") or "?"
    who = attrs.get("user.email") or attrs.get("user.account_uuid") or attrs.get("user.id") or "?"
    when = format_time(record, attrs)

    if event == "skill_activated":
        log.info("SKILL  %s  %s  %s", when, who, describe_skill(attrs))
    elif LOG_ALL_EVENTS:
        log.info("event  %s  %s  %s", when, who, event)


@app.post("/v1/logs")
async def receive_logs(request: Request) -> JSONResponse:
    if AUTH_TOKEN:
        header = request.headers.get("authorization", "")
        if header.removeprefix("Bearer ").strip() != AUTH_TOKEN:
            log.warning("отклонён запрос с неверным токеном от %s", request.client)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.body()
    if request.headers.get("content-encoding", "").lower() == "gzip":
        body = gzip.decompress(body)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        log.error("не разобрал тело запроса: %s", exc)
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # Ошибка разбора одной записи не должна ронять весь батч: экспортёр начнёт его
    # переслать заново и завалит приёмник повторами.
    count = 0
    for resource_log in payload.get("resourceLogs", []):
        resource_attrs = decode_attributes((resource_log.get("resource") or {}).get("attributes"))
        for scope_log in resource_log.get("scopeLogs", []):
            for record in scope_log.get("logRecords", []):
                count += 1
                try:
                    handle_record(record, resource_attrs)
                except Exception:
                    log.exception("не смог обработать запись: %s", json.dumps(record)[:500])

    log.debug("принято записей: %d", count)
    return JSONResponse({"partialSuccess": {}})


@app.post("/v1/metrics")
@app.post("/v1/traces")
async def receive_ignored(request: Request) -> JSONResponse:
    """Метрики и трейсы сейчас выключены на клиентах, но если их включат — не отдаём 404."""
    log.info("получен сигнал %s, игнорирую", request.url.path)
    return JSONResponse({"partialSuccess": {}})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "4318")))
