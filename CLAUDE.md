# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Назначение проекта

`cc-metrics` — бэкенд-приёмник телеметрии Claude Code для организации на плане Claude for Teams (~40 разработчиков
под одной подпиской).

**Единственная целевая метрика — факты использования скиллов**: кто, когда, какой скилл запустил
(«Вася в 10:00 использовал скилл кодревью»). Всё остальное, что прилетит, — побочный поток, который можно
отбрасывать.

Код пока не написан: в репозитории только скелет PyCharm + uv (`main.py` — сгенерированная IDE заглушка
`print_hi()`). Когда появится реальный приёмник, замени этот раздел описанием фактической архитектуры.

## Как организован сбор

Централизованно, через **server-managed settings** в claude.ai: **Admin Settings → Claude Code → Managed settings**
([claude.ai/admin-settings/claude-code](https://claude.ai/admin-settings/claude-code)). Ничего ставить на машины
разработчиков не нужно — настройки доставляются клиентам при старте и раз в час опросом.

Рабочая конфигурация:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://<коллектор>",
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer <токен>",
    "OTEL_LOG_TOOL_DETAILS": "1"
  }
}
```

События — это сигнал **logs**, а не metrics. Поэтому `OTEL_LOGS_EXPORTER=otlp` обязателен, а метрики и трейсы
выключены, чтобы не гнать лишнее. При `http/json` клиент шлёт обычный JSON POST на `<endpoint>/v1/logs`
в стандартной кодировке OTLP: `resourceLogs[].scopeLogs[].logRecords[]`, атрибуты — массив
`{"key": ..., "value": {"stringValue": ...}}`.

## Событие, ради которого всё делается

`claude_code.skill_activated` — логируется и когда скилл вызывает модель через тул `Skill`, и когда человек
набирает `/команду`.

| Атрибут | Значение |
| --- | --- |
| `event.name` | `"skill_activated"` |
| `event.timestamp` | ISO 8601 |
| `event.sequence` | монотонный счётчик внутри сессии |
| `skill.name` | имя скилла |
| `invocation_trigger` | `"user-slash"` / `"claude-proactive"` / `"nested-skill"` |
| `skill.source` | `"bundled"` / `"userSettings"` / `"projectSettings"` / `"plugin"` |
| `skill.kind` | `"workflow"` у workflow-скиллов, иначе отсутствует |
| `plugin.name`, `marketplace.name` | владелец скилла, если он из плагина |

Идентификация человека приходит в стандартных атрибутах любого события: `user.email` (при OAuth-логине —
это наш случай), плюс `user.account_uuid`, `organization.id`, `session.id`, `terminal.type`. Отдельно
прокидывать личность не нужно.

## Обкатка на одном пользователе

До раскатки на всю организацию конфиг ставится не в админку, а в личный
`~/.claude/settings.json`, в блок `env` — так поток идёт только с одной машины:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://<домен>",
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer <токен>",
    "OTEL_LOG_TOOL_DETAILS": "1",
    "OTEL_RESOURCE_ATTRIBUTES": "department=rnd,team.id=ai-sdlc"
  }
}
```

Грабли, на которые тут легко наступить:

- `CLAUDE_CODE_ENABLE_TELEMETRY` должен быть `"1"`. Значение `"0"` выключает вообще всё, и остальные
  переменные становятся бессмысленны.
- `OTEL_LOGS_EXPORTER` обязателен: события — это сигнал logs, без него не уедет ничего, сколько бы
  ни был настроен endpoint.
- Протокол именно `http/json`. Приёмник разбирает только JSON; при `http/protobuf` он ответит 400
  и напишет в лог, что именно не так.
- В **общем** `OTEL_EXPORTER_OTLP_ENDPOINT` указывается база без `/v1/logs` — путь экспортёр добавляет сам.
  А вот в per-signal варианте `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` путь пишется целиком.
- OTel-конфигурация не применяется на лету: нужен полный перезапуск Claude Code.

Когда обкатка пройдена, тот же блок `env` переезжает в **Admin Settings → Claude Code → Managed settings**
и раскатывается на всех (см. разделы выше).

## Два пути вызова скилла — оба ловятся одним событием

Скилл запускается либо человеком (`/имя-скилла`), либо самой моделью, которая распознала интент и решила,
что скилл нужен. Это разные кодовые пути, но `claude_code.skill_activated` покрывает оба, а различает их
атрибут `invocation_trigger`:

| Значение | Что произошло |
| --- | --- |
| `"user-slash"` | человек набрал `/имя-скилла` |
| `"claude-proactive"` | модель вызвала скилл сама, через тул `Skill` |
| `"nested-skill"` | скилл был вызван из другого скилла |

Это главный аргумент в пользу OTel: одно событие, один парсер, разрез «сам вызвал / модель предложила»
достаётся бесплатно. При разборе данных не считай `user-slash` единственным «настоящим» использованием —
`claude-proactive` это тоже полноценный вызов, и для оценки полезности скилла он даже интереснее.

## Критично: без `OTEL_LOG_TOOL_DETAILS=1` данные бесполезны

У пользовательских скиллов и скиллов из сторонних плагинов `skill.name` подменяется на плейсхолдер
`"custom_skill"`, если флаг не выставлен. Наши целевые скиллы (`01-dev-pipeline:cr`, `01-dev-pipeline:arch-check`
и прочие из внутренних плагинов) попадают именно в эту категорию — без флага в бэкенд придёт поток
неразличимых `custom_skill`.

Плата за флаг: он включает логирование параметров тулов вообще — команды Bash, имена MCP-серверов и тулов,
входные аргументы тулов в событиях `tool_result` / `tool_decision`. То есть на приёмник поедут команды
и входные данные тулов всех 40 разработчиков. Отбрасывать лишнее нужно на нашей стороне (или фильтром
в OpenTelemetry Collector), но по проводу оно всё равно уйдёт.

## Что нужно знать про раскатку

- **Роль**: редактировать managed settings может только **Owner / Primary Owner**. Роль Admin страницу
  не видит — ссылка отредиректит на другой раздел.
- **Один клик от каждого разработчика неизбежен**: непустой `OTEL_EXPORTER_OTLP_ENDPOINT` всегда вызывает
  security-диалог при старте. Диалог перечисляет доставляемые переменные. Отказ = Claude Code завершается.
  Повторно диалог не появляется, пока настройки не изменятся.
- **Нужен полный рестарт клиента**: OTel-конфигурация, в отличие от большинства настроек, не применяется
  на лету.
- **Managed-источники не мержатся**: если server-managed отдаёт хоть один ключ, файловые/MDM managed settings
  игнорируются целиком. Исключение — блок `env`, он мержится по ключам (Claude Code v2.1.223+).
- **Не доедет** до тех, кто использует Bedrock / Vertex / Foundry или свой `ANTHROPIC_BASE_URL`: у них fetch
  настроек пропускается, и погасить это server-managed блоком нельзя.
- **Проверка**: `/status` у разработчика показывает активный managed-источник; `claude --debug-file <path>`
  и поиск `Remote settings` в логе — для разбора проблем доставки.

## Альтернатива, если полный поток событий не устраивает

Хуки шлют на бэкенд **только** факты вызова скиллов и ничего больше, без `OTEL_LOG_TOOL_DETAILS`.
Но здесь два пути вызова расходятся, и нужны **два хука** — это прямо задокументировано: хук `PreToolUse`
с матчером `Skill` срабатывает только когда скилл вызывает модель, а набранный руками `/имя-скилла`
проходит мимо `PreToolUse`; прямой путь ловит `UserPromptExpansion`.

- `PreToolUse`, `matcher: "Skill"` → вызовы моделью (аналог `claude-proactive`)
- `UserPromptExpansion` → ручные слэш-команды. На вход даёт `command_name`, `command_args`,
  `command_source`, `expansion_type` (`slash_command` для скиллов и кастомных команд, `mcp_prompt` для
  промптов MCP-серверов)

Второй минус: во входном JSON хука нет `user.email` — есть только `session_id`, `cwd`, `permission_mode`, —
личность придётся прокидывать самим (например, заголовком из `allowedEnvVars`). Хуки раскатываются тем же
admin-механизмом и тоже требуют approval-диалога.

Резервный сигнал для ручного пути (если он вдруг понадобится для сверки): событие `claude_code.user_prompt`
несёт `command_name` и `command_source`, но имена кастомных и плагинных команд там схлопываются в `custom`
без того же `OTEL_LOG_TOOL_DETAILS=1`.

## Приёмник

`main.py` — FastAPI-приложение, принимает OTLP/JSON на `POST /v1/logs` и печатает поток в stdout.
Хранилища пока нет намеренно: сначала убеждаемся, что данные доходят, потом подставляем запись в БД
вместо `handle_record`.

```
INFO  SKILL  2026-08-12 15:54:53  vasya@01.tech  skill=01-dev-pipeline:cr  trigger=user-slash  source=plugin
INFO  event  2026-08-12 15:58:13  vasya@01.tech  api_request
```

Что уже учтено и проверено на синтетических payload'ах: gzip-тела (`Content-Encoding: gzip`), `intValue`
строкой (в OTLP/JSON int64 всегда строка), вложенные `arrayValue` / `kvlistValue`, ответ в форме
`{"partialSuccess": {}}`, который ждёт экспортёр. Ошибка обработки одной записи не роняет батч — иначе
экспортёр начнёт слать его повторно. `/v1/metrics` и `/v1/traces` отвечают 200 и игнорируются: сейчас они
выключены на клиентах, но 404 в этом месте только путал бы.

Если в логах пошли строки `skill=custom_skill` — на клиентах не доехал `OTEL_LOG_TOOL_DETAILS=1`,
приёмник специально помечает это в строке.

| Переменная | Смысл |
| --- | --- |
| `CC_METRICS_TOKEN` | ожидаемый Bearer-токен; пусто — проверка выключена |
| `CC_METRICS_LOG_ALL_EVENTS` | `0` — печатать только `skill_activated` |
| `PORT` | порт при локальном запуске (в контейнере порт задаёт `CMD`) |
| `TZ` | пояс для времени событий; без неё в контейнере UTC |

Локально и в контейнере:

```bash
uv run main.py                                    # http://127.0.0.1:4318

docker build -t cc-metrics .
docker run -d --name cc-metrics -p 4318:4318 \
  -e TZ=Europe/Moscow -e CC_METRICS_TOKEN=<токен> cc-metrics
```

Порт 4318 — стандартный для OTLP поверх HTTP. В `OTEL_EXPORTER_OTLP_ENDPOINT` указывается **база без**
`/v1/logs`: путь экспортёр добавляет сам. Наружу нужен HTTPS-терминатор перед контейнером.

## Окружение и команды

Окружение управляется **uv** (`.venv` создан uv 0.12.3, CPython 3.14). Не используй `pip install` напрямую
и не активируй venv вручную — работай через `uv`, он сам подхватывает `.venv`.

```bash
uv run main.py           # запуск
uv add <package>         # добавить зависимость (правит pyproject.toml + создаёт uv.lock)
uv sync                  # привести .venv в соответствие с pyproject.toml
```

`requires-python = ">=3.14"` — можно свободно использовать синтаксис и stdlib Python 3.14.

Тестового раннера, линтера и форматтера в проекте нет. Прежде чем запускать `pytest`, `ruff` или `mypy`,
их нужно добавить (`uv add --dev ...`) и зафиксировать конфигурацию в `pyproject.toml`.

## Деплой

Coolify, сборка из `Dockerfile` в репозитории (`git@github.com:trase8/cc-metrics.git`, ветка `main`).
Сборка идёт на Linux-хосте; базовые образы мультиархитектурные, отдельной настройки под amd64 не требуется.
`uv.lock` обязан быть в репозитории — `Dockerfile` ставит зависимости с `--frozen`.

Ориентиры для Coolify: порт контейнера **4318**, healthcheck — `GET /health`, HTTPS терминирует Traefik
самого Coolify, `CC_METRICS_TOKEN` задаётся в UI как переменная окружения.

`TZ` намеренно не задаётся: контейнер пишет время событий в UTC, и это устраивает. Если когда-нибудь
понадобится местное время — `tzdata` в образе уже есть, достаточно передать `TZ`.

## Git

Репозиторий: `git@github.com:trase8/cc-metrics.git`, основная ветка `main`. `.venv/`, `.idea/` и `.env*`
закрыты `.gitignore`. Секретов в файлах нет и быть не должно — токен приходит только через переменную
окружения.
