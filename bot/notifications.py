"""Уведомления Ольге в Telegram.

Исходящие сообщения шлём напрямую через Bot API (urllib, синхронно) —
это надёжно в serverless-обработчиках без event loop. Никогда не роняем
основной поток: любая ошибка логируется, функция возвращает False.
"""
from __future__ import annotations

import html
import json
import logging
import urllib.request
from typing import Any

from core import config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


def esc(value: Any) -> str:
    """Экранировать пользовательские данные для HTML-сообщений Telegram."""
    return html.escape(str(value if value is not None else ""), quote=False)


def send_message(chat_id: str, text: str) -> bool:
    """Отправить сообщение в чат. False при любой ошибке (и лог)."""
    token = config.bot_token()
    if not token or not chat_id:
        logger.info("TG send пропущен (нет токена или chat_id): %s", text[:120])
        return False
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        # короче лимита serverless-функции: уведомление не должно подвесить запрос
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            logger.error("TG sendMessage не ok: %s", body)
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - уведомление не должно ронять запрос
        logger.error("TG sendMessage ошибка: %s", exc)
        return False


def notify_admin(text: str) -> bool:
    """Сообщение Ольге (в админ-чат)."""
    chat_id = config.admin_chat_id()
    if not chat_id:
        logger.info("TELEGRAM_ADMIN_CHAT_ID не задан, уведомление в лог: %s", text[:200])
        return False
    return send_message(chat_id, text)


# ---------- готовые уведомления ----------


def notify_new_booking(booking: dict[str, Any]) -> bool:
    """Мгновенное уведомление о новой заявке с сайта."""
    lines = [
        "📝 <b>Новая заявка с сайта</b>",
        f"Имя: <b>{esc(booking.get('name'))}</b>",
        f"Контакт: {esc(booking.get('contact'))}",
        f"Формат: {esc(booking.get('format'))}",
    ]
    message = str(booking.get("message") or "").strip()
    if message:
        lines.append(f"Сообщение: {esc(message[:800])}")
    return notify_admin("\n".join(lines))


def notify_payment(payment: dict[str, Any]) -> bool:
    """Мгновенное уведомление об успешной оплате."""
    customer = payment.get("customer") or {}
    who = customer.get("name") or customer.get("contact") or customer.get("email") or "клиент"
    lines = [
        "💰 <b>Успешная оплата</b>",
        f"Кто: <b>{esc(who)}</b>",
        f"Что: {esc(payment.get('product_title'))}",
        f"Сумма: <b>{esc(payment.get('amount'))} ₽</b>",
    ]
    contact = customer.get("contact") or customer.get("email")
    if contact and contact != who:
        lines.append(f"Контакт: {esc(contact)}")
    if payment.get("slot_label"):
        lines.append(f"Слот: {esc(payment['slot_label'])}")
    return notify_admin("\n".join(lines))


def notify_email_stub(to: str, subject: str, body: str) -> bool:
    """Заглушка email: уведомление в бот вместо реальной отправки."""
    return notify_admin(
        "✉️ <b>Email отправлен (stub)</b>\n"
        f"Кому: {esc(to)}\n"
        f"Тема: {esc(subject)}\n"
        f"Содержимое: {esc(body[:1000])}"
    )


def notify_telemost_sent(email: str, slot_label: str, url: str, stub: bool) -> bool:
    """Уведомление об отправке ссылки на Телемост."""
    mode = " (stub)" if stub else ""
    return notify_admin(
        f"🎥 <b>Ссылка на Телемост отправлена{mode}</b>\n"
        f"Кому: {esc(email)}\n"
        f"Сессия: {esc(slot_label)}\n"
        f"Ссылка: {esc(url)}"
    )


def notify_error(context: str, error: str) -> bool:
    """Служебное уведомление об ошибке."""
    return notify_admin(f"⚠️ <b>Ошибка</b> · {esc(context)}\n{esc(error[:500])}")
