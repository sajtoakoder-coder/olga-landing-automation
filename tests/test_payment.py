"""Тесты оплаты: create → страница /pay → webhook → автовыдача → status.

Внешние зависимости (Telegram, SMTP) замоканы; хранилище — временный файл.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from bot import products, slots as slots_module
from core import payments
from services import email as email_service


@pytest.fixture
def sent_emails(monkeypatch):
    """Перехват services.email.send_email (используется автовыдачей)."""
    calls: list[dict] = []

    def fake_send(to: str, subject: str, body: str):
        calls.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "mode": "stub", "detail": "мок"}

    monkeypatch.setattr(email_service, "send_email", fake_send)
    return calls


@pytest.fixture
def product(store):
    return products.add_product(
        store,
        title="Онлайн-тренинг",
        description="1,5 месяца",
        price=15000,
        kind="payment",
        delivery="email",
        delivery_content="Ссылка на закрытый чат: https://t.me/+secret",
    )


def create_payment_via_api(api, product_id: str) -> dict:
    status, data, _ = api("POST", "/api/payment/create", json_body={"product_id": product_id})
    assert status == 200
    assert data["ok"] is True
    return data


# ---------- create ----------


def test_create_returns_pay_url(api, product, monkeypatch):
    monkeypatch.setenv("APP_URL", "https://olga.vercel.app")
    data = create_payment_via_api(api, product["id"])
    assert data["url"] == f"https://olga.vercel.app/pay?id={data['payment_id']}"


def test_create_get_redirects_to_pay_page(api, product):
    status, _, headers = api(
        "GET", "/api/payment/create", query={"product_id": [product["id"]]}
    )
    assert status == 302
    assert "/pay?id=" in headers["Location"]


def test_create_unknown_product_404(api, admin_notify):
    status, data, _ = api("POST", "/api/payment/create", json_body={"product_id": "nope"})
    assert status == 404
    assert data["ok"] is False


def test_create_without_product_id_400(api):
    status, data, _ = api("POST", "/api/payment/create", json_body={})
    assert status == 400


def test_create_for_request_kind_product_400(api, store):
    request_product = products.add_product(store, title="Сопровождение", price=0, kind="request")
    status, data, _ = api(
        "POST", "/api/payment/create", json_body={"product_id": request_product["id"]}
    )
    assert status == 400
    assert "заявку" in data["error"]


# ---------- страница /pay ----------


def test_pay_page_renders_stub(api, product):
    payment = create_payment_via_api(api, product["id"])
    status, page, headers = api("GET", "/pay", query={"id": [payment["payment_id"]]})
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "Онлайн-тренинг" in page
    assert "15 000 ₽" in page
    assert "тестовая оплата" in page.lower()
    assert "Подтвердить оплату (тест)" in page
    # подпись не светится в HTML (выдаётся только после POST /api/pay)
    assert payments.stub_signature(payment["payment_id"]) not in page


def test_pay_page_unknown_payment_404(api):
    status, _, _ = api("GET", "/pay", query={"id": ["nope"]})
    assert status == 404


def test_pay_page_escapes_product_title(api, store):
    xss = products.add_product(store, title="<script>alert(1)</script>", price=100)
    payment = create_payment_via_api(api, xss["id"])
    _, page, _ = api("GET", "/pay", query={"id": [payment["payment_id"]]})
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_pay_post_requires_email_for_email_delivery(api, product):
    payment = create_payment_via_api(api, product["id"])
    status, data, _ = api(
        "POST", "/api/pay", json_body={"payment_id": payment["payment_id"], "email": ""}
    )
    assert status == 400
    assert "email" in data["error"].lower()


def test_pay_post_invalid_email_400(api, product):
    payment = create_payment_via_api(api, product["id"])
    status, data, _ = api(
        "POST", "/api/pay", json_body={"payment_id": payment["payment_id"], "email": "не почта"}
    )
    assert status == 400


def test_pay_post_stub_returns_signature_and_redirect(api, product, store):
    payment = create_payment_via_api(api, product["id"])
    status, data, _ = api(
        "POST",
        "/api/pay",
        json_body={
            "payment_id": payment["payment_id"],
            "name": "Анна",
            "contact": "@anna",
            "email": "anna@mail.ru",
        },
    )
    assert status == 200
    assert data["mode"] == "stub"
    assert data["signature"] == payments.stub_signature(payment["payment_id"])
    assert "payment_id=" + payment["payment_id"] in data["redirect"]

    saved = store.get("payments", payment["payment_id"])
    assert saved["customer"] == {"name": "Анна", "contact": "@anna", "email": "anna@mail.ru"}


def test_pay_post_live_redirects_to_yoomoney(api, product, monkeypatch):
    monkeypatch.setenv("PAYMENT_MODE", "live")
    monkeypatch.setenv("YOOMONEY_SHOP_ID", "410011234567")
    payment = create_payment_via_api(api, product["id"])
    status, data, _ = api(
        "POST",
        "/api/pay",
        json_body={"payment_id": payment["payment_id"], "email": "anna@mail.ru"},
    )
    assert status == 200
    assert data["mode"] == "live"
    assert data["redirect"].startswith("https://yoomoney.ru/quickpay/confirm.xml?")
    assert "receiver=410011234567" in data["redirect"]
    assert "label=" + payment["payment_id"] in data["redirect"]


# ---------- webhook (stub) ----------


def confirm_stub(api, payment_id: str):
    return api(
        "POST",
        "/api/payment/webhook",
        json_body={"payment_id": payment_id, "signature": payments.stub_signature(payment_id)},
    )


def test_webhook_stub_confirms_and_delivers_email(api, product, store, sent_emails, admin_notify):
    payment = create_payment_via_api(api, product["id"])
    api(
        "POST",
        "/api/pay",
        json_body={"payment_id": payment["payment_id"], "name": "Анна", "email": "anna@mail.ru"},
    )
    status, data, _ = confirm_stub(api, payment["payment_id"])
    assert status == 200
    assert data["status"] == "paid"
    assert data["duplicate"] is False

    saved = store.get("payments", payment["payment_id"])
    assert saved["status"] == "paid"
    assert saved["paid_at"]
    # автовыдача: письмо с содержимым товара
    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == "anna@mail.ru"
    assert "https://t.me/+secret" in sent_emails[0]["body"]
    # уведомление об оплате
    assert any("Успешная оплата" in c for c in admin_notify)
    assert any("15000 ₽" in c for c in admin_notify)


def test_webhook_wrong_signature_400(api, product, store, admin_notify):
    payment = create_payment_via_api(api, product["id"])
    status, data, _ = api(
        "POST",
        "/api/payment/webhook",
        json_body={"payment_id": payment["payment_id"], "signature": "fake"},
    )
    assert status == 400
    assert store.get("payments", payment["payment_id"])["status"] == "pending"


def test_webhook_missing_payment_404(api):
    status, _, _ = api(
        "POST",
        "/api/payment/webhook",
        json_body={"payment_id": "ghost", "signature": payments.stub_signature("ghost")},
    )
    assert status == 404


def test_webhook_idempotent_double_call(api, product, store, sent_emails, admin_notify):
    """Двойной webhook с тем же payment_id не создаёт дубль выдачи/уведомлений."""
    payment = create_payment_via_api(api, product["id"])
    api("POST", "/api/pay", json_body={"payment_id": payment["payment_id"], "email": "a@b.ru"})

    status1, data1, _ = confirm_stub(api, payment["payment_id"])
    emails_after_first = len(sent_emails)
    notifications_after_first = len(admin_notify)

    status2, data2, _ = confirm_stub(api, payment["payment_id"])
    assert status2 == 200
    assert data2["duplicate"] is True
    assert len(sent_emails) == emails_after_first  # письмо не продублировалось
    assert len(admin_notify) == notifications_after_first
    # платежей в базе по-прежнему один
    assert len(store.list("payments")) == 1


# ---------- webhook (ЮMoney live-формат) ----------


def yoomoney_form(payment_id: str, secret: str = "test_secret") -> dict[str, str]:
    form = {
        "notification_type": "p2p-incoming",
        "operation_id": "op-1",
        "amount": "14700.00",
        "currency": "643",
        "datetime": "2026-07-26T12:00:00Z",
        "sender": "41001XXX",
        "codepro": "false",
        "label": payment_id,
    }
    base = "&".join(
        [
            form["notification_type"],
            form["operation_id"],
            form["amount"],
            form["currency"],
            form["datetime"],
            form["sender"],
            form["codepro"],
            secret,
            form["label"],
        ]
    )
    form["sha1_hash"] = hashlib.sha1(base.encode("utf-8")).hexdigest()
    return form


def test_webhook_yoomoney_form_valid_signature(api, product, store, sent_emails, admin_notify):
    payment = create_payment_via_api(api, product["id"])
    api("POST", "/api/pay", json_body={"payment_id": payment["payment_id"], "email": "a@b.ru"})
    status, data, _ = api(
        "POST", "/api/payment/webhook", form_body=yoomoney_form(payment["payment_id"])
    )
    assert status == 200
    assert data["status"] == "paid"
    assert store.get("payments", payment["payment_id"])["status"] == "paid"


def test_webhook_yoomoney_bad_signature_400(api, product, store):
    payment = create_payment_via_api(api, product["id"])
    form = yoomoney_form(payment["payment_id"], secret="wrong_secret")
    status, _, _ = api("POST", "/api/payment/webhook", form_body=form)
    assert status == 400
    assert store.get("payments", payment["payment_id"])["status"] == "pending"


# ---------- статус для success.html ----------


def test_status_pending_and_paid_link_product(api, store, admin_notify):
    link_product = products.add_product(
        store,
        title="Консультация",
        price=5000,
        kind="payment",
        delivery="link",
        delivery_content="https://telemost.yandex.ru/j/room",
    )
    payment = create_payment_via_api(api, link_product["id"])

    status, data, _ = api("GET", "/api/payment/status", query={"id": [payment["payment_id"]]})
    assert status == 200
    assert data["payment"]["status"] == "pending"
    assert data["payment"]["link"] == ""  # до оплаты ссылку не отдаём

    confirm_stub(api, payment["payment_id"])
    status, data, _ = api("GET", "/api/payment/status", query={"id": [payment["payment_id"]]})
    assert data["payment"]["status"] == "paid"
    assert data["payment"]["link"] == "https://telemost.yandex.ru/j/room"
    assert data["payment"]["product_title"] == "Консультация"


def test_status_unknown_404(api):
    status, _, _ = api("GET", "/api/payment/status", query={"id": ["ghost"]})
    assert status == 404


# ---------- слоты в оплате ----------


@pytest.fixture
def slot_product(store):
    return products.add_product(
        store,
        title="Разовая консультация",
        price=5000,
        kind="payment",
        delivery="link",
        delivery_content="",
        requires_slot=True,
    )


@pytest.fixture
def future_slot(store):
    start = datetime.now(timezone.utc) + timedelta(days=2)
    return slots_module.add_slot(
        store,
        start.astimezone().strftime("%Y-%m-%d %H:%M"),
        telemost_url="https://telemost.yandex.ru/j/slot-room",
    )


def test_slot_flow_booking_and_telemost_job(
    api, store, slot_product, future_slot, sent_emails, admin_notify
):
    payment = create_payment_via_api(api, slot_product["id"])
    # страница оплаты предлагает слот
    _, page, _ = api("GET", "/pay", query={"id": [payment["payment_id"]]})
    assert future_slot["id"] in page

    status, data, _ = api(
        "POST",
        "/api/pay",
        json_body={
            "payment_id": payment["payment_id"],
            "name": "Анна",
            "email": "anna@mail.ru",
            "slot_id": future_slot["id"],
        },
    )
    assert status == 200

    confirm_stub(api, payment["payment_id"])

    booked = store.get("slots", future_slot["id"])
    assert booked["status"] == "booked"
    assert booked["payment_id"] == payment["payment_id"]
    assert booked["booked_by"]["name"] == "Анна"
    # слот исчез из доступных
    assert slots_module.available_slots(store) == []
    # телемост-джоба запланирована
    jobs = store.list("jobs")
    assert len(jobs) == 1
    assert jobs[0]["status"] == "scheduled"
    assert jobs[0]["email"] == "anna@mail.ru"
    # статус для success: ссылка придёт на почту
    _, data, _ = api("GET", "/api/payment/status", query={"id": [payment["payment_id"]]})
    assert "email" in data["payment"]["message"].lower()
    assert data["payment"]["slot_label"]


def test_pay_post_slot_already_booked_409(api, store, slot_product, future_slot):
    slots_module.book_slot(store, future_slot["id"], customer={}, payment_id="other")
    payment = create_payment_via_api(api, slot_product["id"])
    status, data, _ = api(
        "POST",
        "/api/pay",
        json_body={
            "payment_id": payment["payment_id"],
            "email": "a@b.ru",
            "slot_id": future_slot["id"],
        },
    )
    assert status == 409
    assert "занят" in data["error"]


def test_pay_post_missing_slot_404(api, slot_product):
    payment = create_payment_via_api(api, slot_product["id"])
    status, _, _ = api(
        "POST",
        "/api/pay",
        json_body={"payment_id": payment["payment_id"], "email": "a@b.ru", "slot_id": "ghost"},
    )
    assert status == 404


def test_slot_stolen_between_details_and_webhook_notifies_admin(
    api, store, slot_product, future_slot, admin_notify
):
    payment = create_payment_via_api(api, slot_product["id"])
    api(
        "POST",
        "/api/pay",
        json_body={
            "payment_id": payment["payment_id"],
            "email": "a@b.ru",
            "slot_id": future_slot["id"],
        },
    )
    # кто-то занял слот, пока клиент оплачивал
    slots_module.book_slot(store, future_slot["id"], customer={"name": "Другая"}, payment_id="other")

    status, data, _ = confirm_stub(api, payment["payment_id"])
    assert status == 200  # оплата всё равно фиксируется
    assert any("уже занят" in c for c in admin_notify)
    # слот остался за первым забронировавшим
    assert store.get("slots", future_slot["id"])["payment_id"] == "other"


# ---------- разное ----------


def test_unknown_path_404(api):
    status, data, _ = api("GET", "/api/nonexistent")
    assert status == 404


def test_products_endpoint_after_payment_flow_still_ok(api, product):
    """Смоук: платёжный флоу не ломает выдачу товаров."""
    payment = create_payment_via_api(api, product["id"])
    confirm_stub(api, payment["payment_id"])
    status, data, _ = api("GET", "/api/products")
    assert status == 200
    assert any(p["id"] == product["id"] for p in data["products"])
