"""Создание 4 дефолтных товаров-заглушек (ТЗ, секция 1.2).

Запуск вручную:  python seed.py
Автоматически:   при первом GET /api/products и при первом /start бота.
"""
from __future__ import annotations

from typing import Any

from bot import products
from storage.store import BaseStore, get_store

DEFAULT_PRODUCTS: list[dict[str, Any]] = [
    {
        "title": "Разовая консультация",
        "description": "Точечно, под конкретный запрос. Онлайн, 60 минут.",
        "price": 5000,
        "kind": "payment",
        "delivery": "link",
        "delivery_content": "",  # пусто = ссылка Телемоста из настроек бота
        "requires_slot": True,
    },
    {
        "title": "Онлайн-тренинг (Ведьма/Гейша)",
        "description": "1,5 месяца групповой работы: инициации, практики, поддержка в чате.",
        "price": 15000,
        "kind": "payment",
        "delivery": "email",
        "delivery_content": (
            "Спасибо за оплату! Ссылка на закрытый Telegram-чат тренинга придёт "
            "вам от Ольги. (Ольга: задай ссылку в боте — Товары → ✏️ → содержимое выдачи.)"
        ),
        "requires_slot": False,
    },
    {
        "title": "Выездной ретрит",
        "description": "Предоплата за место на выездном ретрите. Маленькая группа, вилла, 3 дня.",
        "price": 30000,
        "kind": "payment",
        "delivery": "none",
        "delivery_content": "",
        "requires_slot": False,
    },
    {
        "title": "Индивидуальное сопровождение",
        "description": "1,5 месяца ежедневной поддержки — веду за ручку через изменения.",
        "price": 50000,
        "kind": "request",
        "delivery": "none",
        "delivery_content": "",
        "requires_slot": False,
    },
]

SEEDED_FLAG = "seeded"


def ensure_seed(store: BaseStore) -> bool:
    """Создать дефолтные товары один раз. True, если создали сейчас."""
    if store.get_setting(SEEDED_FLAG):
        return False
    if products.list_products(store):
        store.set_setting(SEEDED_FLAG, True)
        return False
    for item in DEFAULT_PRODUCTS:
        products.add_product(store, **item)
    store.set_setting(SEEDED_FLAG, True)
    return True


def main() -> None:
    store = get_store()
    created = ensure_seed(store)
    if created:
        print("Создано товаров:", len(DEFAULT_PRODUCTS))
    else:
        print("Сид уже выполнялся, товаров в базе:", len(products.list_products(store)))


if __name__ == "__main__":
    main()
