"""Тесты заявок: POST /api/booking → хранилище → уведомление в бот → 200."""
from __future__ import annotations


VALID = {
    "name": "Анна",
    "contact": "@anna",
    "format": "Разовая консультация",
    "message": "Хочу разобраться с запросом",
}


def test_booking_full_flow(api, store, admin_notify):
    status, data, _ = api("POST", "/api/booking", json_body=VALID)
    assert status == 200
    assert data["ok"] is True

    saved = store.get("bookings", data["id"])
    assert saved["name"] == "Анна"
    assert saved["contact"] == "@anna"
    assert saved["format"] == "Разовая консультация"
    assert saved["message"] == "Хочу разобраться с запросом"

    assert len(admin_notify) == 1
    assert "Новая заявка" in admin_notify[0]
    assert "Анна" in admin_notify[0]
    assert "@anna" in admin_notify[0]


def test_booking_message_optional(api, store, admin_notify):
    payload = dict(VALID)
    del payload["message"]
    status, data, _ = api("POST", "/api/booking", json_body=payload)
    assert status == 200
    assert store.get("bookings", data["id"])["message"] == ""


def test_booking_invalid_returns_400_not_500(api, store, admin_notify):
    cases = [
        {},  # пусто
        {**VALID, "name": "A"},  # слишком короткое имя
        {**VALID, "name": ""},
        {**VALID, "contact": "aa"},
        {**VALID, "format": ""},
        {**VALID, "name": "x" * 300},
        {**VALID, "contact": "x" * 300},
        {**VALID, "format": "x" * 300},
    ]
    for payload in cases:
        status, data, _ = api("POST", "/api/booking", json_body=payload)
        assert status == 400, payload
        assert data["ok"] is False
        assert data["error"]
    assert store.list("bookings") == []
    assert admin_notify == []


def test_booking_broken_json_returns_400(store, admin_notify):
    from core.app import dispatch

    # пустое тело
    status, _, _ = dispatch(store, "POST", "/api/booking", {}, b"", "application/json")
    assert status == 400
    # битый JSON
    status, _, _ = dispatch(store, "POST", "/api/booking", {}, b"{broken", "application/json")
    assert status == 400


def test_booking_json_array_rejected(store, admin_notify):
    from core.app import dispatch

    status, _, _ = dispatch(store, "POST", "/api/booking", {}, b"[1,2]", "application/json")
    assert status == 400


def test_booking_huge_message_truncated(api, store, admin_notify):
    payload = {**VALID, "message": "х" * 10_000}
    status, data, _ = api("POST", "/api/booking", json_body=payload)
    assert status == 200
    assert len(store.get("bookings", data["id"])["message"]) == 4000


def test_booking_xss_stored_raw_but_notification_escaped(api, store, monkeypatch):
    """XSS-полезная нагрузка не должна попасть в HTML уведомления неэкранированной."""
    import io
    import json as json_module

    from bot import notifications

    sent_payloads = []

    def fake_urlopen(request, timeout=0):
        sent_payloads.append(json_module.loads(request.data.decode("utf-8")))
        body = json_module.dumps({"ok": True}).encode()
        response = io.BytesIO(body)
        response.__enter__ = lambda *a: response  # type: ignore[attr-defined]
        response.__exit__ = lambda *a: None  # type: ignore[attr-defined]
        return response

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "777")
    monkeypatch.setattr(notifications.urllib.request, "urlopen", fake_urlopen)

    payload = {**VALID, "name": "<img src=x onerror=alert(1)>", "message": "<b>жирно</b>"}
    status, data, _ = api("POST", "/api/booking", json_body=payload)
    assert status == 200

    text = sent_payloads[0]["text"]
    assert "<img" not in text
    assert "&lt;img" in text
    assert "<b>жирно</b>" not in text


def test_booking_wrong_method_404(api):
    status, data, _ = api("GET", "/api/booking")
    assert status == 404
