"""Отложенная отправка ссылки на Телемост перед индивидуальной сессией.

Механика для serverless без надёжного крона: джобы лежат в хранилище
(kind="jobs"), run_due_jobs() вызывается при каждом входящем API-запросе
и из /api/cron — отправляет то, чему пришло время (за TELEMOST_LEAD_MIN
минут до начала сессии). Идемпотентно: отправленная джоба помечается sent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from bot import notifications, slots as slots_module
from core import config
from services import email as email_service
from storage.store import BaseStore

logger = logging.getLogger(__name__)

KIND = "jobs"

# За сколько минут до начала сессии отправлять ссылку (ТЗ: 15–30 минут)
TELEMOST_LEAD_MIN = 20
# Если джоба «протухла» сильнее этого срока после начала сессии — не шлём
EXPIRE_AFTER = timedelta(hours=3)

STATUS_SCHEDULED = "scheduled"
STATUS_SENT = "sent"
STATUS_EXPIRED = "expired"


def schedule_telemost_delivery(
    store: BaseStore, *, payment: dict[str, Any], slot: dict[str, Any]
) -> dict[str, Any] | None:
    """Запланировать отправку ссылки на Телемост после оплаты слота.

    Если у клиента нет email — уведомляем Ольгу, чтобы отправила вручную.
    """
    customer = payment.get("customer") or {}
    email = str(customer.get("email") or "").strip()
    slot_label = slots_module.format_slot(slot)

    if not email or "@" not in email:
        notifications.notify_admin(
            "🎥 <b>Слот оплачен, но у клиента нет email</b>\n"
            f"Сессия: {notifications.esc(slot_label)}\n"
            f"Контакт: {notifications.esc(customer.get('contact') or '—')}\n"
            "Отправь ссылку на Телемост вручную."
        )
        return None

    start = slots_module.slot_start(slot)
    send_at = start - timedelta(minutes=TELEMOST_LEAD_MIN)
    job = {
        "type": "telemost",
        "status": STATUS_SCHEDULED,
        "payment_id": payment.get("id"),
        "slot_id": slot.get("id"),
        "slot_start": slot.get("start"),
        "slot_label": slot_label,
        "email": email,
        "customer_name": str(customer.get("name") or ""),
        "send_at": send_at.isoformat(),
    }
    saved = store.put(KIND, job)
    logger.info("Телемост запланирован: %s -> %s в %s", saved["id"], email, job["send_at"])
    return saved


def _resolve_url(store: BaseStore, job: dict[str, Any]) -> str:
    """Ссылка на Телемост на момент отправки (слот → настройки бота → env)."""
    slot = store.get("slots", str(job.get("slot_id") or ""))
    if slot is not None:
        return slots_module.telemost_url_for(store, slot)
    return (
        str(store.get_setting("telemost_url") or "").strip()
        or config.default_telemost_url()
    )


def run_due_jobs(store: BaseStore, now: datetime | None = None) -> list[dict[str, Any]]:
    """Отправить все джобы, чьё время пришло. Возвращает обработанные джобы."""
    now = now or datetime.now(config.app_tz())
    processed: list[dict[str, Any]] = []
    for job in store.list(KIND):
        if job.get("type") != "telemost" or job.get("status") != STATUS_SCHEDULED:
            continue
        try:
            send_at = datetime.fromisoformat(str(job.get("send_at")))
            slot_start = datetime.fromisoformat(str(job.get("slot_start")))
        except ValueError:
            logger.error("Телемост-джоба %s с битыми датами, пропуск", job.get("id"))
            continue
        if now < send_at:
            continue

        if now > slot_start + EXPIRE_AFTER:
            job["status"] = STATUS_EXPIRED
            store.put(KIND, job)
            notifications.notify_error(
                "телемост",
                f"джоба для {job.get('email')} на {job.get('slot_label')} просрочена и не отправлена",
            )
            processed.append(job)
            continue

        url = _resolve_url(store, job)
        if not url:
            # Ссылки нигде нет — Ольге нужно задать её в Настройках; попробуем в следующий раз
            notifications.notify_error(
                "телемост",
                f"нет ссылки на Телемост для сессии {job.get('slot_label')} — "
                "задай в боте: Настройки → Ссылка на Телемост",
            )
            continue

        name = str(job.get("customer_name") or "").strip()
        greeting = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
        body = (
            f"{greeting}\n\n"
            f"Ваша индивидуальная сессия с Ольгой Андреевой начнётся уже скоро: "
            f"{job.get('slot_label')}.\n\n"
            f"Ссылка для подключения (Яндекс Телемост):\n{url}\n\n"
            "До встречи!"
        )
        result = email_service.send_email(
            str(job.get("email")), "Ссылка на вашу сессию — Ольга Андреева", body
        )
        if result.get("ok"):
            job["status"] = STATUS_SENT
            job["sent_at"] = now.isoformat()
            store.put(KIND, job)
            notifications.notify_telemost_sent(
                str(job.get("email")), str(job.get("slot_label")), url,
                stub=result.get("mode") == "stub",
            )
            processed.append(job)
        else:
            logger.error("Телемост-джоба %s: письмо не ушло: %s", job.get("id"), result)
    return processed
