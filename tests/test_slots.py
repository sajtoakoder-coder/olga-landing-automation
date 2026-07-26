"""Тесты слотов расписания (bot/slots.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot import slots
from core.errors import ConflictError, NotFoundError, ValidationError


def future(hours: int = 24) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


# ---------- парсинг дат ----------


def test_parse_iso_and_ru_formats():
    iso = slots.parse_start("2026-08-01 15:00")
    ru = slots.parse_start("01.08.2026 15:00")
    assert iso == ru
    assert iso.tzinfo is not None


def test_parse_start_moscow_offset(monkeypatch):
    monkeypatch.setenv("APP_TZ", "Europe/Moscow")
    parsed = slots.parse_start("2026-08-01 15:00")
    assert parsed.utcoffset() == timedelta(hours=3)


@pytest.mark.parametrize("bad", ["", "abc", "2026-13-01 10:00", "32.01.2026 10:00", "2026-08-01"])
def test_parse_start_invalid(bad):
    with pytest.raises(ValidationError):
        slots.parse_start(bad)


# ---------- добавление / список / удаление ----------


def test_add_and_list_slots(store):
    slot = slots.add_slot(store, "2026-08-01 15:00", telemost_url="https://telemost.yandex.ru/j/1")
    assert slot["status"] == "free"
    assert slot["duration_min"] == 60
    listed = slots.list_slots(store)
    assert [s["id"] for s in listed] == [slot["id"]]


def test_add_slot_duplicate_time(store):
    slots.add_slot(store, "2026-08-01 15:00")
    with pytest.raises(ConflictError):
        slots.add_slot(store, "01.08.2026 15:00")


def test_add_slot_bad_telemost_url(store):
    with pytest.raises(ValidationError):
        slots.add_slot(store, "2026-08-01 15:00", telemost_url="telemost.yandex.ru")


def test_add_slot_bad_duration(store):
    with pytest.raises(ValidationError):
        slots.add_slot(store, "2026-08-01 15:00", duration_min=0)


def test_add_slots_bulk_mixed(store):
    added, errors = slots.add_slots_bulk(
        store,
        """
        2026-08-01 15:00
        01.08.2026 15:00
        мусор
        02.08.2026 12:30
        """,
    )
    assert len(added) == 2
    assert len(errors) == 2  # дубль и мусор


def test_list_sorted_by_start(store):
    slots.add_slot(store, "2026-08-02 10:00")
    slots.add_slot(store, "2026-08-01 10:00")
    listed = slots.list_slots(store)
    assert listed[0]["start"] < listed[1]["start"]


def test_delete_slot(store):
    slot = slots.add_slot(store, "2026-08-01 15:00")
    assert slots.delete_slot(store, slot["id"]) is True
    assert slots.delete_slot(store, slot["id"]) is False


# ---------- бронирование ----------


def test_book_slot_removes_from_available(store):
    slot = slots.add_slot(store, future(48))
    assert [s["id"] for s in slots.available_slots(store)] == [slot["id"]]

    booked = slots.book_slot(
        store,
        slot["id"],
        customer={"name": "Анна", "contact": "@anna", "email": "a@b.c"},
        payment_id="pay123",
    )
    assert booked["status"] == "booked"
    assert booked["booked_by"]["name"] == "Анна"
    assert booked["payment_id"] == "pay123"

    assert slots.available_slots(store) == []
    # но слот остаётся в полном списке
    assert len(slots.list_slots(store)) == 1


def test_book_slot_twice_conflict(store):
    slot = slots.add_slot(store, future(48))
    slots.book_slot(store, slot["id"], customer={}, payment_id="p1")
    with pytest.raises(ConflictError):
        slots.book_slot(store, slot["id"], customer={}, payment_id="p2")


def test_book_missing_slot(store):
    with pytest.raises(NotFoundError):
        slots.book_slot(store, "nope", customer={}, payment_id="p1")


def test_available_excludes_past(store):
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    slots.add_slot(store, past.astimezone().strftime("%Y-%m-%d %H:%M"))
    assert slots.available_slots(store) == []


# ---------- телемост и форматирование ----------


def test_telemost_url_priority(store, monkeypatch):
    monkeypatch.setenv("DEFAULT_TELEMOST_URL", "https://env.example/t")
    slot = slots.add_slot(store, "2026-08-01 15:00")
    # без своей ссылки и настройки — env
    assert slots.telemost_url_for(store, slot) == "https://env.example/t"
    # настройка бота приоритетнее env
    store.set_setting("telemost_url", "https://settings.example/t")
    assert slots.telemost_url_for(store, slot) == "https://settings.example/t"
    # своя ссылка слота — приоритетнее всего
    slot2 = slots.add_slot(store, "2026-08-02 15:00", telemost_url="https://own.example/t")
    assert slots.telemost_url_for(store, slot2) == "https://own.example/t"


def test_slot_end(store):
    slot = slots.add_slot(store, "2026-08-01 15:00", duration_min=90)
    assert slots.slot_end(slot) - slots.slot_start(slot) == timedelta(minutes=90)


def test_format_slot(store):
    slot = slots.add_slot(store, "2026-08-01 15:00")
    assert slots.format_slot(slot) == "01.08.2026 15:00 (60 мин)"
