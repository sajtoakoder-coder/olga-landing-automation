"""Живая проверка Supabase-хранилища на реальной базе.

Запуск:  python scripts/check_supabase.py
Креды берутся из env или из файла .env в корне (SUPABASE_URL, SUPABASE_KEY).

ВНИМАНИЕ: скрипт предполагает ТЕСТОВУЮ базу — в конце вычищает свои
тестовые записи (товары/заявки/платежи, созданные проверкой).
"""
from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def load_dotenv() -> None:
    path = os.path.join(_ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


def main() -> int:
    load_dotenv()
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        print("FAIL: задайте SUPABASE_URL и SUPABASE_KEY (env или .env)")
        return 1

    from storage.store import SupabaseStore

    store = SupabaseStore(url, key)
    passed: list[str] = []
    created_ids: dict[str, list[str]] = {"products": [], "bookings": [], "payments": []}

    def ok(name: str) -> None:
        passed.append(name)
        print(f"  PASS  {name}")

    print(f"Проверка Supabase: {url}")

    # 1. настройки
    store.set_setting("_healthcheck", {"ping": "pong"})
    assert store.get_setting("_healthcheck") == {"ping": "pong"}, "настройки не сохраняются"
    ok("настройки: запись/чтение")

    # 2. CRUD записи
    record = store.put("products", {"title": "__test__ Проверочный", "price": 1234})
    created_ids["products"].append(record["id"])
    assert store.get("products", record["id"])["price"] == 1234
    record["price"] = 4321
    store.put("products", record)
    assert store.get("products", record["id"])["price"] == 4321, "upsert не обновил запись"
    ok("CRUD: создание/чтение/обновление")

    # 3. кириллица/юникод
    assert store.get("products", record["id"])["title"] == "__test__ Проверочный"
    ok("юникод: кириллица без искажений")

    # 4. полный флоу через API-диспетчер (заявка + оплата) поверх реальной БД
    from core import payments
    from core.app import dispatch

    booking_body = json.dumps(
        {"name": "Тест Supabase", "contact": "@sb_test", "format": "Проверка", "message": "x"}
    ).encode("utf-8")
    status, _, payload = dispatch(store, "POST", "/api/booking", {}, booking_body, "application/json")
    assert status == 200, f"booking: {payload!r}"
    created_ids["bookings"].append(json.loads(payload)["id"])
    ok("API: заявка сохраняется в Supabase")

    status, _, payload = dispatch(
        store, "POST", "/api/payment/create", {},
        json.dumps({"product_id": record["id"]}).encode(), "application/json",
    )
    assert status == 200, f"create: {payload!r}"
    payment_id = json.loads(payload)["payment_id"]
    created_ids["payments"].append(payment_id)

    signature = payments.stub_signature(payment_id)
    status, _, payload = dispatch(
        store, "POST", "/api/payment/webhook", {},
        json.dumps({"payment_id": payment_id, "signature": signature}).encode(), "application/json",
    )
    assert status == 200 and json.loads(payload)["status"] == "paid", f"webhook: {payload!r}"
    assert store.get("payments", payment_id)["status"] == "paid"
    ok("API: оплата (create → webhook → paid) в Supabase")

    # повторный webhook — идемпотентность
    status, _, payload = dispatch(
        store, "POST", "/api/payment/webhook", {},
        json.dumps({"payment_id": payment_id, "signature": signature}).encode(), "application/json",
    )
    assert json.loads(payload)["duplicate"] is True
    ok("идемпотентность webhook")

    # 5. уборка тестовых записей
    for kind, ids in created_ids.items():
        for item_id in ids:
            store.delete(kind, item_id)
    store.delete("_settings", "_healthcheck")
    ok("уборка тестовых записей")

    print(f"\nИТОГ: {len(passed)}/{len(passed)} проверок пройдено — Supabase готов к работе.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
