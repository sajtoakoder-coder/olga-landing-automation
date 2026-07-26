"""Конфигурация приложения. Всё читается из env динамически (без кеша),
чтобы заглушки менялись на боевые ключи без правки кода."""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo


def env(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


# --- Telegram ---

def bot_token() -> str:
    return env("TELEGRAM_BOT_TOKEN")


def admin_chat_id() -> str:
    return env("TELEGRAM_ADMIN_CHAT_ID")


# --- App ---

def app_url() -> str:
    return env("APP_URL", "http://localhost:3000").rstrip("/")


def payment_mode() -> str:
    return env("PAYMENT_MODE", "stub").lower()


def email_mode() -> str:
    return env("EMAIL_MODE", "stub").lower()


def app_tz() -> ZoneInfo:
    try:
        return ZoneInfo(env("APP_TZ", "Europe/Moscow"))
    except Exception:
        return ZoneInfo("UTC")


# --- ЮMoney ---

def yoomoney_receiver() -> str:
    return env("YOOMONEY_SHOP_ID", "test_shop")


def yoomoney_secret() -> str:
    return env("YOOMONEY_SECRET_KEY", "test_secret")


def yoomoney_redirect_url() -> str:
    return env("YOOMONEY_REDIRECT_URL", app_url() + "/site/success.html")


# --- SMTP ---

def smtp_host() -> str:
    return env("SMTP_HOST")


def smtp_port() -> int:
    try:
        return int(env("SMTP_PORT", "465"))
    except ValueError:
        return 465


def smtp_user() -> str:
    return env("SMTP_USER")


def smtp_pass() -> str:
    return env("SMTP_PASS")


def smtp_from() -> str:
    return env("SMTP_FROM", "noreply@example.com")


# --- Телемост ---

def default_telemost_url() -> str:
    return env("DEFAULT_TELEMOST_URL")
