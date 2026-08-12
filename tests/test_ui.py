"""Юнит-тесты для чистых хелперов страницы /ui (ui.py) — без базы и без рендеринга."""

import json
from datetime import date, datetime, timezone

from ui import day_range, embed_json, parse_day_param


# --- day_range ---------------------------------------------------------------

def test_day_range_single_day():
    d = datetime(2026, 8, 12, 15, 30, tzinfo=timezone.utc)
    result = day_range(d, d)
    assert result == [datetime(2026, 8, 12, tzinfo=timezone.utc)]


def test_day_range_normalizes_time_of_day_to_midnight():
    start = datetime(2026, 8, 1, 23, 59, tzinfo=timezone.utc)
    end = datetime(2026, 8, 3, 0, 1, tzinfo=timezone.utc)
    result = day_range(start, end)
    assert result == [
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 2, tzinfo=timezone.utc),
        datetime(2026, 8, 3, tzinfo=timezone.utc),
    ]


def test_day_range_start_after_end_is_empty():
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert day_range(start, end) == []


# --- parse_day_param -----------------------------------------------------------

def test_parse_day_param_empty_string_is_none():
    assert parse_day_param("", end_of_day=False) is None


def test_parse_day_param_invalid_string_is_none():
    assert parse_day_param("not-a-date", end_of_day=False) is None


def test_parse_day_param_start_of_day():
    result = parse_day_param("2026-08-12", end_of_day=False)
    assert result == datetime(2026, 8, 12, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_day_param_end_of_day():
    result = parse_day_param("2026-08-12", end_of_day=True)
    assert result == datetime(2026, 8, 12, 23, 59, 59, 999999, tzinfo=timezone.utc)


# --- embed_json ------------------------------------------------------------------

def test_embed_json_round_trips_basic_data():
    payload = {"labels": ["2026-08-12"], "datasets": [{"label": "cr", "data": [1, 2]}]}
    assert json.loads(embed_json(payload)) == payload


def test_embed_json_escapes_script_close_tag():
    # Имя скилла приходит по сети — если в нём окажется "</script>", тег не должен обрываться раньше времени.
    result = embed_json({"skill.name": "</script><script>alert(1)</script>"})
    assert "</" not in result
    assert "<\\/script>" in result


def test_embed_json_keeps_non_ascii_readable():
    assert "скилл" in embed_json({"name": "скилл"})


def test_embed_json_stringifies_non_serializable_values():
    result = embed_json({"when": date(2026, 1, 1)})
    assert "2026-01-01" in result
