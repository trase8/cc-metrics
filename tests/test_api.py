"""Юнит-тесты для разбора OTLP и обработки записей (api.py) — без сети и без базы."""

import asyncio
import gzip
import json
import logging
import re

import api
import config

from api import (
    MAX_TEXT_LEN,
    clean_text,
    decode_any_value,
    decode_attributes,
    describe_broken_record,
    describe_skill,
    drop_identity,
    gunzip_capped,
    handle_record,
    read_body_capped,
)


class _FakeBatchRequest:
    """Запрос с готовым телом — чтобы дёргать receive_logs() без сети."""

    def __init__(self, body: bytes, headers=None):
        self._body = body
        self.headers = headers or {}
        self.client = None

    async def stream(self):
        yield self._body


def _post(body: bytes):
    return asyncio.run(api.receive_logs(_FakeBatchRequest(body)))


def _batch(*records) -> bytes:
    return json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": list(records)}]}]}).encode()


def _skill_record(**attrs) -> dict:
    attrs = {"event.name": "skill_activated", **attrs}
    return {"attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]}


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


def test_decode_any_value_non_dict_returns_none():
    # `"stringValue" in 5` — это TypeError, а прийти по проводу может что угодно.
    assert decode_any_value(5) is None
    assert decode_any_value("строка") is None
    assert decode_any_value(None) is None


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


def test_decode_attributes_tolerates_object_instead_of_list():
    # Так выглядели атрибуты, на которых разбор падал: объект вместо массива.
    assert decode_attributes({"user.email": "vasya@01.tech"}) == {}


def test_decode_attributes_skips_non_dict_items():
    assert decode_attributes(["мусор", {"key": "ok", "value": {"stringValue": "y"}}]) == {"ok": "y"}


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


def test_handle_record_maps_every_attribute_to_its_own_column():
    # Главная защита от молчаливой порчи: у каждого атрибута свой уникальный маркер, поэтому
    # перепутанные местами колонки видно сразу. Восемь из девяти колонок — nullable text,
    # так что в базе такая перестановка не вызвала бы никакой ошибки.
    record = {
        "attributes": [
            {"key": "event.name", "value": {"stringValue": "skill_activated"}},
            {"key": "skill.name", "value": {"stringValue": "маркер-skill"}},
            {"key": "invocation_trigger", "value": {"stringValue": "маркер-trigger"}},
            {"key": "skill.source", "value": {"stringValue": "маркер-source"}},
            {"key": "plugin.name", "value": {"stringValue": "маркер-plugin"}},
            {"key": "marketplace.name", "value": {"stringValue": "маркер-marketplace"}},
            {"key": "skill.kind", "value": {"stringValue": "маркер-kind"}},
            {"key": "session.id", "value": {"stringValue": "маркер-session"}},
            {"key": "event.sequence", "value": {"intValue": "42"}},
            {"key": "terminal.type", "value": {"stringValue": "маркер-terminal"}},
        ],
    }
    assert handle_record(record, {}) == config.SkillUsageRow(
        skill="маркер-skill",
        trigger="маркер-trigger",
        source="маркер-source",
        plugin="маркер-plugin",
        marketplace="маркер-marketplace",
        skill_kind="маркер-kind",
        session_id="маркер-session",
        event_sequence=42,
        terminal_type="маркер-terminal",
    )


def test_handle_record_missing_skill_name_falls_back_to_placeholder():
    record = {"attributes": [{"key": "event.name", "value": {"stringValue": "skill_activated"}}]}
    assert handle_record(record, {}).skill == "?"


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
    assert row.terminal_type == "record-terminal"  # запись переопределяет ресурс при совпадении ключа
    assert row.session_id == "sess-1"  # ключ, которого не было в записи, приходит из resource_attrs


def test_handle_record_ignores_non_int_sequence():
    # event.sequence пришёл как stringValue, а не intValue — реалистичный вариант испорченных данных.
    record = {
        "attributes": [
            {"key": "event.name", "value": {"stringValue": "skill_activated"}},
            {"key": "skill.name", "value": {"stringValue": "x"}},
            {"key": "event.sequence", "value": {"stringValue": "not-an-int"}},
        ],
    }
    assert handle_record(record, {}).event_sequence is None


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
    assert row.skill == "01-dev-pipeline:cr"  # неперсональные поля на месте


def test_handle_record_returns_named_row_not_bare_tuple():
    # Именно тип связывает api.py с INSERT_SQL: голый кортеж снова сделал бы порядок
    # колонок неявным договором между двумя файлами.
    record = {"attributes": [{"key": "event.name", "value": {"stringValue": "skill_activated"}}]}
    assert isinstance(handle_record(record, {}), config.SkillUsageRow)


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


# --- clean_text ------------------------------------------------------------------

def test_clean_text_removes_control_characters():
    # Перевод строки внутри skill.name давал в логе строку, неотличимую от настоящей записи.
    assert clean_text("cr\n2026-08-13 00:00:00 INFO  SKILL  skill=ПОДДЕЛКА") == (
        "cr 2026-08-13 00:00:00 INFO  SKILL  skill=ПОДДЕЛКА"
    )


def test_clean_text_caps_length():
    assert len(clean_text("A" * 100_000)) == MAX_TEXT_LEN


def test_clean_text_keeps_none_as_none():
    # В базе это отсутствующий атрибут, а не строка "None".
    assert clean_text(None) is None


def test_describe_skill_cleans_control_characters():
    assert "\n" not in describe_skill({"skill.name": "cr\nПОДДЕЛКА"})


def test_handle_record_cleans_text_before_writing_the_row():
    row = handle_record(_skill_record(**{"skill.name": "cr\nПОДДЕЛКА" + "A" * 100_000}), {})
    assert "\n" not in row.skill
    assert len(row.skill) == MAX_TEXT_LEN


# --- describe_broken_record ------------------------------------------------------

def test_describe_broken_record_prints_shape_without_values():
    # Значений не печатаем вообще: в сырой записи лежит личность, а drop_identity()
    # к моменту ошибки ещё не отработал.
    result = describe_broken_record({"attributes": {"user.email": "vasya@01.tech"}, "timeUnixNano": "1"})
    assert "vasya@01.tech" not in result
    assert '"attributes": "dict"' in result  # форма записи для отладки осталась


def test_describe_broken_record_handles_non_dict():
    assert describe_broken_record("строка") == "<str>"


# --- receive_logs: личность и кривые тела ----------------------------------------

def test_broken_record_does_not_leak_identity_into_the_log(monkeypatch, caplog):
    # Ветка обработки ошибки печатала сырую запись — то есть до drop_identity(). Любое
    # падение handle_record() на записи с user.email клало адрес в лог, а логи живут долго.
    def boom(record, resource_attrs):
        raise ValueError("что угодно")

    monkeypatch.setattr(api, "handle_record", boom)
    body = _batch({"attributes": [{"key": "user.email", "value": {"stringValue": "vasya@01.tech"}}]})

    with caplog.at_level(logging.ERROR, logger="cc-metrics"):
        response = _post(body)

    assert response.status_code == 200  # одна битая запись не роняет батч
    assert "vasya@01.tech" not in caplog.text
    assert "attributes" in caplog.text


def test_receive_logs_rejects_body_that_is_not_an_object():
    # Дальше везде .get(), а на списке или строке это AttributeError мимо try, то есть 500.
    for body in (b"[]", '"строка"'.encode(), b"5"):
        assert _post(body).status_code == 400


def test_receive_logs_rejects_deeply_nested_json():
    # На такой вложенности json.loads бросает RecursionError, а не JSONDecodeError:
    # без отдельной ветки в except это был бы 500. Тело — объект намеренно, иначе
    # сработала бы проверка "тело не объект" и ветка RecursionError осталась бы непройденной.
    body = b'{"resourceLogs":' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    assert _post(body).status_code == 400


def test_receive_logs_survives_garbage_inside_the_batch():
    body = json.dumps({
        "resourceLogs": [
            "мусор",
            {
                "resource": "не объект",
                "scopeLogs": ["мусор", {"logRecords": ["мусор", _skill_record(**{"skill.name": "cr"})]}],
            },
        ],
    }).encode()
    assert _post(body).status_code == 200


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
