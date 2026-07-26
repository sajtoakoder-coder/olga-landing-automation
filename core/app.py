"""Маршрутизация и реализация всех API-эндпоинтов.

Каждый api/*.py на Vercel — тонкая обёртка, вызывающая dispatch() со своим
путём. Локальный dev-сервер и тесты используют тот же dispatch — поведение
одинаково везде.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from html import escape
from typing import Any

from bot import notifications, products, slots as slots_module
from core import config, payments
from core.errors import ConflictError, NotFoundError, ValidationError
from seed import ensure_seed
from services import telemost
from storage.store import BaseStore, StorageError

logger = logging.getLogger(__name__)

Response = tuple[int, dict[str, str], bytes]

JSON_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "no-store",
}
HTML_HEADERS = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}

MAX_MESSAGE_LEN = 4000
MAX_FIELD_LEN = 200


def json_response(status: int, data: Any) -> Response:
    return status, dict(JSON_HEADERS), json.dumps(data, ensure_ascii=False).encode("utf-8")


def html_response(status: int, body: str) -> Response:
    return status, dict(HTML_HEADERS), body.encode("utf-8")


def error_response(status: int, message: str) -> Response:
    return json_response(status, {"ok": False, "error": message})


def _first(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0] if values else ""


def _parse_json(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError(f"некорректный JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("ожидается JSON-объект")
    return data


def _parse_form(body: bytes) -> dict[str, str]:
    pairs = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] for key, values in pairs.items()}


# ============ эндпоинты ============


def products_get(store: BaseStore) -> Response:
    """GET /api/products — видимые товары для лендинга (+автосид при пустой базе)."""
    ensure_seed(store)
    items = [products.to_public(p) for p in products.visible_products(store)]
    return json_response(200, {"ok": True, "products": items})


def slots_get(store: BaseStore) -> Response:
    """GET /api/slots — свободные будущие слоты для выбора клиентом."""
    items = [
        {"id": s["id"], "start": s["start"], "label": slots_module.format_slot(s)}
        for s in slots_module.available_slots(store)
    ]
    return json_response(200, {"ok": True, "slots": items})


def booking_post(store: BaseStore, body: bytes) -> Response:
    """POST /api/booking — заявка с формы лендинга."""
    data = _parse_json(body)

    name = " ".join(str(data.get("name") or "").split())
    contact = " ".join(str(data.get("contact") or "").split())
    fmt = " ".join(str(data.get("format") or "").split())
    message = str(data.get("message") or "").strip()

    if len(name) < 2:
        raise ValidationError("имя: минимум 2 символа")
    if len(name) > MAX_FIELD_LEN:
        raise ValidationError("имя: слишком длинное")
    if len(contact) < 3:
        raise ValidationError("контакт: укажите telegram, email или телефон")
    if len(contact) > MAX_FIELD_LEN:
        raise ValidationError("контакт: слишком длинный")
    if not fmt:
        raise ValidationError("формат: выберите формат работы")
    if len(fmt) > MAX_FIELD_LEN:
        raise ValidationError("формат: слишком длинный")
    message = message[:MAX_MESSAGE_LEN]  # огромные сообщения обрезаем

    booking = store.put(
        "bookings",
        {"name": name, "contact": contact, "format": fmt, "message": message},
    )
    notifications.notify_new_booking(booking)
    return json_response(200, {"ok": True, "id": booking["id"]})


def payment_create(store: BaseStore, method: str, query: dict[str, list[str]], body: bytes) -> Response:
    """POST /api/payment/create {product_id} → {url}. GET — 302 на страницу оплаты."""
    if method == "GET":
        product_id = _first(query, "product_id")
    else:
        product_id = str(_parse_json(body).get("product_id") or "")
    if not product_id:
        raise ValidationError("нет product_id")

    payment = payments.create_payment(store, product_id)
    url = payments.pay_page_url(payment)
    if method == "GET":
        headers = dict(JSON_HEADERS)
        headers["Location"] = url
        return 302, headers, b""
    return json_response(200, {"ok": True, "payment_id": payment["id"], "url": url})


def payment_webhook(store: BaseStore, body: bytes, content_type: str) -> Response:
    """POST /api/payment/webhook — подтверждение оплаты (stub JSON или ЮMoney form)."""
    form_mode = "json" not in (content_type or "").lower()
    payload: dict[str, Any] = _parse_form(body) if form_mode else _parse_json(body)
    payment, already = payments.handle_webhook(store, payload, form_mode=form_mode)
    return json_response(
        200,
        {
            "ok": True,
            "payment_id": payment["id"],
            "status": payment["status"],
            "duplicate": already,
        },
    )


def payment_status(store: BaseStore, query: dict[str, list[str]]) -> Response:
    """GET /api/payment/status?id= — статус для страницы success."""
    payment_id = _first(query, "id") or _first(query, "payment_id")
    payment = payments.get_payment(store, payment_id)
    return json_response(200, {"ok": True, "payment": payments.public_status(store, payment)})


def pay_page(store: BaseStore, method: str, query: dict[str, list[str]], body: bytes) -> Response:
    """GET /pay?id= — страница оплаты. POST /api/pay — сохранить данные клиента."""
    if method == "GET":
        payment_id = _first(query, "id") or _first(query, "payment_id")
        payment = payments.get_payment(store, payment_id)
        return html_response(200, render_pay_page(store, payment))

    data = _parse_json(body)
    payment = payments.attach_details(
        store,
        str(data.get("payment_id") or ""),
        name=str(data.get("name") or ""),
        contact=str(data.get("contact") or ""),
        email=str(data.get("email") or ""),
        slot_id=str(data.get("slot_id") or "") or None,
    )
    if payment.get("status") == payments.STATUS_PAID:
        return json_response(
            200, {"ok": True, "mode": "paid", "redirect": payments.success_url(payment)}
        )
    if config.payment_mode() == "live":
        return json_response(
            200, {"ok": True, "mode": "live", "redirect": payments.yoomoney_url(payment)}
        )
    return json_response(
        200,
        {
            "ok": True,
            "mode": "stub",
            "payment_id": payment["id"],
            "signature": payments.stub_signature(payment["id"]),
            "redirect": payments.success_url(payment),
        },
    )


def bot_webhook(store: BaseStore, method: str, body: bytes) -> Response:
    """POST — апдейт Telegram; GET — установить webhook и показать статус."""
    if method == "GET":
        return json_response(200, _setup_bot_webhook())

    try:
        update = _parse_json(body)
    except ValidationError as exc:
        logger.error("Бот-вебхук: %s", exc)
        return json_response(200, {"ok": True})  # Telegram не должен ретраить мусор
    if update:
        from bot import handlers  # ленивый импорт aiogram — не грузим в остальных функциях

        try:
            asyncio.run(handlers.process_update(store, update))
        except Exception as exc:  # noqa: BLE001 - апдейт не должен ронять вебхук
            logger.exception("Ошибка обработки апдейта: %s", exc)
    return json_response(200, {"ok": True})


def _setup_bot_webhook() -> dict[str, Any]:
    token = config.bot_token()
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN не задан"}
    webhook_url = f"{config.app_url()}/api/bot/webhook"
    try:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/setWebhook",
            data=json.dumps({"url": webhook_url, "drop_pending_updates": False}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"setWebhook не удался: {exc}"}
    return {
        "ok": bool(result.get("ok")),
        "webhook_url": webhook_url,
        "telegram_response": result.get("description") or result.get("result"),
        "admin_chat_id_set": bool(config.admin_chat_id()),
        "hint": "Отправьте боту /start, чтобы получить chat_id"
        if not config.admin_chat_id()
        else "",
    }


def cron(store: BaseStore) -> Response:
    """GET /api/cron — обработать отложенные отправки (Телемост)."""
    processed = telemost.run_due_jobs(store)
    return json_response(200, {"ok": True, "processed": len(processed)})


# ============ диспетчер ============


def dispatch(
    store: BaseStore,
    method: str,
    path: str,
    query: dict[str, list[str]] | None = None,
    body: bytes = b"",
    content_type: str = "",
) -> Response:
    """Обработать запрос. Возвращает (status, headers, body)."""
    method = method.upper()
    path = path.split("?")[0].rstrip("/") or "/"
    query = query or {}

    if method == "OPTIONS":
        return 204, dict(JSON_HEADERS), b""

    # отложенные джобы проверяем при каждом запросе (ТЗ) — кроме самого крона
    if path != "/api/cron":
        try:
            telemost.run_due_jobs(store)
        except Exception as exc:  # noqa: BLE001 - джобы не должны ломать запрос
            logger.error("run_due_jobs: %s", exc)

    try:
        if path == "/api/products" and method == "GET":
            return products_get(store)
        if path == "/api/slots" and method == "GET":
            return slots_get(store)
        if path == "/api/booking" and method == "POST":
            return booking_post(store, body)
        if path == "/api/payment/create" and method in ("GET", "POST"):
            return payment_create(store, method, query, body)
        if path == "/api/payment/webhook" and method == "POST":
            return payment_webhook(store, body, content_type)
        if path == "/api/payment/status" and method == "GET":
            return payment_status(store, query)
        if path in ("/pay", "/api/pay") and method in ("GET", "POST"):
            return pay_page(store, method, query, body)
        if path == "/api/bot/webhook" and method in ("GET", "POST"):
            return bot_webhook(store, method, body)
        if path == "/api/cron" and method == "GET":
            return cron(store)
        return error_response(404, "не найдено")
    except ValidationError as exc:
        return error_response(400, str(exc))
    except NotFoundError as exc:
        return error_response(404, str(exc))
    except ConflictError as exc:
        return error_response(409, str(exc))
    except StorageError as exc:
        logger.error("Хранилище недоступно: %s", exc)
        return error_response(503, "хранилище временно недоступно")
    except Exception as exc:  # noqa: BLE001 - 500 с логом вместо падения
        logger.exception("Необработанная ошибка %s %s: %s", method, path, exc)
        return error_response(500, "внутренняя ошибка")


# ============ страница оплаты ============

PAY_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Оплата — Ольга Андреева</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Bodoni+Moda:ital,opsz,wght@0,6..96,400;0,6..96,500;1,6..96,400&family=Cormorant+Garamond:ital,wght@0,400;1,400&family=Manrope:wght@300;400;500;600&display=swap" rel="stylesheet" />
<style>
:root{--ink:#0a0908;--paper:#f2ede4;--paper-2:#e8e1d4;--dust:#8a7d70;--dust-2:#5a4f47;--blood:#c94a3a;--blood-deep:#9c3324;--serif:'Bodoni Moda',serif;--serif-italic:'Cormorant Garamond',Georgia,serif;--sans:'Manrope',system-ui,sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--ink);color:var(--paper);font-family:var(--sans);min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px}
.brand{font-family:var(--serif);letter-spacing:.32em;text-transform:uppercase;font-size:15px;margin-bottom:28px;color:var(--paper)}
.brand em{font-style:italic;color:var(--blood);letter-spacing:.14em}
.card{background:var(--paper);color:var(--ink);max-width:460px;width:100%;padding:40px 36px;position:relative}
.card::before{content:'FORMULARIUM · SOLVO';position:absolute;top:12px;right:16px;font-size:9px;letter-spacing:.4em;color:var(--dust-2)}
.stub-note{background:rgba(201,74,58,.09);border:1px dashed var(--blood-deep);color:var(--blood-deep);font-size:12px;letter-spacing:.06em;padding:10px 14px;margin-bottom:22px;line-height:1.5}
h1{font-family:var(--serif);font-weight:400;font-size:30px;line-height:1.08;margin-bottom:6px}
h1 em{font-style:italic;color:var(--blood-deep)}
.price{font-family:var(--serif);font-size:22px;margin:10px 0 24px}
.price small{display:block;font-family:var(--sans);font-size:10px;letter-spacing:.4em;text-transform:uppercase;color:var(--dust-2);margin-bottom:4px}
.field{display:flex;flex-direction:column;gap:6px;margin-bottom:18px}
.field label{font-size:10px;letter-spacing:.4em;text-transform:uppercase;color:var(--dust-2)}
.field label .req{color:var(--blood-deep)}
.field input,.field select{background:transparent;border:0;border-bottom:1px solid var(--ink);padding:10px 0;font-family:var(--serif);font-style:italic;font-size:18px;color:var(--ink);outline:none;border-radius:0;width:100%}
.field input:focus,.field select:focus{border-color:var(--blood-deep)}
.error{display:none;color:var(--blood-deep);font-size:12px;letter-spacing:.06em;margin:-6px 0 14px}
.error.show{display:block}
button.pay{width:100%;margin-top:8px;padding:20px 26px;background:var(--ink);color:var(--paper);font-family:var(--sans);font-size:12px;letter-spacing:.4em;text-transform:uppercase;border:0;cursor:pointer;display:flex;justify-content:space-between;align-items:center;transition:background .3s}
button.pay:hover{background:var(--blood-deep)}
button.pay:disabled{opacity:.6;cursor:wait}
button.pay .arr{font-family:var(--serif);font-style:italic;font-size:18px;letter-spacing:0}
.back{margin-top:22px;font-size:11px;letter-spacing:.3em;text-transform:uppercase;color:var(--dust)}
.back a{color:inherit;text-decoration:none;border-bottom:1px solid var(--dust-2);padding-bottom:2px}
.back a:hover{color:var(--paper)}
</style>
</head>
<body>
<div class="brand">Olga <em>Andreeva</em></div>
<div class="card">
  __STUB_NOTE__
  <h1>__TITLE__</h1>
  <div class="price"><small>к оплате</small>__PRICE__</div>
  <form id="payForm" novalidate>
    <div class="field"><label for="p-name">Имя</label>
      <input id="p-name" type="text" autocomplete="name" /></div>
    <div class="field"><label for="p-contact">Telegram для связи</label>
      <input id="p-contact" type="text" placeholder="@nickname" /></div>
    <div class="field"><label for="p-email">Email __EMAIL_REQ__</label>
      <input id="p-email" type="email" autocomplete="email" placeholder="you@mail.ru" /></div>
    __SLOT_FIELD__
    <div class="error" id="payError"></div>
    <button class="pay" type="submit"><span>__BUTTON__</span><span class="arr">→</span></button>
  </form>
</div>
<div class="back"><a href="__BACK_URL__">← вернуться на сайт</a></div>
<script>
(function(){
  var PAYMENT_ID = "__PAYMENT_ID__";
  var MODE = "__MODE__";
  var form = document.getElementById('payForm');
  var errorBox = document.getElementById('payError');
  var button = form.querySelector('button.pay');
  var emailRequired = __EMAIL_REQUIRED_JS__;

  function fail(text){ errorBox.textContent = text; errorBox.classList.add('show'); button.disabled = false; }

  form.addEventListener('submit', function(e){
    e.preventDefault();
    errorBox.classList.remove('show');
    var email = document.getElementById('p-email').value.trim();
    if(emailRequired && !/^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(email)){
      fail('Укажите корректный email — на него придёт доступ.');
      return;
    }
    var slotSelect = document.getElementById('p-slot');
    button.disabled = true;
    fetch('/api/pay', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        payment_id: PAYMENT_ID,
        name: document.getElementById('p-name').value,
        contact: document.getElementById('p-contact').value,
        email: email,
        slot_id: slotSelect ? slotSelect.value : ''
      })
    }).then(function(r){ return r.json(); }).then(function(data){
      if(!data.ok){ fail(data.error || 'Что-то пошло не так.'); return; }
      if(data.mode === 'live' || data.mode === 'paid'){ window.location = data.redirect; return; }
      // stub: имитируем подтверждение от ЮMoney через webhook
      fetch('/api/payment/webhook', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({payment_id: data.payment_id, signature: data.signature})
      }).then(function(r){ return r.json(); }).then(function(hook){
        if(!hook.ok){ fail(hook.error || 'Webhook не подтвердился.'); return; }
        window.location = data.redirect;
      }).catch(function(){ fail('Сеть недоступна. Попробуйте ещё раз.'); });
    }).catch(function(){ fail('Сеть недоступна. Попробуйте ещё раз.'); });
  });
})();
</script>
</body>
</html>"""

STUB_NOTE_HTML = (
    '<div class="stub-note">Это тестовая оплата: деньги не списываются. '
    "Нажмите «Подтвердить» для имитации успешного платежа.</div>"
)


def render_pay_page(store: BaseStore, payment: dict[str, Any]) -> str:
    """Собрать HTML страницы оплаты для конкретного платежа."""
    stub = config.payment_mode() != "live"
    email_required = payment.get("delivery", {}).get("type") == "email" or bool(
        payment.get("requires_slot")
    )

    slot_field = ""
    if payment.get("requires_slot"):
        options = "".join(
            f'<option value="{escape(s["id"])}">{escape(slots_module.format_slot(s))}</option>'
            for s in slots_module.available_slots(store)
        )
        if options:
            slot_field = (
                '<div class="field"><label for="p-slot">Время сессии <span class="req">*</span></label>'
                f'<select id="p-slot"><option value="">— выберите слот —</option>{options}</select></div>'
            )
        else:
            slot_field = (
                '<div class="field"><label>Время сессии</label>'
                '<input type="text" disabled value="Ольга свяжется с вами для выбора времени" /></div>'
            )

    page = PAY_PAGE_TEMPLATE
    replacements = {
        "__STUB_NOTE__": STUB_NOTE_HTML if stub else "",
        "__TITLE__": escape(str(payment.get("product_title") or "Оплата")),
        "__PRICE__": escape(products.format_price(int(payment.get("amount", 0)))),
        "__EMAIL_REQ__": '<span class="req">*</span>' if email_required else "",
        "__SLOT_FIELD__": slot_field,
        "__BUTTON__": "Подтвердить оплату (тест)" if stub else "Перейти к оплате",
        "__BACK_URL__": escape(config.app_url() + "/"),
        "__PAYMENT_ID__": escape(str(payment.get("id") or "")),
        "__MODE__": "stub" if stub else "live",
        "__EMAIL_REQUIRED_JS__": "true" if email_required else "false",
    }
    for token, value in replacements.items():
        page = page.replace(token, value)
    return page
