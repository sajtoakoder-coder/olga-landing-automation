"""CRUD товаров. Товары показываются на лендинге через GET /api/products
и управляются Ольгой через Telegram-бота."""
from __future__ import annotations

import re
from typing import Any

from core import config
from core.errors import NotFoundError, ValidationError
from storage.store import BaseStore

KIND = "products"

PRODUCT_KINDS = ("payment", "request")  # оплата / заявка
DELIVERY_TYPES = ("email", "link", "none")  # выдача после оплаты

KIND_LABELS = {"payment": "оплата", "request": "заявка"}
DELIVERY_LABELS = {"email": "email", "link": "ссылка", "none": "ничего"}

MAX_TITLE_LEN = 120
MAX_DESCRIPTION_LEN = 600
MAX_PRICE = 10_000_000


def _clean_str(value: Any, name: str, max_len: int, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{name}: ожидается строка")
    cleaned = " ".join(value.split())
    if required and not cleaned:
        raise ValidationError(f"{name}: не может быть пустым")
    if len(cleaned) > max_len:
        raise ValidationError(f"{name}: максимум {max_len} символов")
    return cleaned


def parse_price(value: Any) -> int:
    """Цена в рублях: int >= 0. Принимает "5000", "5 000", 5000."""
    if isinstance(value, bool):
        raise ValidationError("цена: ожидается число")
    if isinstance(value, int):
        price = value
    elif isinstance(value, float):
        if value != int(value):
            raise ValidationError("цена: только целые рубли")
        price = int(value)
    elif isinstance(value, str):
        digits = re.sub(r"[\s₽]", "", value)
        if not digits.isdigit():
            raise ValidationError("цена: ожидается число в рублях, например 5000")
        price = int(digits)
    else:
        raise ValidationError("цена: ожидается число")
    if price < 0:
        raise ValidationError("цена: не может быть отрицательной")
    if price > MAX_PRICE:
        raise ValidationError(f"цена: максимум {MAX_PRICE} ₽")
    return price


def _validate_enum(value: Any, allowed: tuple[str, ...], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{name}: допустимо {', '.join(allowed)}")
    return value


def add_product(
    store: BaseStore,
    *,
    title: str,
    description: str = "",
    price: Any = 0,
    kind: str = "payment",
    delivery: str = "none",
    delivery_content: str = "",
    requires_slot: bool = False,
    is_visible: bool = True,
    sort: int | None = None,
    product_id: str | None = None,
) -> dict[str, Any]:
    """Создать товар. product_id задаётся только сидом (детерминированные id
    делают повторный сид идемпотентным — гонка не плодит дубли)."""
    record = {
        "title": _clean_str(title, "название", MAX_TITLE_LEN, required=True),
        "description": _clean_str(description, "описание", MAX_DESCRIPTION_LEN, required=False),
        "price": parse_price(price),
        "kind": _validate_enum(kind, PRODUCT_KINDS, "тип"),
        "delivery": _validate_enum(delivery, DELIVERY_TYPES, "тип выдачи"),
        "delivery_content": _clean_str(
            delivery_content, "содержимое выдачи", 2000, required=False
        ),
        "requires_slot": bool(requires_slot),
        "is_visible": bool(is_visible),
        "sort": int(sort) if sort is not None else _next_sort(store),
    }
    if product_id:
        record["id"] = str(product_id)
    return store.put(KIND, record)


def _next_sort(store: BaseStore) -> int:
    existing = store.list(KIND)
    return max((int(p.get("sort", 0)) for p in existing), default=0) + 10


def list_products(store: BaseStore, include_hidden: bool = True) -> list[dict[str, Any]]:
    """Все товары, отсортированные по sort, затем по дате создания."""
    products = store.list(KIND)
    if not include_hidden:
        products = [p for p in products if p.get("is_visible", True)]
    return sorted(products, key=lambda p: (int(p.get("sort", 0)), str(p.get("created_at", ""))))


def visible_products(store: BaseStore) -> list[dict[str, Any]]:
    """Товары для лендинга."""
    return list_products(store, include_hidden=False)


def get_product(store: BaseStore, product_id: str) -> dict[str, Any]:
    product = store.get(KIND, product_id)
    if product is None:
        raise NotFoundError(f"товар {product_id} не найден")
    return product


def update_product(store: BaseStore, product_id: str, **fields: Any) -> dict[str, Any]:
    """Обновить отдельные поля товара."""
    product = get_product(store, product_id)
    if "title" in fields:
        product["title"] = _clean_str(fields.pop("title"), "название", MAX_TITLE_LEN, required=True)
    if "description" in fields:
        product["description"] = _clean_str(
            fields.pop("description"), "описание", MAX_DESCRIPTION_LEN, required=False
        )
    if "price" in fields:
        product["price"] = parse_price(fields.pop("price"))
    if "kind" in fields:
        product["kind"] = _validate_enum(fields.pop("kind"), PRODUCT_KINDS, "тип")
    if "delivery" in fields:
        product["delivery"] = _validate_enum(fields.pop("delivery"), DELIVERY_TYPES, "тип выдачи")
    if "delivery_content" in fields:
        product["delivery_content"] = _clean_str(
            fields.pop("delivery_content"), "содержимое выдачи", 2000, required=False
        )
    if "requires_slot" in fields:
        product["requires_slot"] = bool(fields.pop("requires_slot"))
    if "is_visible" in fields:
        product["is_visible"] = bool(fields.pop("is_visible"))
    if "sort" in fields:
        product["sort"] = int(fields.pop("sort"))
    if fields:
        raise ValidationError(f"неизвестные поля: {', '.join(sorted(fields))}")
    return store.put(KIND, product)


def delete_product(store: BaseStore, product_id: str) -> bool:
    return store.delete(KIND, product_id)


def set_visibility(store: BaseStore, product_id: str, visible: bool) -> dict[str, Any]:
    return update_product(store, product_id, is_visible=visible)


def payment_link(product: dict[str, Any]) -> str:
    """Ссылка «начать оплату» для товара (работает и в stub-, и в live-режиме)."""
    return f"{config.app_url()}/api/payment/create?product_id={product['id']}"


def to_public(product: dict[str, Any]) -> dict[str, Any]:
    """Представление товара для лендинга — без приватного содержимого выдачи."""
    return {
        "id": product["id"],
        "title": product.get("title", ""),
        "description": product.get("description", ""),
        "price": int(product.get("price", 0)),
        "kind": product.get("kind", "payment"),
        "requires_slot": bool(product.get("requires_slot", False)),
    }


def format_price(price: int) -> str:
    """5000 -> "5 000 ₽"."""
    return f"{price:,}".replace(",", " ") + " ₽"
