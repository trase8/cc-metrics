"""Юнит-тесты для чистых хелперов страницы /ui (ui.py) — без базы и без рендеринга."""

import asyncio
import json
from datetime import date, datetime, timezone

from ui import (
    FEATURED_SLOTS,
    MAX_DAY,
    MIN_DAY,
    OTHER_LABEL,
    PALETTE_SLOTS,
    day_range,
    embed_json,
    load_chart_data,
    parse_day_param,
    slot_color,
)
from ui import ui as ui_handler


# --- day_range ---------------------------------------------------------------

def test_day_range_single_day():
    d = datetime(2026, 8, 12, 15, 30, tzinfo=timezone.utc)
    assert day_range(d, d) == [date(2026, 8, 12)]


def test_day_range_drops_time_of_day():
    # Границы окна приходят с временем (until — конец суток), на оси X остаются только дни.
    start = datetime(2026, 8, 1, 23, 59, tzinfo=timezone.utc)
    end = datetime(2026, 8, 3, 0, 1, tzinfo=timezone.utc)
    assert day_range(start, end) == [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)]


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


def test_parse_day_param_clamps_date_below_minimum():
    assert parse_day_param("0001-01-01", end_of_day=True) == MIN_DAY


def test_parse_day_param_clamps_date_above_maximum():
    assert parse_day_param("9999-12-31", end_of_day=False) == MAX_DAY


def test_ui_survives_minimal_date():
    # Регрессия: у 0001-01-01 минус окно по умолчанию нет представимого результата —
    # datetime бросал OverflowError, и получалось 500 на ровном месте.
    response = asyncio.run(ui_handler(skill="", since="", until="0001-01-01"))
    assert response.status_code == 503  # база не подключена, но страница отрисовалась


# --- embed_json ------------------------------------------------------------------

def test_embed_json_round_trips_basic_data():
    payload = {"labels": ["2026-08-12"], "datasets": [{"label": "cr", "data": [1, 2]}]}
    assert json.loads(embed_json(payload)) == payload


def test_embed_json_escapes_script_close_tag():
    # Имя скилла приходит по сети — если в нём окажется "</script>", тег не должен обрываться раньше времени.
    result = embed_json({"skill.name": "</script><script>alert(1)</script>"})
    assert "<" not in result
    assert json.loads(result) == {"skill.name": "</script><script>alert(1)</script>"}


def test_embed_json_escapes_html_comment_opener():
    # "<!--<script>" переводит разбор страницы в состояние, где настоящий </script>
    # её уже не закрывает: экранировать только "</" мало.
    result = embed_json({"skill.name": "<!--<script>"})
    assert "<" not in result
    assert json.loads(result) == {"skill.name": "<!--<script>"}


def test_embed_json_keeps_non_ascii_readable():
    assert "скилл" in embed_json({"name": "скилл"})


def test_embed_json_stringifies_non_serializable_values():
    result = embed_json({"when": date(2026, 1, 1)})
    assert "2026-01-01" in result


# --- slot_color -------------------------------------------------------------------

def test_slot_color_uses_palette_variables_for_first_slots():
    assert slot_color(0) == {"color_var": "--series-1"}
    assert slot_color(PALETTE_SLOTS - 1) == {"color_var": f"--series-{PALETTE_SLOTS}"}


def test_slot_color_generates_color_beyond_palette():
    # Регрессия: CSS-переменных всего PALETTE_SLOTS штук. Слот сверх палитры не должен
    # ссылаться на несуществующую --series-N — cssVar() вернул бы пустую строку, и линия
    # осталась бы без цвета.
    beyond = slot_color(PALETTE_SLOTS)
    assert "color_var" not in beyond
    assert beyond["color"].startswith("hsl(")


def test_slot_color_gives_every_featured_slot_a_color():
    colors = [slot_color(i) for i in range(FEATURED_SLOTS)]
    assert all(c.get("color") or c.get("color_var") for c in colors)


def test_slot_color_keeps_neighbouring_slots_apart():
    # Золотой угол: соседние по рейтингу линии не должны получать почти один оттенок.
    hues = [float(slot_color(i)["color"].split("(")[1].split()[0]) for i in range(PALETTE_SLOTS, FEATURED_SLOTS)]
    assert all(abs(a - b) > 20 for a, b in zip(hues, hues[1:]))


# --- load_chart_data (с подставным соединением вместо Postgres) --------------------

class _FakeConn:
    """Отдаёт заранее заданные строки и запоминает запросы: рейтинг узнаём по LIMIT."""

    def __init__(self, featured, rows):
        self._featured = [{"skill": s} for s in featured]
        self._rows = rows
        self.queries = []

    async def fetch(self, sql, *params):
        self.queries.append((sql, params))
        return self._featured if "LIMIT" in sql else self._rows


def _bucket(day: int) -> datetime:
    return datetime(2026, 8, day, tzinfo=timezone.utc)


def _load(conn, skill="", since_day=1, until_day=3):
    return asyncio.run(load_chart_data(conn, skill, _bucket(since_day), _bucket(until_day)))


def test_load_chart_data_spreads_counts_over_days():
    conn = _FakeConn(["cr"], [{"bucket": _bucket(1), "skill": "cr", "n": 2},
                              {"bucket": _bucket(3), "skill": "cr", "n": 5}])
    chart = _load(conn)
    assert chart["labels"] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert chart["datasets"] == [{"label": "cr", "color_var": "--series-1", "data": [2, 0, 5]}]


def test_load_chart_data_folds_non_featured_skills_into_other():
    featured = [f"skill-{i}" for i in range(FEATURED_SLOTS)]
    rows = [{"bucket": _bucket(1), "skill": "skill-0", "n": 1},
            {"bucket": _bucket(1), "skill": "редкий-а", "n": 2},
            {"bucket": _bucket(1), "skill": "редкий-б", "n": 3}]
    chart = _load(_FakeConn(featured, rows), until_day=1)
    labels = [ds["label"] for ds in chart["datasets"]]
    assert labels == ["skill-0", OTHER_LABEL]  # "Другое" всегда последним
    other = next(ds for ds in chart["datasets"] if ds["label"] == OTHER_LABEL)
    assert other["data"] == [5]  # 2 + 3 — оба скилла вне топа сложились в одну линию
    assert other["color_var"] == "--series-other"


def test_load_chart_data_ignores_rows_outside_the_day_window():
    # Строка вне окна не должна ронять страницу (согласованный WHERE такого не даёт, но данные внешние).
    conn = _FakeConn(["cr"], [{"bucket": _bucket(9), "skill": "cr", "n": 4}])
    assert _load(conn)["datasets"] == []


def test_load_chart_data_filters_by_skill_with_third_placeholder():
    conn = _FakeConn(["cr"], [])
    _load(conn, skill="cr")
    sql, params = conn.queries[-1]
    assert "skill ILIKE $3" in sql  # $1/$2 заняты границами окна
    assert params == (_bucket(1), _bucket(3), "%cr%")


def test_load_chart_data_without_skill_passes_only_window_bounds():
    conn = _FakeConn(["cr"], [])
    _load(conn)
    sql, params = conn.queries[-1]
    assert "ILIKE" not in sql
    assert params == (_bucket(1), _bucket(3))
