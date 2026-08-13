"""Приём OTLP-логов на /v1/logs и разбор события claude_code.skill_activated."""

from __future__ import annotations

import gzip
import io
import json
import zlib
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import config
from config import log

router = APIRouter()


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


def drop_identity(attrs: dict[str, Any]) -> dict[str, Any]:
    """Выбрасывает атрибуты личности (`user.email`, `user.account_uuid`, `user.id`).

    Метрики анонимные: кто именно запустил скилл — не собираем и не храним. Отбираем по
    префиксу, а не по списку ключей: Claude Code может добавить новые `user.*`-атрибуты,
    и они не должны просочиться ни в лог, ни в базу.
    """
    return {key: value for key, value in attrs.items() if not key.startswith("user.")}


def decode_attributes(attributes: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Превращает список OTLP-атрибутов в плоский словарь."""
    result: dict[str, Any] = {}
    for attr in attributes or []:
        key = attr.get("key")
        if isinstance(key, str):
            result[key] = decode_any_value(attr.get("value") or {})
    return result


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
    # drop_identity — единственный барьер: дальше по коду личности в attrs уже нет,
    # поэтому она не попадёт ни в лог, ни в строку для вставки.
    attrs = drop_identity({**resource_attrs, **decode_attributes(record.get("attributes"))})
    event = attrs.get("event.name") or "?"

    if event != "skill_activated":
        if config.LOG_ALL_EVENTS:
            log.info("event  %s", event)
        return None

    log.info("SKILL  %s", describe_skill(attrs))

    sequence = attrs.get("event.sequence")
    return (
        str(attrs.get("skill.name") or "?"),
        attrs.get("invocation_trigger"),
        attrs.get("skill.source"),
        attrs.get("plugin.name"),
        attrs.get("marketplace.name"),
        attrs.get("skill.kind"),
        attrs.get("session.id"),
        sequence if isinstance(sequence, int) else None,
        attrs.get("terminal.type"),
    )


@router.post("/v1/logs", dependencies=[Depends(config.require_token)])
async def receive_logs(request: Request) -> JSONResponse:
    body = await read_body_capped(request, config.MAX_BODY_BYTES)
    if body is None:
        log.error("тело запроса больше %d байт — отклоняю", config.MAX_BODY_BYTES)
        return JSONResponse({"error": "payload too large"}, status_code=413)

    if request.headers.get("content-encoding", "").lower() == "gzip":
        unpacked = gunzip_capped(body, config.MAX_DECOMPRESSED_BYTES)
        if unpacked is None:
            log.error("gzip не распаковался или разросся больше %d байт", config.MAX_DECOMPRESSED_BYTES)
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

    if rows and config.pool is not None:
        try:
            async with config.pool.acquire() as conn:
                await conn.executemany(config.INSERT_SQL, rows)
        except Exception:
            # 503, а не 200: экспортёр повторит батч, а уникальный ключ не даст задвоиться.
            # Молча ответить 200 значило бы потерять события при любом сбое базы.
            log.exception("не смог записать %d строк в базу", len(rows))
            return JSONResponse({"error": "storage unavailable"}, status_code=503)

    log.debug("принято записей: %d, сохранено вызовов скиллов: %d", count, len(rows))
    return JSONResponse({"partialSuccess": {}})


@router.post("/v1/metrics", dependencies=[Depends(config.require_token)])
@router.post("/v1/traces", dependencies=[Depends(config.require_token)])
async def receive_ignored(request: Request) -> JSONResponse:
    """Метрики и трейсы сейчас выключены на клиентах, но если их включат — не отдаём 404."""
    # debug, а не info: иначе каждый батч метрик добавлял бы строку в лог.
    log.debug("получен сигнал %s, игнорирую", request.url.path)
    return JSONResponse({"partialSuccess": {}})
