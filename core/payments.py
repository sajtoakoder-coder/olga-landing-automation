"""Платежи: создание, подтверждение (webhook), автовыдача после оплаты.

PAYMENT_MODE=stub — тестовая страница оплаты, подпись sha256(payment_id:secret).
PAYMENT_MODE=live — ЮMoney quickpay (receiver=YOOMONEY_SHOP_ID) + HTTP-уведомления
с подписью sha1 по протоколу ЮMoney. Переключение — только через env.
"""
from __future__ import annotations

import hashlib
import logging
import re
import urllib.parse
from typing import Any

from bot import notifications, products, slots as slots_module
from core import config
from core.errors import ConflictError, NotFoundError, ValidationError
from services import email as email_service
from services import telemost
from storage.store import BaseStore, now_iso

logger = logging.getLogger(__name__)

KIND = "payments"

STATUS_PENDING = "pending"
STATUS_PAID = "paid"

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def get_payment(store: BaseStore, payment_id: str) -> dict[str, Any]:
    payment = store.get(KIND, str(payment_id or ""))
    if payment is None:
        raise NotFoundError(f"платёж {payment_id} не найден")
    return payment


def create_payment(store: BaseStore, product_id: str) -> dict[str, Any]:
    """Создать платёж по товару. Снимок цены/выдачи фиксируется на момент создания."""
    product = products.get_product(store, str(product_id or ""))
    if product.get("kind") != "payment":
        raise ValidationError("этот формат оформляется через заявку, а не оплату")
    payment = {
        "product_id": product["id"],
        "product_title": product.get("title", ""),
        "amount": int(product.get("price", 0)),
        "status": STATUS_PENDING,
        "mode": config.payment_mode(),
        "paid_at": None,
        "customer": {"name": "", "contact": "", "email": ""},
        "slot_id": None,
        "slot_label": None,
        "requires_slot": bool(product.get("requires_slot", False)),
        "delivery": {
            "type": product.get("delivery", "none"),
            "content": product.get("delivery_content", ""),
        },
        "delivery_result": None,
    }
    return store.put(KIND, payment)


def pay_page_url(payment: dict[str, Any]) -> str:
    return f"{config.app_url()}/pay?id={payment['id']}"


def success_url(payment: dict[str, Any]) -> str:
    base = config.yoomoney_redirect_url()
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}payment_id={payment['id']}"


def attach_details(
    store: BaseStore,
    payment_id: str,
    *,
    name: str = "",
    contact: str = "",
    email: str = "",
    slot_id: str | None = None,
) -> dict[str, Any]:
    """Сохранить данные клиента (и выбранный слот) перед оплатой."""
    payment = get_payment(store, payment_id)
    if payment.get("status") == STATUS_PAID:
        return payment

    name = " ".join(str(name or "").split())[:200]
    contact = " ".join(str(contact or "").split())[:200]
    email = str(email or "").strip()[:200]
    if email and not EMAIL_RE.match(email):
        raise ValidationError("email выглядит некорректно")
    needs_email = payment.get("delivery", {}).get("type") == "email" or payment.get(
        "requires_slot"
    )
    if needs_email and not email:
        raise ValidationError("нужен email — на него придёт доступ/ссылка")

    payment["customer"] = {"name": name, "contact": contact, "email": email}

    if slot_id:
        slot = slots_module.get_slot(store, str(slot_id))
        if slot.get("status") != slots_module.STATUS_FREE:
            raise ConflictError("этот слот уже занят — выберите другой")
        payment["slot_id"] = slot["id"]
        payment["slot_label"] = slots_module.format_slot(slot)

    return store.put(KIND, payment)


# ---------- подписи и URL оплаты ----------


def stub_signature(payment_id: str) -> str:
    raw = f"{payment_id}:{config.yoomoney_secret()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def yoomoney_url(payment: dict[str, Any]) -> str:
    """Ссылка на оплату ЮMoney (quickpay) для live-режима."""
    params = {
        "receiver": config.yoomoney_receiver(),
        "quickpay-form": "shop",
        "targets": f"Оплата: {payment.get('product_title', '')}"[:150],
        "paymentType": "AC",
        "sum": payment.get("amount", 0),
        "label": payment["id"],
        "successURL": success_url(payment),
    }
    return "https://yoomoney.ru/quickpay/confirm.xml?" + urllib.parse.urlencode(params)


def verify_stub_webhook(store: BaseStore, payload: dict[str, Any]) -> str:
    """Проверка стаб-вебхука: {payment_id, signature}."""
    payment_id = str(payload.get("payment_id") or "")
    signature = str(payload.get("signature") or "")
    if not payment_id:
        raise ValidationError("нет payment_id")
    if signature != stub_signature(payment_id):
        raise ValidationError("неверная подпись")
    return payment_id


def verify_yoomoney_webhook(store: BaseStore, form: dict[str, str]) -> str:
    """Проверка HTTP-уведомления ЮMoney (sha1-подпись по протоколу)."""
    fields = [
        str(form.get("notification_type") or ""),
        str(form.get("operation_id") or ""),
        str(form.get("amount") or ""),
        str(form.get("currency") or ""),
        str(form.get("datetime") or ""),
        str(form.get("sender") or ""),
        str(form.get("codepro") or ""),
        config.yoomoney_secret(),
        str(form.get("label") or ""),
    ]
    digest = hashlib.sha1("&".join(fields).encode("utf-8")).hexdigest()
    if digest != str(form.get("sha1_hash") or "").lower():
        raise ValidationError("неверная подпись ЮMoney")
    payment_id = str(form.get("label") or "")
    if not payment_id:
        raise ValidationError("пустой label (payment_id)")
    return payment_id


def handle_webhook(
    store: BaseStore, payload: dict[str, Any], *, form_mode: bool
) -> tuple[dict[str, Any], bool]:
    """Общая точка входа вебхука. form_mode=True — формат ЮMoney (form-encoded)."""
    if form_mode:
        payment_id = verify_yoomoney_webhook(store, payload)
    else:
        payment_id = verify_stub_webhook(store, payload)
    return confirm_payment(store, payment_id)


# ---------- подтверждение и автовыдача ----------


def _resolve_link(store: BaseStore, payment: dict[str, Any]) -> str:
    """Ссылка для выдачи типа "link": содержимое товара, иначе дефолт Телемоста."""
    content = str(payment.get("delivery", {}).get("content") or "").strip()
    if content:
        return content
    if payment.get("requires_slot"):
        # реальную ссылку пришлёт телемост-джоба перед сессией
        return ""
    return (
        str(store.get_setting("telemost_url") or "").strip()
        or config.default_telemost_url()
    )


def confirm_payment(store: BaseStore, payment_id: str) -> tuple[dict[str, Any], bool]:
    """Подтвердить оплату. Идемпотентно: повторный вебхук не дублирует выдачу.

    Возвращает (payment, already_paid).
    """
    payment = get_payment(store, payment_id)
    if payment.get("status") == STATUS_PAID:
        logger.info("Повторный webhook по оплаченному платежу %s — игнор", payment_id)
        return payment, True

    payment["status"] = STATUS_PAID
    payment["paid_at"] = now_iso()
    store.put(KIND, payment)  # фиксируем оплату до сайд-эффектов

    customer = payment.get("customer") or {}
    email = str(customer.get("email") or "").strip()

    # --- бронирование слота и телемост ---
    slot = None
    if payment.get("slot_id"):
        try:
            slot = slots_module.book_slot(
                store, payment["slot_id"], customer=customer, payment_id=payment["id"]
            )
            payment["slot_label"] = slots_module.format_slot(slot)
        except ConflictError:
            existing = store.get("slots", payment["slot_id"]) or {}
            if existing.get("payment_id") == payment["id"]:
                slot = existing  # уже забронирован этим же платежом (повтор)
            else:
                notifications.notify_error(
                    "слот",
                    f"оплата {payment['id']} ({payment.get('product_title')}): "
                    f"слот {payment.get('slot_label')} уже занят другим клиентом — свяжись с клиентом",
                )
        except NotFoundError:
            notifications.notify_error(
                "слот",
                f"оплата {payment['id']}: выбранный слот удалён — договорись с клиентом о времени",
            )
    if slot is not None:
        telemost.schedule_telemost_delivery(store, payment=payment, slot=slot)

    # --- автовыдача ---
    delivery = payment.get("delivery") or {}
    delivery_type = delivery.get("type", "none")
    result: dict[str, Any] = {"type": delivery_type}

    if delivery_type == "email":
        content = str(delivery.get("content") or "").strip()
        if not content:
            content = "Спасибо за оплату! Ольга свяжется с вами в ближайшее время."
        if email:
            sent = email_service.send_email(email, "Ваш доступ — Ольга Андреева", content)
            result["email"] = {"to": email, **sent}
        else:
            result["email"] = {"ok": False, "detail": "клиент не указал email"}
            notifications.notify_error(
                "выдача", f"оплата {payment['id']}: тип выдачи email, но клиент без email"
            )
    elif delivery_type == "link":
        link = _resolve_link(store, payment)
        result["link"] = link
        if link and email:
            # дублируем ссылку на почту, если она есть (по ТЗ)
            sent = email_service.send_email(
                email,
                "Ваша ссылка — Ольга Андреева",
                f"Спасибо за оплату!\n\nВаша ссылка: {link}",
            )
            result["email"] = {"to": email, **sent}

    payment["delivery_result"] = result
    payment = store.put(KIND, payment)

    notifications.notify_payment(payment)
    return payment, False


def public_status(store: BaseStore, payment: dict[str, Any]) -> dict[str, Any]:
    """Статус платежа для страницы success (без приватных данных)."""
    paid = payment.get("status") == STATUS_PAID
    delivery = payment.get("delivery") or {}
    delivery_type = delivery.get("type", "none")
    info: dict[str, Any] = {
        "id": payment["id"],
        "status": payment.get("status", STATUS_PENDING),
        "product_title": payment.get("product_title", ""),
        "amount": int(payment.get("amount", 0)),
        "delivery_type": delivery_type,
        "link": "",
        "message": "",
    }
    if not paid:
        info["message"] = "Платёж ещё обрабатывается. Обновите страницу через минуту."
        return info

    email = str((payment.get("customer") or {}).get("email") or "").strip()
    if delivery_type == "link":
        link = _resolve_link(store, payment)
        if link:
            info["link"] = link
            info["message"] = "Ваша ссылка для подключения готова."
        else:
            info["message"] = (
                "Ссылка для подключения придёт на ваш email незадолго до сессии."
                if email
                else "Ольга свяжется с вами, чтобы передать ссылку."
            )
    elif delivery_type == "email":
        info["message"] = (
            f"Мы отправили доступ на {email}." if email else "Ольга отправит вам доступ лично."
        )
    else:
        info["message"] = "Оплата прошла. Мы свяжемся с вами в ближайшее время."
    if payment.get("slot_label"):
        info["slot_label"] = payment["slot_label"]
    return info
