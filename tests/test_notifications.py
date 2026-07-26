"""Тесты уведомлений в Telegram (bot/notifications.py)."""
from __future__ import annotations

import io
import json

import pytest

from bot import notifications


@pytest.fixture
def tg_calls(monkeypatch):
    """Мок Telegram API: собирает отправленные запросы."""
    calls: list[dict] = []

    def fake_urlopen(request, timeout=0):
        calls.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data.decode("utf-8")),
            }
        )
        body = json.dumps({"ok": True, "result": {"message_id": 1}}).encode("utf-8")
        response = io.BytesIO(body)
        response.__enter__ = lambda *a: response  # type: ignore[attr-defined]
        response.__exit__ = lambda *a: None  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(notifications.urllib.request, "urlopen", fake_urlopen)
    return calls


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "777")


def test_send_message_posts_to_bot_api(tg_calls, admin_env):
    assert notifications.send_message("777", "привет") is True
    call = tg_calls[0]
    assert call["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert call["payload"]["chat_id"] == "777"
    assert call["payload"]["text"] == "привет"
    assert call["payload"]["parse_mode"] == "HTML"


def test_send_message_without_token_skips(tg_calls):
    assert notifications.send_message("777", "привет") is False
    assert tg_calls == []


def test_notify_admin_without_chat_id_skips(tg_calls, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    assert notifications.notify_admin("тест") is False
    assert tg_calls == []


def test_send_message_network_error_returns_false(monkeypatch, admin_env):
    def boom(request, timeout=0):
        raise OSError("нет сети")

    monkeypatch.setattr(notifications.urllib.request, "urlopen", boom)
    assert notifications.send_message("777", "x") is False


def test_send_message_api_not_ok_returns_false(monkeypatch, admin_env):
    def not_ok(request, timeout=0):
        body = json.dumps({"ok": False, "description": "Bad Request"}).encode()
        response = io.BytesIO(body)
        response.__enter__ = lambda *a: response  # type: ignore[attr-defined]
        response.__exit__ = lambda *a: None  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(notifications.urllib.request, "urlopen", not_ok)
    assert notifications.send_message("777", "x") is False


def test_notify_new_booking_escapes_html(tg_calls, admin_env):
    notifications.notify_new_booking(
        {
            "name": "<script>alert(1)</script>",
            "contact": "@anna",
            "format": "Консультация",
            "message": "хочу <b>всё</b>",
        }
    )
    text = tg_calls[0]["payload"]["text"]
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&lt;b&gt;" in text
    assert "Новая заявка" in text


def test_notify_payment_contents(tg_calls, admin_env):
    notifications.notify_payment(
        {
            "product_title": "Тренинг",
            "amount": 15000,
            "customer": {"name": "Анна", "contact": "@anna"},
            "slot_label": "01.08.2026 15:00 (60 мин)",
        }
    )
    text = tg_calls[0]["payload"]["text"]
    assert "Успешная оплата" in text
    assert "Анна" in text
    assert "Тренинг" in text
    assert "15000 ₽" in text
    assert "@anna" in text
    assert "01.08.2026 15:00" in text


def test_notify_email_stub_message(tg_calls, admin_env):
    notifications.notify_email_stub("a@b.c", "Доступ", "https://t.me/+x")
    text = tg_calls[0]["payload"]["text"]
    assert "Email отправлен (stub)" in text
    assert "a@b.c" in text
    assert "https://t.me/+x" in text


def test_long_text_truncated(tg_calls, admin_env):
    notifications.send_message("777", "x" * 10000)
    assert len(tg_calls[0]["payload"]["text"]) == 4000
