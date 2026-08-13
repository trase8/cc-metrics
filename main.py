"""Приёмник OTLP-логов Claude Code.

Принимает поток событий на /v1/logs, печатает в stdout и (если задан DATABASE_URL)
сохраняет вызовы скиллов в Postgres. Просмотр — на /ui.

Разбит на модули: config.py (env, авторизация, БД), api.py (/v1/*), ui.py (/ui).
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

import api
import config
import ui

app = FastAPI(title="cc-metrics", lifespan=config.lifespan)
app.include_router(api.router)
app.include_router(ui.router)


@app.get("/health")
async def health() -> JSONResponse:
    """Состояние приёмника вместе с состоянием базы прямо сейчас, а не на момент старта.

    503 при недоступной базе — намеренно: в этом состоянии приёмник и так отвечает 503 на
    /v1/logs, и снаружи он должен выглядеть сломанным, а не здоровым. Событий это не теряет,
    экспортёр повторит батч. Режим "только лог" (DATABASE_URL не задан) остаётся здоровым:
    это рабочая конфигурация, а не поломка.
    """
    if config.pool is None:
        return JSONResponse({"status": "ok", "database": "off"})
    if not await config.check_database():
        return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "database": "ok"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "4318")),
        access_log=config.ACCESS_LOG,
    )
