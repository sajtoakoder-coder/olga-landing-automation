"""Тесты сида: идемпотентность, гонка, миграция-дедупликация."""
from __future__ import annotations

import seed
from bot import products


def test_seed_creates_four_with_fixed_ids(store):
    assert seed.ensure_seed(store) is True
    items = products.list_products(store)
    assert len(items) == 4
    assert {p["id"] for p in items} == {
        "seed-consultation",
        "seed-training",
        "seed-retreat",
        "seed-support",
    }
    # у всех сид-товаров есть фото для карточки
    by_id = {p["id"]: p for p in items}
    for pid in ("seed-consultation", "seed-training", "seed-retreat", "seed-support"):
        assert by_id[pid]["image"].startswith("/photos/"), pid
    # повторный вызов ничего не создаёт
    assert seed.ensure_seed(store) is False
    assert len(products.list_products(store)) == 4


def test_seed_enrich_migration_adds_images_to_legacy(store):
    """База со старым сидом (без фото): миграция досыпает фото и описания."""
    for item in seed.DEFAULT_PRODUCTS:
        legacy = {k: v for k, v in item.items() if k not in ("image",)}
        legacy["description"] = "Коротко."
        products.add_product(store, **legacy)
    store.set_setting(seed.SEEDED_FLAG, True)
    store.set_setting(seed.SEED_V2_FLAG, True)

    assert seed.ensure_seed(store) is False
    by_title = {p["title"]: p for p in products.list_products(store)}
    assert by_title["Разовая консультация"]["image"].startswith("/photos/")
    assert len(by_title["Разовая консультация"]["description"]) > 80
    # заявочный товар тоже получает фото
    assert by_title["Индивидуальное сопровождение"]["image"].startswith("/photos/")
    assert store.get_setting(seed.SEED_V3_FLAG) is True
    assert store.get_setting(seed.SEED_V4_FLAG) is True

    # свои товары Ольги (не из сида) миграция не трогает
    custom = products.add_product(store, title="Авторский курс", price=1000)
    assert products.get_product(store, custom["id"])["image"] == ""


def test_seed_race_with_fixed_ids_is_idempotent(store):
    """Два «одновременных» сида на пустой базе → всё равно 4 товара."""
    for item in seed.DEFAULT_PRODUCTS:
        products.add_product(store, **item)
    for item in seed.DEFAULT_PRODUCTS:
        products.add_product(store, **item)  # второй гонщик перезаписывает те же id
    assert len(products.list_products(store)) == 4


def test_seed_migration_collapses_old_duplicates(store):
    """База после гонки старой версии: дубли с рандомными id + флаг seeded."""
    for _ in range(2):
        for item in seed.DEFAULT_PRODUCTS:
            legacy = {k: v for k, v in item.items() if k != "product_id"}
            products.add_product(store, **legacy)
    store.set_setting(seed.SEEDED_FLAG, True)
    assert len(products.list_products(store)) == 8

    assert seed.ensure_seed(store) is False
    titles = [p["title"] for p in products.list_products(store)]
    assert len(titles) == 4
    assert len(set(titles)) == 4
    assert store.get_setting(seed.SEED_V2_FLAG) is True
    # дальше дедуп не запускается (флаг), даже если Ольга назовёт свой товар так же
    products.add_product(store, title="Выездной ретрит", price=999)
    seed.ensure_seed(store)
    assert len(products.list_products(store)) == 5


def test_seed_migration_keeps_olgas_products(store):
    products.add_product(store, title="Авторский курс", price=7000)
    store.set_setting(seed.SEEDED_FLAG, True)
    seed.ensure_seed(store)
    titles = [p["title"] for p in products.list_products(store)]
    assert titles == ["Авторский курс"]


def test_seed_respects_deliberate_wipe(store):
    """Ольга удалила все товары — сид не пересоздаёт их заново."""
    seed.ensure_seed(store)
    for product in products.list_products(store):
        products.delete_product(store, product["id"])
    assert seed.ensure_seed(store) is False
    assert products.list_products(store) == []
