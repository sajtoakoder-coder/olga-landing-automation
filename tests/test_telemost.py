"""Тесты отложенной отправки ссылок на Телемост (services/telemost.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot import slots as slots_module
from services import telemost


@pytest.fixture
def admin_notify(monkeypatch):
    calls = []
    monkeypatch.setattr(
        telemost.notifications, "notify_admin", lambda text: calls.append(text) or True
    )
    return calls


@pytest.fixture
def slot(store):
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    return slots_module.add_slot(
        store,
        start.astimezone().strftime("%Y-%m-%d %H:%M"),
        telemost_url="https://telemost.yandex.ru/j/room",
    )


def payment_for(slot_record, email="client@mail.ru"):
    return {
        "id": "pay1",
        "customer": {"name": "Анна", "contact": "@anna", "email": email},
        "slot_id": slot_record["id"],
    }


def test_schedule_sets_send_at_20_min_before(store, slot, admin_notify):
    job = telemost.schedule_telemost_delivery(store, payment=payment_for(slot), slot=slot)
    assert job is not None
    assert job["status"] == "scheduled"
    send_at = datetime.fromisoformat(job["send_at"])
    start = slots_module.slot_start(slot)
    assert start - send_at == timedelta(minutes=20)
    assert 15 <= 20 <= 30  # требование ТЗ: за 15–30 минут


def test_schedule_without_email_notifies_admin(store, slot, admin_notify):
    job = telemost.schedule_telemost_delivery(
        store, payment=payment_for(slot, email=""), slot=slot
    )
    assert job is None
    assert len(admin_notify) == 1
    assert "нет email" in admin_notify[0]
    assert store.list("jobs") == []


def test_run_due_before_time_does_nothing(store, slot, admin_notify):
    telemost.schedule_telemost_delivery(store, payment=payment_for(slot), slot=slot)
    processed = telemost.run_due_jobs(store)
    assert processed == []
    assert store.list("jobs")[0]["status"] == "scheduled"


def test_run_due_sends_once(store, slot, admin_notify, caplog):
    job = telemost.schedule_telemost_delivery(store, payment=payment_for(slot), slot=slot)
    at_time = datetime.fromisoformat(job["send_at"]) + timedelta(minutes=1)

    with caplog.at_level("INFO"):
        processed = telemost.run_due_jobs(store, now=at_time)
    assert len(processed) == 1
    stored = store.get("jobs", job["id"])
    assert stored["status"] == "sent"
    # письмо ушло через stub: в бот два сообщения — "Email отправлен" и "Ссылка отправлена"
    assert any("Email отправлен (stub)" in c for c in admin_notify)
    assert any("Ссылка на Телемост отправлена (stub)" in c for c in admin_notify)
    assert any("https://telemost.yandex.ru/j/room" in c for c in admin_notify)

    # идемпотентность: повторный запуск ничего не шлёт
    admin_notify.clear()
    assert telemost.run_due_jobs(store, now=at_time) == []
    assert admin_notify == []


def test_run_due_expired_job_marked(store, slot, admin_notify):
    job = telemost.schedule_telemost_delivery(store, payment=payment_for(slot), slot=slot)
    too_late = slots_module.slot_start(slot) + timedelta(hours=4)
    processed = telemost.run_due_jobs(store, now=too_late)
    assert len(processed) == 1
    assert store.get("jobs", job["id"])["status"] == "expired"
    assert any("просрочена" in c for c in admin_notify)


def test_run_due_no_url_keeps_scheduled(store, admin_notify, monkeypatch):
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    bare_slot = slots_module.add_slot(store, start.astimezone().strftime("%Y-%m-%d %H:%M"))
    job = telemost.schedule_telemost_delivery(store, payment=payment_for(bare_slot), slot=bare_slot)
    at_time = datetime.fromisoformat(job["send_at"]) + timedelta(minutes=1)

    assert telemost.run_due_jobs(store, now=at_time) == []
    assert store.get("jobs", job["id"])["status"] == "scheduled"
    assert any("Настройки" in c for c in admin_notify)

    # Ольга задала дефолтную ссылку в настройках — следующая проверка отправляет
    store.set_setting("telemost_url", "https://telemost.yandex.ru/j/default")
    admin_notify.clear()
    processed = telemost.run_due_jobs(store, now=at_time)
    assert len(processed) == 1
    assert store.get("jobs", job["id"])["status"] == "sent"
    assert any("https://telemost.yandex.ru/j/default" in c for c in admin_notify)


def test_url_resolved_at_send_time_even_if_slot_deleted(store, slot, admin_notify, monkeypatch):
    monkeypatch.setenv("DEFAULT_TELEMOST_URL", "https://env.example/t")
    job = telemost.schedule_telemost_delivery(store, payment=payment_for(slot), slot=slot)
    slots_module.delete_slot(store, slot["id"])
    at_time = datetime.fromisoformat(job["send_at"]) + timedelta(minutes=1)

    processed = telemost.run_due_jobs(store, now=at_time)
    assert len(processed) == 1
    assert any("https://env.example/t" in c for c in admin_notify)
