"""Слоты расписания для индивидуальных сессий.

Слот создаёт Ольга через бота; клиент выбирает свободный слот при оплате.
Забронированный слот исчезает из доступных автоматически.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from core import config
from core.errors import ConflictError, NotFoundError, ValidationError
from storage.store import BaseStore

KIND = "slots"

STATUS_FREE = "free"
STATUS_BOOKED = "booked"

# "2026-08-01 15:00" или "01.08.2026 15:00"
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})$")
_RU_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})[ T](\d{1,2}):(\d{2})$")


def parse_start(value: str | datetime) -> datetime:
    """Разобрать время начала слота. Наивное время трактуется в APP_TZ."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = " ".join(str(value).split())
        match = _ISO_RE.match(text)
        if match:
            year, month, day, hour, minute = (int(g) for g in match.groups())
        else:
            match = _RU_RE.match(text)
            if match:
                day, month, year, hour, minute = (int(g) for g in match.groups())
            else:
                raise ValidationError(
                    "не смогла разобрать дату. Формат: 2026-08-01 15:00 или 01.08.2026 15:00"
                )
        try:
            parsed = datetime(year, month, day, hour, minute)
        except ValueError as exc:
            raise ValidationError(f"несуществующая дата/время: {exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.app_tz())
    return parsed


def now_local() -> datetime:
    return datetime.now(config.app_tz())


def add_slot(
    store: BaseStore,
    start: str | datetime,
    *,
    telemost_url: str = "",
    duration_min: int = 60,
) -> dict[str, Any]:
    """Добавить свободный слот."""
    start_dt = parse_start(start)
    if duration_min <= 0 or duration_min > 24 * 60:
        raise ValidationError("длительность: от 1 до 1440 минут")
    telemost_url = str(telemost_url or "").strip()
    if telemost_url and not telemost_url.lower().startswith(("http://", "https://")):
        raise ValidationError("ссылка на Телемост должна начинаться с http(s)://")
    for existing in store.list(KIND):
        if existing.get("start") == start_dt.isoformat():
            raise ConflictError("слот на это время уже существует")
    record = {
        "start": start_dt.isoformat(),
        "duration_min": int(duration_min),
        "telemost_url": telemost_url,
        "status": STATUS_FREE,
        "booked_by": None,
        "payment_id": None,
    }
    return store.put(KIND, record)


def add_slots_bulk(store: BaseStore, text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Добавить слоты пачкой — по строке на слот. Возвращает (добавленные, ошибки)."""
    added: list[dict[str, Any]] = []
    errors: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            added.append(add_slot(store, line))
        except (ValidationError, ConflictError) as exc:
            errors.append(f"«{line}»: {exc}")
    return added, errors


def list_slots(store: BaseStore, include_booked: bool = True) -> list[dict[str, Any]]:
    """Слоты, отсортированные по времени начала."""
    slots = store.list(KIND)
    if not include_booked:
        slots = [s for s in slots if s.get("status") == STATUS_FREE]
    return sorted(slots, key=lambda s: str(s.get("start", "")))


def available_slots(store: BaseStore) -> list[dict[str, Any]]:
    """Свободные слоты в будущем — то, что видит клиент."""
    now = now_local()
    result = []
    for slot in list_slots(store, include_booked=False):
        try:
            start = datetime.fromisoformat(slot["start"])
        except (KeyError, ValueError):
            continue
        if start > now:
            result.append(slot)
    return result


def get_slot(store: BaseStore, slot_id: str) -> dict[str, Any]:
    slot = store.get(KIND, slot_id)
    if slot is None:
        raise NotFoundError(f"слот {slot_id} не найден")
    return slot


def delete_slot(store: BaseStore, slot_id: str) -> bool:
    return store.delete(KIND, slot_id)


def book_slot(
    store: BaseStore,
    slot_id: str,
    *,
    customer: dict[str, Any] | None = None,
    payment_id: str | None = None,
) -> dict[str, Any]:
    """Забронировать слот. Забронированный слот исчезает из available_slots."""
    slot = get_slot(store, slot_id)
    if slot.get("status") == STATUS_BOOKED:
        raise ConflictError("слот уже забронирован")
    slot["status"] = STATUS_BOOKED
    slot["booked_by"] = dict(customer) if customer else {}
    slot["payment_id"] = payment_id
    return store.put(KIND, slot)


def slot_start(slot: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(slot["start"])


def slot_end(slot: dict[str, Any]) -> datetime:
    return slot_start(slot) + timedelta(minutes=int(slot.get("duration_min", 60)))


def telemost_url_for(store: BaseStore, slot: dict[str, Any]) -> str:
    """Ссылка Телемоста слота: своя, иначе из настроек бота, иначе из env."""
    return (
        str(slot.get("telemost_url") or "").strip()
        or str(store.get_setting("telemost_url") or "").strip()
        or config.default_telemost_url()
    )


def format_slot(slot: dict[str, Any]) -> str:
    """Человекочитаемое представление слота для бота: 01.08 15:00 (60 мин)."""
    try:
        start = slot_start(slot)
        text = start.strftime("%d.%m.%Y %H:%M")
    except (KeyError, ValueError):
        text = str(slot.get("start", "?"))
    return f"{text} ({int(slot.get('duration_min', 60))} мин)"
