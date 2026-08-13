"""Проверки /health: приёмник поднимается и честно докладывает о состоянии базы."""

from fastapi.testclient import TestClient

import config
from main import app


def test_health_reports_log_only_mode_as_healthy():
    # DATABASE_URL не задан — это рабочая конфигурация «только лог», а не поломка.
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "off"}


def test_health_reports_database_ok(monkeypatch):
    async def available():
        return True

    # Без `with` жизненный цикл приложения не запускается — пул подставляем руками.
    monkeypatch.setattr(config, "pool", object())
    monkeypatch.setattr(config, "check_database", available)
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_returns_503_when_database_is_unavailable(monkeypatch):
    # Приёмник с мёртвой базой отвечает 503 и на /v1/logs — снаружи он не должен
    # выглядеть здоровым, иначе поломку видно только по трейсбекам в логе.
    async def unavailable():
        return False

    monkeypatch.setattr(config, "pool", object())
    monkeypatch.setattr(config, "check_database", unavailable)
    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}
