"""Создание 4 дефолтных товаров-заглушек (ТЗ, секция 1.2).

Запуск вручную:  python seed.py
Автоматически:   при первом GET /api/products и при первом /start бота.

Сид идемпотентен: у дефолтных товаров фиксированные id, поэтому даже два
одновременных сида на пустой базе (гонка serverless-инстансов) дают ровно
4 записи. ensure_seed также разово чинит базу, если гонка старой версии
успела насоздавать дублей.
"""
from __future__ import annotations

from typing import Any

from bot import products
from storage.store import BaseStore, get_store

DEFAULT_PRODUCTS: list[dict[str, Any]] = [
    {
        "product_id": "seed-consultation",
        "title": "Разовая консультация",
        "description": "Точечно, под конкретный запрос. Онлайн, 60 минут.",
        "price": 5000,
        "kind": "payment",
        "delivery": "link",
        "delivery_content": "",  # пусто = ссылка Телемоста из настроек бота
        "requires_slot": True,
    },
    {
        "product_id": "seed-training",
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
        "product_id": "seed-retreat",
        "title": "Выездной ретрит",
        "description": "Предоплата за место на выездном ретрите. Маленькая группа, вилла, 3 дня.",
        "price": 30000,
        "kind": "payment",
        "delivery": "none",
        "delivery_content": "",
        "requires_slot": False,
    },
    {
        "product_id": "seed-support",
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
SEED_V2_FLAG = "seed_v2"  # выставлен = сид с фиксированными id + дедуп выполнены


def _seed_titles() -> set[str]:
    return {item["title"] for item in DEFAULT_PRODUCTS}


def _dedupe_seed_products(store: BaseStore, existing: list[dict[str, Any]]) -> int:
    """Схлопнуть дубли сид-товаров (по названию), оставив самый ранний.

    Товары Ольги (названия вне сид-списка) не трогаем.
    """
    titles = _seed_titles()
    seen: set[str] = set()
    removed = 0
    ordered = sorted(existing, key=lambda p: (str(p.get("created_at", "")), str(p.get("id", ""))))
    for product in ordered:
        title = product.get("title", "")
        if title not in titles:
            continue
        if title in seen:
            products.delete_product(store, product["id"])
            removed += 1
        else:
            seen.add(title)
    return removed


def ensure_seed(store: BaseStore) -> bool:
    """Создать дефолтные товары один раз (и разово починить дубли).

    True, если товары созданы этим вызовом.
    """
    if store.get_setting(SEED_V2_FLAG):
        return False

    existing = products.list_products(store)
    created = False
    if not existing and not store.get_setting(SEEDED_FLAG):
        for item in DEFAULT_PRODUCTS:
            products.add_product(store, **item)
        created = True
    elif existing:
        _dedupe_seed_products(store, existing)

    store.set_setting(SEEDED_FLAG, True)
    store.set_setting(SEED_V2_FLAG, True)
    return created


def main() -> None:
    store = get_store()
    created = ensure_seed(store)
    if created:
        print("Создано товаров:", len(DEFAULT_PRODUCTS))
    else:
        print("Сид уже выполнялся, товаров в базе:", len(products.list_products(store)))


if __name__ == "__main__":
    main()
