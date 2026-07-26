"""Тесты CRUD товаров (bot/products.py)."""
from __future__ import annotations

import pytest

from bot import products
from core.errors import NotFoundError, ValidationError


def make(store, **kwargs):
    defaults = dict(
        title="Разовая консультация",
        description="60 минут онлайн",
        price=5000,
        kind="payment",
        delivery="link",
        delivery_content="https://telemost.yandex.ru/j/xxx",
    )
    defaults.update(kwargs)
    return products.add_product(store, **defaults)


# ---------- добавление ----------


def test_add_product_full(store):
    product = make(store)
    assert product["id"]
    assert product["title"] == "Разовая консультация"
    assert product["price"] == 5000
    assert product["kind"] == "payment"
    assert product["delivery"] == "link"
    assert product["is_visible"] is True
    assert product["requires_slot"] is False


def test_add_product_price_from_string(store):
    assert make(store, price="15 000")["price"] == 15000
    assert make(store, title="B", price="5000₽")["price"] == 5000


def test_add_product_strips_whitespace(store):
    product = make(store, title="  Тренинг   Ведьма  ", description=" a\n b ")
    assert product["title"] == "Тренинг Ведьма"
    assert product["description"] == "a b"


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", ""),
        ("title", "   "),
        ("title", "x" * 200),
        ("price", -1),
        ("price", "дорого"),
        ("price", 99_000_000),
        ("kind", "subscription"),
        ("delivery", "sms"),
        ("description", "x" * 700),
    ],
)
def test_add_product_validation(store, field, value):
    with pytest.raises(ValidationError):
        make(store, **{field: value})


# ---------- список / видимость ----------


def test_list_sorted_by_sort_field(store):
    first = make(store, title="A")
    second = make(store, title="B")
    third = make(store, title="C", sort=1)
    titles = [p["title"] for p in products.list_products(store)]
    assert titles == ["C", "A", "B"]
    assert first["sort"] < second["sort"]
    assert third["sort"] == 1


def test_hide_and_show_product(store):
    product = make(store)
    assert len(products.visible_products(store)) == 1

    products.set_visibility(store, product["id"], False)
    assert products.visible_products(store) == []
    # товар остался в полном списке (для бота)
    assert len(products.list_products(store)) == 1
    assert products.get_product(store, product["id"])["is_visible"] is False

    products.set_visibility(store, product["id"], True)
    assert len(products.visible_products(store)) == 1


# ---------- обновление ----------


def test_update_product_fields(store):
    product = make(store)
    updated = products.update_product(
        store,
        product["id"],
        title="Новое имя",
        price="9 000",
        description="Другое описание",
        delivery="email",
        delivery_content="https://t.me/+secret",
    )
    assert updated["title"] == "Новое имя"
    assert updated["price"] == 9000
    assert updated["delivery"] == "email"
    stored = products.get_product(store, product["id"])
    assert stored == updated


def test_update_product_invalid_field(store):
    product = make(store)
    with pytest.raises(ValidationError):
        products.update_product(store, product["id"], nonexistent="x")


def test_update_product_invalid_value(store):
    product = make(store)
    with pytest.raises(ValidationError):
        products.update_product(store, product["id"], price="abc")
    with pytest.raises(ValidationError):
        products.update_product(store, product["id"], title="")


def test_update_missing_product(store):
    with pytest.raises(NotFoundError):
        products.update_product(store, "nope", title="x")


# ---------- удаление ----------


def test_delete_product(store):
    product = make(store)
    assert products.delete_product(store, product["id"]) is True
    assert products.delete_product(store, product["id"]) is False
    with pytest.raises(NotFoundError):
        products.get_product(store, product["id"])


# ---------- ссылки и публичное представление ----------


def test_payment_link_uses_app_url(store, monkeypatch):
    monkeypatch.setenv("APP_URL", "https://olga.vercel.app")
    product = make(store)
    assert (
        products.payment_link(product)
        == f"https://olga.vercel.app/api/payment/create?product_id={product['id']}"
    )


def test_to_public_hides_delivery_content(store):
    product = make(store, delivery_content="https://t.me/+secret-chat")
    public = products.to_public(product)
    # приватно только содержимое выдачи; тип выдачи нужен фронту (email-обязательность)
    assert "delivery_content" not in public
    assert "secret-chat" not in str(public)
    assert public["delivery"] == "link"
    assert public["id"] == product["id"]
    assert public["price"] == 5000
    assert public["requires_slot"] is False
    assert public["image"] == ""


def test_product_image_field(store):
    product = make(store, image="/photos/01_portrait_red_glove.jpeg")
    assert product["image"] == "/photos/01_portrait_red_glove.jpeg"
    assert products.to_public(product)["image"] == "/photos/01_portrait_red_glove.jpeg"

    updated = products.update_product(store, product["id"], image="https://cdn.example/x.jpg")
    assert updated["image"] == "https://cdn.example/x.jpg"
    # убрать фото
    assert products.update_product(store, product["id"], image="")["image"] == ""


def test_product_image_validation(store):
    with pytest.raises(ValidationError):
        make(store, image="ftp://bad")
    with pytest.raises(ValidationError):
        make(store, image="просто текст")
    with pytest.raises(ValidationError):
        make(store, image="https://x/" + "a" * 500)


def test_format_price():
    assert products.format_price(5000) == "5 000 ₽"
    assert products.format_price(150) == "150 ₽"
    assert products.format_price(1234567) == "1 234 567 ₽"
