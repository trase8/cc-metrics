"""Приёмник OTLP-логов Claude Code.

Принимает поток событий на /v1/logs, печатает в stdout и (если задан DATABASE_URL)
сохраняет вызовы скиллов в Postgres. Просмотр — на /ui.

Разбит на модули: config.py (env, авторизация, БД), api.py (/v1/*), ui.py (/ui).
"""

from __future__ import annotations

import os

from fastapi import FastAPI

import api
import config
import ui

app = FastAPI(title="cc-metrics", lifespan=config.lifespan)
app.include_router(api.router)
app.include_router(ui.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "4318")),
        access_log=config.ACCESS_LOG,
    )
