"""Юнит-тесты для config.py: нормализация DSN, авторизация, lifespan без базы."""

import asyncio
import re

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

import config
from config import lifespan, normalize_dsn, require_token, require_ui_auth


# --- normalize_dsn -------------------------------------------------------------

def test_normalize_dsn_strips_sqlalchemy_asyncpg_prefix():
    assert normalize_dsn("postgresql+asyncpg://u:p@host/db") == "postgresql://u:p@host/db"


def test_normalize_dsn_strips_postgres_asyncpg_prefix():
    assert normalize_dsn("postgres+asyncpg://u:p@host/db") == "postgresql://u:p@host/db"


def test_normalize_dsn_leaves_plain_dsn_untouched():
    assert normalize_dsn("postgresql://u:p@host/db") == "postgresql://u:p@host/db"


# --- require_token -------------------------------------------------------------

class _FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.client = None


def test_require_token_disabled_when_empty(monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN", "")
    require_token(_FakeRequest())  # не должно бросить исключение вообще без заголовка


def test_require_token_accepts_correct_bearer(monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN", "secret123")
    require_token(_FakeRequest({"authorization": "Bearer secret123"}))


def test_require_token_rejects_missing_header(monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN", "secret123")
    with pytest.raises(HTTPException) as exc:
        require_token(_FakeRequest())
    assert exc.value.status_code == 401


def test_require_token_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN", "secret123")
    with pytest.raises(HTTPException):
        require_token(_FakeRequest({"authorization": "Bearer wrong"}))


def test_require_token_non_ascii_header_rejected_not_crashed(monkeypatch):
    # Регрессия: secrets.compare_digest на str с не-ASCII бросал TypeError раньше исправления.
    monkeypatch.setattr(config, "AUTH_TOKEN", "secret123")
    with pytest.raises(HTTPException) as exc:
        require_token(_FakeRequest({"authorization": "Bearer привет"}))
    assert exc.value.status_code == 401


# --- require_ui_auth -----------------------------------------------------------

def test_require_ui_auth_disabled_when_either_var_missing(monkeypatch):
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASSWORD", "")
    require_ui_auth(credentials=None)  # пароль пуст — проверка выключена, даже если логин задан


def test_require_ui_auth_accepts_correct_credentials(monkeypatch):
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASSWORD", "hunter2")
    require_ui_auth(credentials=HTTPBasicCredentials(username="admin", password="hunter2"))


def test_require_ui_auth_rejects_missing_credentials(monkeypatch):
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASSWORD", "hunter2")
    with pytest.raises(HTTPException) as exc:
        require_ui_auth(credentials=None)
    assert exc.value.status_code == 401


def test_require_ui_auth_rejects_wrong_password(monkeypatch):
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASSWORD", "hunter2")
    with pytest.raises(HTTPException):
        require_ui_auth(credentials=HTTPBasicCredentials(username="admin", password="wrong"))


def test_require_ui_auth_non_ascii_password_rejected_not_crashed(monkeypatch):
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASSWORD", "hunter2")
    with pytest.raises(HTTPException):
        require_ui_auth(credentials=HTTPBasicCredentials(username="admin", password="привет"))


# --- lifespan без базы ----------------------------------------------------------

def test_lifespan_without_database_url_leaves_pool_none(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "pool", None)

    async def run():
        async with lifespan(None):
            assert config.pool is None

    asyncio.run(run())


# --- согласованность DDL и INSERT_SQL -------------------------------------------

def test_insert_sql_placeholder_count_matches_column_count():
    columns = re.search(r"INSERT INTO skill_usage \(([^)]+)\)", config.INSERT_SQL, re.S).group(1)
    placeholders = re.search(r"VALUES \(([^)]+)\)", config.INSERT_SQL, re.S).group(1)
    assert len(columns.split(",")) == len(placeholders.split(","))
