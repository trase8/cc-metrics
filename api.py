"""Приём OTLP-логов на /v1/logs и разбор события claude_code.skill_activated."""

from __future__ import annotations

import gzip
import io
import json
import re
import zlib
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import config
from config import log

router = APIRouter()

# Длина имени скилла и прочих текстовых атрибутов ничем не ограничена: по проводу приезжает
# что прислали, и стотысячный "A" уедет и в лог, и в text-колонку целиком.
MAX_TEXT_LEN = 200

# Управляющие символы вырезаем: перевод строки внутри skill.name даёт в логе строку,
# неотличимую от настоящей записи приёмника. Заодно C1-диапазон, ломающий терминал.
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


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


def clean_text(value: Any) -> str | None:
    """Готовит пришедшее по сети значение к печати в лог и к записи в базу.

    Вычищает управляющие символы и режет длину — см. CONTROL_CHARS и MAX_TEXT_LEN.
    None остаётся None: в базе это отсутствующий атрибут, а не строка "None".
    """
    if value is None:
        return None
    return CONTROL_CHARS.sub(" ", str(value))[:MAX_TEXT_LEN]


def decode_any_value(value: Any) -> Any:
    """Разворачивает OTLP AnyValue в обычный питоновский тип.

    В OTLP/JSON int64 приезжает строкой — приводим к int, иначе сравнения ломаются.
    """
    if not isinstance(value, dict):
        # По проводу вместо объекта может приехать что угодно, а `"x" in 5` — это TypeError.
        return None
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


def decode_attributes(attributes: Any) -> dict[str, Any]:
    """Превращает список OTLP-атрибутов в плоский словарь.

    Терпимо к мусору: вместо списка может приехать объект или строка, а `.get()` на строке —
    это 500 мимо try вокруг handle_record().
    """
    result: dict[str, Any] = {}
    for attr in attributes if isinstance(attributes, list) else []:
        if not isinstance(attr, dict):
            continue
        key = attr.get("key")
        if isinstance(key, str):
            result[key] = decode_any_value(attr.get("value"))
    return result


def dicts(value: Any) -> list[dict[str, Any]]:
    """Отбирает из значения только объекты. Всё, что не список объектов, — не наш формат."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def describe_skill(attrs: dict[str, Any]) -> str:
    skill = clean_text(attrs.get("skill.name")) or "?"
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
            parts.append(f"{label}={clean_text(attrs[key])}")
    return "  ".join(parts)


def describe_broken_record(record: Any) -> str:
    """Безопасное для лога описание записи, которую не удалось разобрать.

    Печатаем только имена полей и типы значений, без самих значений: в сырой записи лежит
    личность (`user.email`), а drop_identity() к моменту ошибки ещё не отработал. Отфильтровать
    её по префиксу здесь нельзя — ломается как раз тот случай, когда attributes пришли не
    списком и обойти их поэлементно уже не получится. Для отладки формы записи типов хватает.
    """
    if not isinstance(record, dict):
        return f"<{type(record).__name__}>"
    return json.dumps({k: type(v).__name__ for k, v in sorted(record.items())})[:500]


def handle_record(record: dict[str, Any], resource_attrs: dict[str, Any]) -> config.SkillUsageRow | None:
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

    # Только по именам: позиционный вызов вернул бы ту самую ловушку, ради которой
    # заведён SkillUsageRow — перепутанные местами nullable-колонки не дают ошибки.
    sequence = attrs.get("event.sequence")
    return config.SkillUsageRow(
        skill=clean_text(attrs.get("skill.name")) or "?",
        trigger=clean_text(attrs.get("invocation_trigger")),
        source=clean_text(attrs.get("skill.source")),
        plugin=clean_text(attrs.get("plugin.name")),
        marketplace=clean_text(attrs.get("marketplace.name")),
        skill_kind=clean_text(attrs.get("skill.kind")),
        session_id=clean_text(attrs.get("session.id")),
        event_sequence=sequence if isinstance(sequence, int) else None,
        terminal_type=clean_text(attrs.get("terminal.type")),
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
    except (json.JSONDecodeError, RecursionError) as exc:
        # RecursionError — на глубоко вложенном JSON вида [[[[…]]]]. Тоже кривое тело, тоже 400.
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

    if not isinstance(payload, dict):
        # Дальше везде .get(), а на списке или строке это AttributeError мимо try ниже,
        # то есть 500 вместо внятного отказа.
        log.error("тело не объект, а %s — отклоняю", type(payload).__name__)
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # Ошибка разбора одной записи не должна ронять весь батч: экспортёр начнёт его
    # переслать заново и завалит приёмник повторами.
    count = 0
    rows: list[config.SkillUsageRow] = []
    for resource_log in dicts(payload.get("resourceLogs")):
        resource = resource_log.get("resource")
        resource_attrs = decode_attributes(
            resource.get("attributes") if isinstance(resource, dict) else None
        )
        for scope_log in dicts(resource_log.get("scopeLogs")):
            for record in dicts(scope_log.get("logRecords")):
                count += 1
                try:
                    row = handle_record(record, resource_attrs)
                except Exception:
                    log.exception("не смог обработать запись: %s", describe_broken_record(record))
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
