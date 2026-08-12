# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Назначение

`cc-metrics` — приёмник телеметрии Claude Code для организации на плане Claude for Teams (~40 разработчиков).

**Единственная целевая метрика — факты использования скиллов**: кто, когда, какой скилл запустил. Всё
остальное, что прилетает по OTel, — побочный поток, который отбрасывается.

## Приёмник

`main.py` — FastAPI-приложение, принимает OTLP/JSON на `POST /v1/logs`, пишет вызовы скиллов в Postgres
(если задан `DATABASE_URL`) и отдаёт их же на `GET /ui`.

Что важно знать про формат OTLP и уже учтено в коде:

- Приходит стандартная кодировка: `resourceLogs[].scopeLogs[].logRecords[]`, атрибуты — массив
  `{"key": ..., "value": {"stringValue": ...}}`. В OTLP/JSON `intValue` всегда строка.
- Экспортёр ждёт ответ вида `{"partialSuccess": {}}`.
- Ошибка обработки одной записи не должна ронять батч: иначе экспортёр начнёт слать его повторно.
- Тело может быть сжато gzip. Стоят потолки на размер тела и на объём распаковки — против gzip-бомбы.
- `/v1/metrics` и `/v1/traces` отвечают 200 и игнорируются: на клиентах они выключены, но 404 здесь
  только путал бы при отладке.
- Разбирается **только JSON**. При `http/protobuf` приёмник отвечает 400 и пишет в лог, что менять.
- Если запись в базу упала — ответ `/v1/logs` **503**, а не 200: OTLP-экспортёр повторит батч, а
  уникальный индекс `(session_id, event_sequence)` не даст задвоить строки. Молча ответить 200 значило
  бы потерять события при любом сбое базы.

По умолчанию в лог идут только строки `SKILL`. Отфильтровать поток на стороне клиента нельзя — Claude Code
шлёт все события подряд, отбор возможен только здесь. Поэтому глушатся два источника шума: прочие события
и access-лог uvicorn, который иначе печатал бы строку на каждый батч. Ошибки, отказы по токену и стартовый
баннер остаются всегда.

| Переменная | Смысл |
| --- | --- |
| `DATABASE_URL` | подключение к Postgres. Пусто — пишем только в лог, без хранилища |
| `CC_METRICS_TOKEN` | Bearer-токен для `/v1/logs`, `/v1/metrics`, `/v1/traces`; пусто — проверка выключена |
| `CC_METRICS_UI_USER`, `CC_METRICS_UI_PASSWORD` | HTTP Basic для `/ui`; нужны обе, иначе страница открыта |
| `CC_METRICS_LOG_LEVEL` | `INFO` (по умолчанию) / `DEBUG` / `WARNING` / `ERROR` — уровень только нашего логгера |
| `CC_METRICS_LOG_ALL_EVENTS` | по умолчанию `0` — только `skill_activated`; `1` — весь поток событий |
| `CC_METRICS_ACCESS_LOG` | по умолчанию `0` — access-лог uvicorn заглушен; `1` — для отладки |
| `PORT` | порт (по умолчанию 4318 — стандартный для OTLP поверх HTTP) |
| `TZ` | пояс для времени событий; без неё UTC |

```bash
uv run main.py           # http://127.0.0.1:4318, страница просмотра — /ui
uv add <package>         # зависимости; правит pyproject.toml + uv.lock
uv sync                  # привести .venv в соответствие с pyproject.toml
```

Окружение управляется **uv** (CPython 3.14). Не использовать `pip install` и не активировать venv вручную.
Тестового раннера, линтера и форматтера в проекте нет — если понадобятся, добавлять через `uv add --dev`.

## Хранилище: таблица `skill_usage`

Единственная таблица, создаётся приёмником при старте (`CREATE TABLE IF NOT EXISTS`), отдельных миграций
нет. `DATABASE_URL` — стандартный DSN (`postgresql://user:pass@host:port/db`); схемы SQLAlchemy вида
`postgresql+asyncpg://` тоже принимаются, префикс срезается перед передачей в asyncpg.

Уникальный индекс `(session_id, event_sequence)` — не техническая деталь, а условие корректности: без
него повторная доставка батча задваивает счётчики использования скиллов, и незаметно.

`/ui` — серверный HTML без JS-фреймворка, фильтры (пользователь, скилл, триггер, диапазон дат) через
GET-параметры формы. Работает и при пустой базе, и без `DATABASE_URL` (в этом случае явно говорит, что
хранилища нет, вместо пустой страницы).

## Данные: событие `claude_code.skill_activated`

Логируется и когда скилл вызывает модель через тул `Skill`, и когда человек набирает `/команду`.

| Атрибут | Значение |
| --- | --- |
| `event.timestamp` / `timeUnixNano` | время события |
| `event.sequence` | монотонный счётчик внутри сессии |
| `skill.name` | имя скилла |
| `invocation_trigger` | `user-slash` (набрал руками) / `claude-proactive` (модель решила сама) / `nested-skill` |
| `skill.source` | `bundled` / `userSettings` / `projectSettings` / `plugin` |
| `skill.kind` | `workflow` у workflow-скиллов, иначе отсутствует |
| `plugin.name`, `marketplace.name` | владелец скилла, если он из плагина |

Личность приходит в стандартных атрибутах любого события: `user.email` (при OAuth-логине — наш случай),
`user.account_uuid`, `organization.id`, `session.id`, `terminal.type`. Прокидывать её отдельно не нужно.

При анализе не считай `user-slash` единственным «настоящим» использованием: `claude-proactive` — это
полноценный вызов, и для оценки полезности скилла он даже интереснее.

## Ловушка: без `OTEL_LOG_TOOL_DETAILS=1` данные бесполезны

У пользовательских скиллов и скиллов из сторонних плагинов `skill.name` подменяется на плейсхолдер
`custom_skill`. Целевые скиллы (`01-dev-pipeline:cr`, `01-dev-pipeline:arch-check`) попадают именно в эту
категорию — без флага придёт поток неразличимых `custom_skill`. Приёмник помечает такие строки в логе.

Плата за флаг: он включает логирование параметров тулов вообще — команды Bash и входные аргументы тулов
в событиях `tool_result` / `tool_decision`. По проводу это уйдёт в любом случае, отбрасывать нужно здесь.

## Конфигурация клиентов

Ставится централизованно в **Admin Settings → Claude Code → Managed settings**
([claude.ai/admin-settings/claude-code](https://claude.ai/admin-settings/claude-code)) — на машинах
разработчиков делать ничего не нужно. Для обкатки на одном человеке тот же блок кладётся
в личный `~/.claude/settings.json`.

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://cc-metrics.quickname.tech",
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer <токен>",
    "OTEL_LOG_TOOL_DETAILS": "1",
    "OTEL_RESOURCE_ATTRIBUTES": "department=rnd,team.id=ai-sdlc"
  }
}
```

- События — сигнал **logs**, поэтому `OTEL_LOGS_EXPORTER=otlp` обязателен. Без него не уедет ничего,
  каким бы правильным ни был endpoint.
- `CLAUDE_CODE_ENABLE_TELEMETRY` при значении `"0"` выключает всё, остальные переменные обессмысливаются.
- В `OTEL_EXPORTER_OTLP_ENDPOINT` указывается база **без** `/v1/logs` — путь экспортёр добавляет сам.
  В per-signal варианте `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` путь пишется целиком.
- OTel-конфигурация не применяется на лету — нужен полный перезапуск Claude Code.
- Редактировать managed settings может только **Owner / Primary Owner**; роль Admin страницу не видит.
- Каждый разработчик один раз увидит security-диалог (его вызывает непустой endpoint). Отказ завершает
  Claude Code.

## Деплой

Coolify, сборка из `Dockerfile`. `uv.lock` обязан быть в репозитории — зависимости ставятся с `--frozen`.
Healthcheck — `GET /health`, HTTPS терминирует Traefik самого Coolify.

Порт по умолчанию 4318, но берётся из `PORT`. Подходит любой вариант: «Ports Exposes» = 4318 в Coolify
либо `PORT=3000` под порт платформы. Несовпадение даёт 502 от Traefik на всех путях — это первое, что
стоит проверять, если домен отвечает, а сервис нет.

## Git

`git@github.com:trase8/cc-metrics.git`, ветка `main`, репозиторий **публичный**. Секретов в файлах нет
и быть не должно — токен и строка подключения приходят только через переменные окружения.

Коммиты делает только пользователь: не выполнять `git commit` и не предлагать его, пока не попросят явно.
