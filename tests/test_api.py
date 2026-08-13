"""Юнит-тесты для разбора OTLP и обработки записей (api.py) — без сети и без базы."""

import asyncio
import gzip
import re

import config

from api import (
    decode_any_value,
    decode_attributes,
    describe_skill,
    drop_identity,
    gunzip_capped,
    handle_record,
    read_body_capped,
)


# --- decode_any_value -------------------------------------------------------

def test_decode_any_value_string():
    assert decode_any_value({"stringValue": "hi"}) == "hi"


def test_decode_any_value_int_value_is_string_in_otlp_json():
    # В OTLP/JSON int64 всегда приезжает строкой — должен вернуться int, а не str.
    assert decode_any_value({"intValue": "42"}) == 42


def test_decode_any_value_int_value_unparseable_returns_raw():
    assert decode_any_value({"intValue": "not-an-int"}) == "not-an-int"


def test_decode_any_value_double_and_bool():
    assert decode_any_value({"doubleValue": 1.5}) == 1.5
    assert decode_any_value({"boolValue": True}) is True


def test_decode_any_value_array_value_decodes_each_element():
    value = {"arrayValue": {"values": [{"stringValue": "a"}, {"intValue": "2"}]}}
    assert decode_any_value(value) == ["a", 2]


def test_decode_any_value_kvlist_value_decodes_to_dict():
    value = {"kvlistValue": {"values": [{"key": "a", "value": {"stringValue": "b"}}]}}
    assert decode_any_value(value) == {"a": "b"}


def test_decode_any_value_unknown_shape_returns_none():
    assert decode_any_value({}) is None


# --- decode_attributes -------------------------------------------------------

def test_decode_attributes_builds_flat_dict():
    attrs = [
        {"key": "a", "value": {"stringValue": "1"}},
        {"key": "b", "value": {"intValue": "2"}},
    ]
    assert decode_attributes(attrs) == {"a": "1", "b": 2}


def test_decode_attributes_skips_non_string_keys():
    attrs = [{"key": None, "value": {"stringValue": "x"}}, {"key": "ok", "value": {"stringValue": "y"}}]
    assert decode_attributes(attrs) == {"ok": "y"}


def test_decode_attributes_handles_none_input():
    assert decode_attributes(None) == {}


# --- describe_skill -----------------------------------------------------------

def test_describe_skill_includes_present_fields_only():
    attrs = {"skill.name": "01-dev-pipeline:cr", "invocation_trigger": "user-slash"}
    assert describe_skill(attrs) == "skill=01-dev-pipeline:cr  trigger=user-slash"


def test_describe_skill_flags_redacted_custom_skill():
    assert describe_skill({"skill.name": "custom_skill"}).startswith("skill=custom_skill (!!")


def test_describe_skill_missing_name_uses_placeholder():
    assert describe_skill({}).startswith("skill=?")


# --- handle_record -------------------------------------------------------------

def test_handle_record_ignores_non_skill_events():
    record = {"attributes": [{"key": "event.name", "value": {"stringValue": "api_request"}}]}
    assert handle_record(record, {}) is None


def test_handle_record_missing_skill_name_falls_back_to_placeholder():
    record = {"attributes": [{"key": "event.name", "value": {"stringValue": "skill_activated"}}]}
    row = handle_record(record, {})
    assert row[0] == "?"


def test_handle_record_record_attrs_win_over_resource_attrs_on_conflict():
    resource_attrs = {"terminal.type": "resource-terminal", "session.id": "sess-1"}
    record = {
        "attributes": [
            {"key": "event.name", "value": {"stringValue": "skill_activated"}},
            {"key": "terminal.type", "value": {"stringValue": "record-terminal"}},
            {"key": "skill.name", "value": {"stringValue": "01-dev-pipeline:cr"}},
        ],
    }
    row = handle_record(record, resource_attrs)
    assert row[8] == "record-terminal"  # запись переопределяет ресурс при совпадении ключа
    assert row[6] == "sess-1"  # ключ, которого не было в записи, приходит из resource_attrs


def test_handle_record_ignores_non_int_sequence():
    # event.sequence пришёл как stringValue, а не intValue — реалистичный вариант испорченных данных.
    record = {
        "attributes": [
            {"key": "event.name", "value": {"stringValue": "skill_activated"}},
            {"key": "skill.name", "value": {"stringValue": "x"}},
            {"key": "event.sequence", "value": {"stringValue": "not-an-int"}},
        ],
    }
    row = handle_record(record, {})
    assert row[7] is None


def test_handle_record_keeps_identity_out_of_the_row():
    # Личность приходит и в ресурсных атрибутах, и в самой записи — ни то, ни другое
    # не должно оказаться ни в одном поле строки: метрики анонимные.
    resource_attrs = {"user.account_uuid": "uuid-from-resource"}
    record = {
        "attributes": [
            {"key": "event.name", "value": {"stringValue": "skill_activated"}},
            {"key": "skill.name", "value": {"stringValue": "01-dev-pipeline:cr"}},
            {"key": "user.email", "value": {"stringValue": "vasya@01.tech"}},
        ],
    }
    row = handle_record(record, resource_attrs)
    assert "vasya@01.tech" not in str(row)
    assert "uuid-from-resource" not in str(row)
    assert row[0] == "01-dev-pipeline:cr"  # неперсональные поля на месте


def test_handle_record_row_length_matches_insert_sql():
    # Строка из api.py подставляется в INSERT_SQL из config.py — при правке колонок
    # эти два места разъезжаются молча, до первой записи в реальную базу.
    record = {"attributes": [{"key": "event.name", "value": {"stringValue": "skill_activated"}}]}
    placeholders = re.search(r"VALUES \(([^)]+)\)", config.INSERT_SQL, re.S).group(1)
    assert len(handle_record(record, {})) == len(placeholders.split(","))


# --- drop_identity ---------------------------------------------------------------

def test_drop_identity_removes_known_user_attributes():
    attrs = {
        "user.email": "vasya@01.tech",
        "user.account_uuid": "uuid",
        "user.id": "id",
        "skill.name": "cr",
    }
    assert drop_identity(attrs) == {"skill.name": "cr"}


def test_drop_identity_removes_unknown_user_attributes_too():
    # Отбор по префиксу, а не по списку ключей: новый user.*-атрибут в Claude Code
    # не должен просочиться в лог и в базу.
    assert drop_identity({"user.display_name": "Вася"}) == {}


def test_drop_identity_keeps_attributes_with_similar_names():
    attrs = {"userland": "x", "terminal.type": "iTerm.app", "organization.id": "org"}
    assert drop_identity(attrs) == attrs


# --- gunzip_capped -------------------------------------------------------------

def test_gunzip_capped_round_trips_within_limit():
    original = b"hello " * 100
    assert gunzip_capped(gzip.compress(original), limit=10_000) == original


def test_gunzip_capped_returns_none_when_over_limit():
    assert gunzip_capped(gzip.compress(b"x" * 1000), limit=100) is None


def test_gunzip_capped_returns_none_for_corrupted_middle():
    # Регрессия: zlib.error не подкласс OSError, битая середина архива раньше давала 500 вместо 413.
    # Данные не должны сжиматься в пару десятков байт, иначе порча может попасть за пределы архива.
    original = bytes((i * 37 + 11) % 256 for i in range(4000))
    broken = bytearray(gzip.compress(original))
    mid = len(broken) // 2
    for i in range(mid, mid + 40):
        broken[i] ^= 0xFF
    assert gunzip_capped(bytes(broken), limit=10_000) is None


def test_gunzip_capped_returns_none_for_non_gzip_data():
    assert gunzip_capped(b"not gzip at all", limit=10_000) is None


# --- read_body_capped (async, без реального Request) ---------------------------

class _FakeRequest:
    def __init__(self, chunks):
        self._chunks = chunks

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


def test_read_body_capped_concatenates_chunks_within_limit():
    result = asyncio.run(read_body_capped(_FakeRequest([b"hello ", b"world"]), limit=100))
    assert result == b"hello world"


def test_read_body_capped_stops_reading_once_over_limit():
    read_chunks = []

    class _TrackingRequest:
        async def stream(self):
            for chunk in (b"a" * 10, b"b" * 10, b"c" * 10):
                read_chunks.append(chunk)
                yield chunk

    result = asyncio.run(read_body_capped(_TrackingRequest(), limit=15))
    assert result is None
    assert read_chunks == [b"a" * 10, b"b" * 10]  # третий чанк не читается после превышения лимита
