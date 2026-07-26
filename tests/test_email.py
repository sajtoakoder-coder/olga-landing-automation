"""Тесты отправки email (services/email.py): stub логирует, live зовёт SMTP."""
from __future__ import annotations

from unittest import mock

import pytest

from services import email as email_service


@pytest.fixture
def admin_notify(monkeypatch):
    calls = []
    monkeypatch.setattr(
        email_service.notifications, "notify_admin", lambda text: calls.append(text) or True
    )
    return calls


# ---------- stub ----------


def test_stub_mode_logs_and_notifies(admin_notify, caplog):
    with caplog.at_level("INFO"):
        result = email_service.send_email("client@mail.ru", "Доступ", "https://t.me/+chat")
    assert result["ok"] is True
    assert result["mode"] == "stub"
    assert any("EMAIL STUB" in r.message for r in caplog.records)
    assert len(admin_notify) == 1
    assert "client@mail.ru" in admin_notify[0]
    assert "https://t.me/+chat" in admin_notify[0]


def test_stub_is_default_mode(admin_notify):
    assert email_service.send_email("a@b.c", "s", "b")["mode"] == "stub"


def test_invalid_address_rejected(admin_notify):
    assert email_service.send_email("", "s", "b")["ok"] is False
    assert email_service.send_email("не-адрес", "s", "b")["ok"] is False
    assert admin_notify == []


# ---------- live ----------


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("EMAIL_MODE", "live")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "user@example.com")
    monkeypatch.setenv("SMTP_PASS", "secret")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")


def test_live_mode_sends_via_smtp_ssl(live_env, admin_notify):
    with mock.patch.object(email_service.smtplib, "SMTP_SSL") as smtp_ssl:
        client = smtp_ssl.return_value.__enter__.return_value
        result = email_service.send_email("client@mail.ru", "Тема", "Текст")

    assert result == {"ok": True, "mode": "live", "detail": "отправлено по SMTP"}
    smtp_ssl.assert_called_once_with("smtp.example.com", 465, timeout=20)
    client.login.assert_called_once_with("user@example.com", "secret")
    message = client.send_message.call_args[0][0]
    assert message["To"] == "client@mail.ru"
    assert message["From"] == "noreply@example.com"
    assert message["Subject"] == "Тема"
    assert "Текст" in message.get_content()
    # в live-режиме бот не получает "Email отправлен (stub)"
    assert admin_notify == []


def test_live_mode_port_587_uses_starttls(live_env, monkeypatch, admin_notify):
    monkeypatch.setenv("SMTP_PORT", "587")
    with mock.patch.object(email_service.smtplib, "SMTP") as smtp:
        client = smtp.return_value.__enter__.return_value
        result = email_service.send_email("client@mail.ru", "Тема", "Текст")
    assert result["ok"] is True
    smtp.assert_called_once_with("smtp.example.com", 587, timeout=20)
    client.starttls.assert_called_once()
    client.login.assert_called_once()


def test_live_mode_smtp_error_reported(live_env, admin_notify):
    with mock.patch.object(email_service.smtplib, "SMTP_SSL") as smtp_ssl:
        smtp_ssl.return_value.__enter__.side_effect = OSError("connect fail")
        result = email_service.send_email("client@mail.ru", "Тема", "Текст")
    assert result["ok"] is False
    assert result["mode"] == "live"
    assert len(admin_notify) == 1
    assert "Ошибка" in admin_notify[0]


def test_live_mode_without_host_fails_gracefully(monkeypatch, admin_notify):
    monkeypatch.setenv("EMAIL_MODE", "live")
    result = email_service.send_email("client@mail.ru", "Тема", "Текст")
    assert result["ok"] is False
    assert "SMTP_HOST" in result["detail"]
    assert len(admin_notify) == 1
