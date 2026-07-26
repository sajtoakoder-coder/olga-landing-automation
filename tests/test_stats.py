"""Тесты статистики (bot/stats.py)."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.stats import collect_stats, format_stats

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 7, 26, 15, 0, tzinfo=TZ)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def add_booking(store, created: datetime, name: str = "Анна") -> None:
    store.put("bookings", {"name": name, "contact": "@a", "created_at": iso(created)})


def add_payment(
    store,
    paid: datetime | None,
    amount: int = 5000,
    title: str = "Консультация",
    status: str = "paid",
) -> None:
    store.put(
        "payments",
        {
            "product_title": title,
            "amount": amount,
            "status": status,
            "paid_at": iso(paid) if paid else None,
            "created_at": iso(paid or NOW),
        },
    )


def test_empty_store_returns_zeros(store):
    stats = collect_stats(store, "all", now=NOW)
    assert stats == {
        "period": "all",
        "bookings": 0,
        "payments": 0,
        "revenue": 0,
        "conversion_pct": 0,
        "top_products": [],
    }
    # и текст не падает
    assert "Статистика" in format_stats(stats)


def test_counts_by_period(store, monkeypatch):
    monkeypatch.setenv("APP_TZ", "Europe/Moscow")
    add_booking(store, NOW - timedelta(hours=1))      # сегодня
    add_booking(store, NOW - timedelta(days=3))       # неделя
    add_booking(store, NOW - timedelta(days=20))      # месяц
    add_booking(store, NOW - timedelta(days=100))     # всё время

    add_payment(store, NOW - timedelta(hours=2), amount=1000)
    add_payment(store, NOW - timedelta(days=5), amount=2000)
    add_payment(store, NOW - timedelta(days=25), amount=3000)
    add_payment(store, NOW - timedelta(days=200), amount=4000)

    today = collect_stats(store, "today", now=NOW)
    assert (today["bookings"], today["payments"], today["revenue"]) == (1, 1, 1000)

    week = collect_stats(store, "week", now=NOW)
    assert (week["bookings"], week["payments"], week["revenue"]) == (2, 2, 3000)

    month = collect_stats(store, "month", now=NOW)
    assert (month["bookings"], month["payments"], month["revenue"]) == (3, 3, 6000)

    all_time = collect_stats(store, "all", now=NOW)
    assert (all_time["bookings"], all_time["payments"], all_time["revenue"]) == (4, 4, 10000)


def test_today_boundary_is_local_midnight(store, monkeypatch):
    monkeypatch.setenv("APP_TZ", "Europe/Moscow")
    midnight = NOW.replace(hour=0, minute=0)
    add_booking(store, midnight + timedelta(minutes=1))
    add_booking(store, midnight - timedelta(minutes=1))
    assert collect_stats(store, "today", now=NOW)["bookings"] == 1


def test_pending_payments_not_counted(store):
    add_payment(store, NOW, status="pending")
    stats = collect_stats(store, "all", now=NOW)
    assert stats["payments"] == 0
    assert stats["revenue"] == 0


def test_conversion(store):
    for _ in range(4):
        add_booking(store, NOW)
    add_payment(store, NOW)
    assert collect_stats(store, "all", now=NOW)["conversion_pct"] == 25


def test_conversion_no_bookings_no_crash(store):
    add_payment(store, NOW)
    assert collect_stats(store, "all", now=NOW)["conversion_pct"] == 0


def test_top_products(store):
    add_payment(store, NOW, amount=5000, title="Консультация")
    add_payment(store, NOW, amount=5000, title="Консультация")
    add_payment(store, NOW, amount=30000, title="Ретрит")
    top = collect_stats(store, "all", now=NOW)["top_products"]
    assert top[0]["title"] == "Консультация"
    assert top[0]["count"] == 2
    assert top[0]["total"] == 10000
    assert top[1]["title"] == "Ретрит"


def test_unknown_period_falls_back_to_all(store):
    add_booking(store, NOW - timedelta(days=500))
    assert collect_stats(store, "quarter", now=NOW)["bookings"] == 1


def test_broken_dates_ignored_for_periods(store):
    store.put("bookings", {"name": "x", "created_at": "мусор"})
    add_booking(store, NOW)
    assert collect_stats(store, "today", now=NOW)["bookings"] == 1
    # за всё время учитываются все записи
    assert collect_stats(store, "all", now=NOW)["bookings"] == 2


def test_format_stats_lists_top(store):
    add_booking(store, NOW)
    add_payment(store, NOW, amount=15000, title="Тренинг Ведьма")
    text = format_stats(collect_stats(store, "all", now=NOW))
    assert "Заявок: <b>1</b>" in text
    assert "Оплат: <b>1</b>" in text
    assert "15 000 ₽" in text
    assert "Конверсия" in text
    assert "Тренинг Ведьма — 1 шт." in text
