"""Юнит-тесты для config.py: нормализация DSN, авторизация, lifespan без базы."""

import asyncio
import logging
import re
import secrets

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

import config
from config import check_database, lifespan, normalize_dsn, require_token, require_ui_auth


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
    require_ui_auth(_FakeRequest(), credentials=None)  # пароль пуст — проверка выключена, даже если логин задан


def test_require_ui_auth_accepts_correct_credentials(monkeypatch):
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASSWORD", "hunter2")
    require_ui_auth(_FakeRequest(), credentials=HTTPBasicCredentials(username="admin", password="hunter2"))


def test_require_ui_auth_rejects_missing_credentials(monkeypatch):
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASSWORD", "hunter2")
    with pytest.raises(HTTPException) as exc:
        require_ui_auth(_FakeRequest(), credentials=None)
    assert exc.value.status_code == 401


def test_require_ui_auth_rejects_wrong_password(monkeypatch):
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASSWORD", "hunter2")
    with pytest.raises(HTTPException):
        require_ui_auth(_FakeRequest(), credentials=HTTPBasicCredentials(username="admin", password="wrong"))


def test_require_ui_auth_non_ascii_password_rejected_not_crashed(monkeypatch):
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASSWORD", "hunter2")
    with pytest.raises(HTTPException):
        require_ui_auth(_FakeRequest(), credentials=HTTPBasicCredentials(username="admin", password="привет"))


def test_require_ui_auth_logs_rejected_attempt(monkeypatch, caplog):
    # Без этой строки перебор пароля к публично доступной странице не оставляет никаких следов:
    # отказ по токену логируется, а отказ по Basic раньше — нет.
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASSWORD", "hunter2")
    with caplog.at_level(logging.WARNING, logger="cc-metrics"), pytest.raises(HTTPException):
        require_ui_auth(_FakeRequest(), credentials=None)
    assert "отклонён вход в /ui" in caplog.text


def test_require_ui_auth_compares_password_even_when_login_is_wrong(monkeypatch):
    # Короткое замыкание `and` пропускало бы сравнение пароля при неверном логине,
    # и по времени ответа было бы видно, угадан логин или нет.
    monkeypatch.setattr(config, "UI_USER", "admin")
    monkeypatch.setattr(config, "UI_PASSWORD", "hunter2")
    calls = []
    real = secrets.compare_digest
    monkeypatch.setattr(secrets, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))

    with pytest.raises(HTTPException):
        require_ui_auth(_FakeRequest(), credentials=HTTPBasicCredentials(username="кто-то", password="x"))
    assert len(calls) == 2  # логин и пароль, а не только логин


# --- lifespan без базы ----------------------------------------------------------

def test_lifespan_without_database_url_leaves_pool_none(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "pool", None)

    async def run():
        async with lifespan(None):
            assert config.pool is None

    asyncio.run(run())


# --- check_database (с подставным пулом вместо Postgres) -------------------------

class _FakePool:
    """Пул, который либо отдаёт соединение, либо падает — как настоящий при мёртвой базе."""

    def __init__(self, failure: Exception | None = None):
        self.failure = failure
        self.timeouts = []

    def acquire(self, timeout=None):
        self.timeouts.append(timeout)
        return self

    async def __aenter__(self):
        if self.failure is not None:
            raise self.failure
        return self

    async def __aexit__(self, *_):
        return False

    async def fetchval(self, sql, timeout=None):
        self.timeouts.append(timeout)
        return 1


def test_check_database_false_without_pool(monkeypatch):
    monkeypatch.setattr(config, "pool", None)
    assert asyncio.run(check_database()) is False


def test_check_database_true_when_query_succeeds(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(config, "pool", pool)
    assert asyncio.run(check_database()) is True
    # Оба ожидания ограничены: зависшая база не должна вешать ещё и healthcheck.
    assert pool.timeouts == [config.HEALTH_TIMEOUT, config.HEALTH_TIMEOUT]


def test_check_database_false_when_pool_raises(monkeypatch):
    monkeypatch.setattr(config, "pool", _FakePool(failure=OSError("база не отвечает")))
    assert asyncio.run(check_database()) is False


# --- согласованность DDL и INSERT_SQL -------------------------------------------

def test_insert_sql_lists_columns_in_field_order():
    columns = re.search(r"INSERT INTO skill_usage \(([^)]+)\)", config.INSERT_SQL, re.S).group(1)
    assert [c.strip() for c in columns.split(",")] == list(config.SkillUsageRow._fields)


def test_insert_sql_placeholders_are_numbered_from_one_in_order():
    # Генератор плейсхолдеров легко испортить off-by-one'ом: $0 или пропуск последнего поля
    # уронили бы вставку только на реальной базе.
    placeholders = re.search(r"VALUES \(([^)]+)\)", config.INSERT_SQL, re.S).group(1)
    expected = [f"${i}" for i in range(1, len(config.SkillUsageRow._fields) + 1)]
    assert [p.strip() for p in placeholders.split(",")] == expected


def test_every_row_field_is_declared_in_ddl():
    declared = set(re.findall(r"^ {4}(\w+)\s", config.DDL, re.M))
    assert set(config.SkillUsageRow._fields) <= declared
