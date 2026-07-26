"""Статистика для бота: заявки, оплаты, конверсия, топ товаров по периодам."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from core import config
from bot.products import format_price
from storage.store import BaseStore

PERIODS = ("today", "week", "month", "all")
PERIOD_LABELS = {
    "today": "за сегодня",
    "week": "за неделю",
    "month": "за месяц",
    "all": "за всё время",
}


def _period_start(period: str, now: datetime) -> datetime | None:
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return now - timedelta(days=30)
    return None  # all


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.app_tz())
    return parsed.astimezone(config.app_tz())


def _in_period(value: Any, start: datetime | None) -> bool:
    if start is None:
        return True
    parsed = _parse_dt(value)
    return parsed is not None and parsed >= start


def collect_stats(
    store: BaseStore, period: str = "all", now: datetime | None = None
) -> dict[str, Any]:
    """Подсчитать статистику за период: today | week | month | all."""
    if period not in PERIODS:
        period = "all"
    now = now or datetime.now(config.app_tz())
    start = _period_start(period, now)

    bookings = [b for b in store.list("bookings") if _in_period(b.get("created_at"), start)]
    paid = [
        p
        for p in store.list("payments")
        if p.get("status") == "paid" and _in_period(p.get("paid_at") or p.get("created_at"), start)
    ]

    revenue = sum(int(p.get("amount", 0)) for p in paid)
    conversion = round(100 * len(paid) / len(bookings)) if bookings else 0

    by_product: dict[str, dict[str, Any]] = {}
    for payment in paid:
        title = str(payment.get("product_title") or "—")
        entry = by_product.setdefault(title, {"title": title, "count": 0, "total": 0})
        entry["count"] += 1
        entry["total"] += int(payment.get("amount", 0))
    top_products = sorted(by_product.values(), key=lambda e: (-e["count"], -e["total"]))

    return {
        "period": period,
        "bookings": len(bookings),
        "payments": len(paid),
        "revenue": revenue,
        "conversion_pct": conversion,
        "top_products": top_products,
    }


def format_stats(stats: dict[str, Any]) -> str:
    """Текст статистики для сообщения в боте."""
    label = PERIOD_LABELS.get(stats.get("period", "all"), "за всё время")
    lines = [
        f"📊 <b>Статистика {label}</b>",
        f"Заявок: <b>{stats['bookings']}</b>",
        f"Оплат: <b>{stats['payments']}</b>",
        f"Сумма: <b>{format_price(stats['revenue'])}</b>",
        f"Конверсия заявка → оплата: <b>{stats['conversion_pct']}%</b>",
    ]
    top = stats.get("top_products") or []
    if top:
        lines.append("")
        lines.append("Топ товаров:")
        for i, entry in enumerate(top[:5], 1):
            lines.append(
                f"{i}. {entry['title']} — {entry['count']} шт. ({format_price(entry['total'])})"
            )
    return "\n".join(lines)
