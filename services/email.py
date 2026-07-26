"""Отправка email клиентам.

EMAIL_MODE=stub — письмо только логируется + уведомление Ольге в бот.
EMAIL_MODE=live — реальная отправка по SMTP (SMTP_HOST/PORT/USER/PASS/FROM).
Переключение — только через env, без правки кода.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from bot import notifications
from core import config

logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str) -> dict[str, Any]:
    """Отправить письмо. Возвращает {"ok": bool, "mode": str, "detail": str}."""
    to = str(to or "").strip()
    if not to or "@" not in to:
        return {"ok": False, "mode": config.email_mode(), "detail": "некорректный адрес"}

    if config.email_mode() == "live":
        return _send_live(to, subject, body)
    return _send_stub(to, subject, body)


def _send_stub(to: str, subject: str, body: str) -> dict[str, Any]:
    logger.info("EMAIL STUB -> %s | %s | %s", to, subject, body)
    notifications.notify_email_stub(to, subject, body)
    return {"ok": True, "mode": "stub", "detail": "залогировано, уведомление в бот"}


def _send_live(to: str, subject: str, body: str) -> dict[str, Any]:
    host = config.smtp_host()
    port = config.smtp_port()
    user = config.smtp_user()
    password = config.smtp_pass()
    sender = config.smtp_from()

    if not host:
        message = "EMAIL_MODE=live, но SMTP_HOST не задан"
        logger.error(message)
        notifications.notify_error("email", message)
        return {"ok": False, "mode": "live", "detail": message}

    email_message = EmailMessage()
    email_message["From"] = sender
    email_message["To"] = to
    email_message["Subject"] = subject
    email_message.set_content(body)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as client:
                if user:
                    client.login(user, password)
                client.send_message(email_message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as client:
                client.starttls()
                if user:
                    client.login(user, password)
                client.send_message(email_message)
    except Exception as exc:  # noqa: BLE001 - ошибка SMTP не должна ронять запрос
        logger.error("SMTP ошибка при отправке на %s: %s", to, exc)
        notifications.notify_error("email", f"не отправилось на {to}: {exc}")
        return {"ok": False, "mode": "live", "detail": str(exc)}

    logger.info("EMAIL LIVE отправлен -> %s | %s", to, subject)
    return {"ok": True, "mode": "live", "detail": "отправлено по SMTP"}
