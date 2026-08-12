"""Юнит-тесты для разбора OTLP и обработки записей (api.py) — без сети и без базы."""

import asyncio
import gzip
from datetime import datetime, timedelta, timezone

from api import (
    decode_any_value,
    decode_attributes,
    describe_skill,
    gunzip_capped,
    handle_record,
    parse_occurred_at,
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


# --- parse_occurred_at -------------------------------------------------------

def test_parse_occurred_at_uses_time_unix_nano():
    record = {"timeUnixNano": "1700000000000000000"}
    assert parse_occurred_at(record, {}) == datetime.fromtimestamp(1700000000, tz=timezone.utc)


def test_parse_occurred_at_falls_back_to_observed_when_time_unix_nano_broken():
    record = {"timeUnixNano": "not-a-number", "observedTimeUnixNano": "1700000000000000000"}
    assert parse_occurred_at(record, {}) == datetime.fromtimestamp(1700000000, tz=timezone.utc)


def test_parse_occurred_at_assumes_utc_for_naive_iso_attr():
    attrs = {"event.timestamp": "2026-01-01T12:00:00"}
    assert parse_occurred_at({}, attrs) == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_occurred_at_preserves_explicit_timezone_from_iso_attr():
    attrs = {"event.timestamp": "2026-01-01T12:00:00+03:00"}
    result = parse_occurred_at({}, attrs)
    assert result.utcoffset() == timedelta(hours=3)
    assert result.hour == 12


def test_parse_occurred_at_falls_back_to_now_when_nothing_usable():
    before = datetime.now(timezone.utc)
    result = parse_occurred_at({}, {})
    after = datetime.now(timezone.utc)
    assert before <= result <= after


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
    assert row[2] == "?"


def test_handle_record_record_attrs_win_over_resource_attrs_on_conflict():
    resource_attrs = {"department": "rnd", "user.email": "resource@example.com"}
    record = {
        "timeUnixNano": "1700000000000000000",
        "attributes": [
            {"key": "event.name", "value": {"stringValue": "skill_activated"}},
            {"key": "user.email", "value": {"stringValue": "record@example.com"}},
            {"key": "skill.name", "value": {"stringValue": "01-dev-pipeline:cr"}},
        ],
    }
    row = handle_record(record, resource_attrs)
    assert row[1] == "record@example.com"  # запись переопределяет ресурс при совпадении ключа
    assert row[10] == "rnd"  # ключ, которого не было в записи, приходит из resource_attrs


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
    assert row[9] is None


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
