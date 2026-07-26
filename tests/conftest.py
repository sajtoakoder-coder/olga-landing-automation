"""Общие фикстуры: чистое окружение и файловое хранилище во временной папке."""
from __future__ import annotations

import pytest

from storage.store import FileStore

ENV_VARS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ADMIN_CHAT_ID",
    "YOOMONEY_SHOP_ID",
    "YOOMONEY_SECRET_KEY",
    "YOOMONEY_REDIRECT_URL",
    "YOOMONEY_WEBHOOK_URL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASS",
    "SMTP_FROM",
    "EMAIL_MODE",
    "DEFAULT_TELEMOST_URL",
    "APP_URL",
    "APP_TZ",
    "PAYMENT_MODE",
    "STORAGE_BACKEND",
    "STORAGE_PATH",
    "KV_REST_API_URL",
    "KV_REST_API_TOKEN",
    "UPSTASH_REDIS_REST_URL",
    "UPSTASH_REDIS_REST_TOKEN",
    "VERCEL",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    """Тесты не должны зависеть от env машины разработчика."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


@pytest.fixture
def store(tmp_path):
    """Файловое хранилище во временной папке."""
    return FileStore(str(tmp_path / "store.json"))


@pytest.fixture
def api(store):
    """Помощник вызова API через core.app.dispatch (без HTTP-сервера)."""
    import json as json_module
    import urllib.parse

    from core.app import dispatch

    def call(
        method: str,
        path: str,
        *,
        json_body=None,
        form_body=None,
        query=None,
        content_type: str | None = None,
    ):
        body = b""
        ct = content_type or ""
        if json_body is not None:
            body = json_module.dumps(json_body).encode("utf-8")
            ct = content_type or "application/json"
        elif form_body is not None:
            body = urllib.parse.urlencode(form_body).encode("utf-8")
            ct = content_type or "application/x-www-form-urlencoded"
        status, headers, payload = dispatch(store, method, path, query or {}, body, ct)
        if headers.get("Content-Type", "").startswith("application/json") and payload:
            return status, json_module.loads(payload.decode("utf-8")), headers
        return status, payload.decode("utf-8", errors="replace"), headers

    return call


@pytest.fixture
def admin_notify(monkeypatch):
    """Перехват уведомлений Ольге (bot.notifications.notify_admin)."""
    from bot import notifications

    calls: list[str] = []
    monkeypatch.setattr(notifications, "notify_admin", lambda text: calls.append(text) or True)
    return calls
