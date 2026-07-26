"""E2E-смоук: реальные HTTP-запросы к dev-серверу (та же маршрутизация, что на Vercel)."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from scripts.dev_server import make_server


@pytest.fixture
def base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "e2e-store.json"))
    server = make_server(0)
    port = server.server_address[1]
    monkeypatch.setenv("APP_URL", f"http://127.0.0.1:{port}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def http(method: str, url: str, json_body=None):
    data = None
    headers = {}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        with exc:  # закрываем тело ответа, иначе деструктор ругается при GC
            return exc.code, exc.read(), dict(exc.headers)


def http_json(method: str, url: str, json_body=None):
    status, body, headers = http(method, url, json_body)
    return status, json.loads(body.decode("utf-8"))


# ---------- API ----------


def test_products_endpoint_returns_seeded_list(base_url):
    status, data = http_json("GET", f"{base_url}/api/products")
    assert status == 200
    assert data["ok"] is True
    assert isinstance(data["products"], list)
    assert len(data["products"]) == 4  # автосид дефолтных товаров
    titles = [p["title"] for p in data["products"]]
    assert "Разовая консультация" in titles
    # приватное содержимое выдачи не отдаём наружу
    assert all("delivery_content" not in p for p in data["products"])


def test_booking_valid_and_invalid(base_url):
    status, data = http_json(
        "POST",
        f"{base_url}/api/booking",
        {"name": "Анна", "contact": "@anna", "format": "Консультация", "message": "хочу"},
    )
    assert status == 200 and data["ok"] is True

    status, data = http_json("POST", f"{base_url}/api/booking", {"name": "A"})
    assert status == 400 and data["ok"] is False


def test_full_stub_payment_over_http(base_url):
    _, products_data = http_json("GET", f"{base_url}/api/products")
    email_product = next(p for p in products_data["products"] if p["title"].startswith("Онлайн"))

    status, created = http_json(
        "POST", f"{base_url}/api/payment/create", {"product_id": email_product["id"]}
    )
    assert status == 200 and created["url"].startswith(base_url)

    status, page, headers = http("GET", created["url"])
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    page_text = page.decode("utf-8")
    assert "Подтвердить оплату (тест)" in page_text
    assert email_product["title"] in page_text

    status, details = http_json(
        "POST",
        f"{base_url}/api/pay",
        {"payment_id": created["payment_id"], "name": "Анна", "email": "anna@mail.ru"},
    )
    assert status == 200 and details["mode"] == "stub"

    status, hook = http_json(
        "POST",
        f"{base_url}/api/payment/webhook",
        {"payment_id": created["payment_id"], "signature": details["signature"]},
    )
    assert status == 200 and hook["status"] == "paid"

    status, info = http_json(
        "GET", f"{base_url}/api/payment/status?id={created['payment_id']}"
    )
    assert status == 200
    assert info["payment"]["status"] == "paid"


def test_payment_create_missing_product_404_not_500(base_url):
    status, data = http_json("POST", f"{base_url}/api/payment/create", {"product_id": "ghost"})
    assert status == 404


def test_webhook_bad_signature_400_not_500(base_url):
    status, data = http_json(
        "POST", f"{base_url}/api/payment/webhook", {"payment_id": "x", "signature": "bad"}
    )
    assert status == 400


def test_slots_and_cron_endpoints(base_url):
    status, data = http_json("GET", f"{base_url}/api/slots")
    assert status == 200 and data["slots"] == []
    status, data = http_json("GET", f"{base_url}/api/cron")
    assert status == 200 and data["processed"] == 0


def test_bot_webhook_get_reports_missing_token(base_url):
    status, data = http_json("GET", f"{base_url}/api/bot/webhook")
    assert status == 200
    assert data["ok"] is False
    assert "TELEGRAM_BOT_TOKEN" in data["error"]


def test_bot_webhook_post_never_fails(base_url):
    status, data = http_json("POST", f"{base_url}/api/bot/webhook", {"update_id": 1})
    assert status == 200 and data["ok"] is True


# ---------- статика ----------


def test_index_served_with_formats_section(base_url):
    status, body, headers = http("GET", f"{base_url}/")
    assert status == 200
    text = body.decode("utf-8")
    assert 'id="formats"' in text
    assert 'id="bookingForm"' in text
    assert "/api/products" in text  # динамическая загрузка подключена
    assert "/api/booking" in text
    # галереи отзывов и сертификатов
    assert 'id="reviews"' in text
    assert 'id="certificates"' in text
    assert 'id="lightbox"' in text


def test_review_and_certificate_photos_served(base_url):
    for path in ("/photos/reviews/r01.jpg", "/photos/certificates/c01.jpg"):
        status, body, headers = http("GET", f"{base_url}{path}")
        assert status == 200, path
        assert headers.get("Content-Type") == "image/jpeg"
        assert len(body) > 10_000


def test_success_page_served(base_url):
    for path in ("/site/success.html", "/success"):
        status, body, _ = http("GET", f"{base_url}{path}")
        assert status == 200
        assert "Оплата" in body.decode("utf-8")


def test_photos_served(base_url):
    status, body, headers = http("GET", f"{base_url}/photos/01_portrait_red_glove.jpeg")
    assert status == 200
    assert headers.get("Content-Type") == "image/jpeg"
    assert len(body) > 10_000


def test_static_traversal_blocked(base_url):
    status, _, _ = http("GET", f"{base_url}/site/..%2Frequirements.txt")
    assert status == 404


def test_unknown_api_404(base_url):
    status, data = http_json("GET", f"{base_url}/api/ghost")
    assert status == 404
