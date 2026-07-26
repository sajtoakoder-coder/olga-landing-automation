"""Тесты SupabaseStore: контракт хранилища на эмуляторе PostgREST + выбор бэкенда."""
from __future__ import annotations

import io
import json
import urllib.parse
from typing import Any

import pytest

from storage import store as store_module
from storage.store import FileStore, StorageError, SupabaseStore, UpstashStore, get_store

KINDS = ["products", "bookings", "payments", "slots", "jobs"]


class FakePostgrest:
    """Минимальная эмуляция PostgREST для запросов SupabaseStore."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []

    @staticmethod
    def _param(query: dict[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        if not values:
            return None
        value = values[0]
        if value.startswith("eq."):
            return urllib.parse.unquote(value[3:])
        return value

    def execute(self, method: str, url: str, body: Any, prefer: str) -> tuple[int, Any]:
        parsed = urllib.parse.urlsplit(url)
        assert parsed.path.endswith("/rest/v1/olga_records")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        self.requests.append((method, parsed.query))
        kind = self._param(query, "kind")
        item_id = self._param(query, "id")

        if method == "GET":
            result = [
                {"data": row["data"]}
                for (k, i), row in self.rows.items()
                if k == kind and (item_id is None or i == item_id)
            ]
            return 200, result
        if method == "POST":
            assert "resolution=merge-duplicates" in prefer
            for row in body:
                self.rows[(row["kind"], row["id"])] = {"data": row["data"]}
            return 201, None
        if method == "DELETE":
            if query.get("kind") == ["not.is.null"]:
                deleted = list(self.rows.values())
                self.rows.clear()
            else:
                keys = [
                    key
                    for key in self.rows
                    if key[0] == kind and (item_id is None or key[1] == item_id)
                ]
                deleted = [self.rows.pop(key) for key in keys]
            if "return=representation" in prefer:
                return 200, [{"data": row["data"]} for row in deleted]
            return 204, None
        raise AssertionError(f"неожиданный метод {method}")


def _wire(monkeypatch: pytest.MonkeyPatch, fake: FakePostgrest) -> None:
    def fake_urlopen(request, timeout=0):
        assert request.get_header("Apikey") == "service-key"
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        status, result = fake.execute(
            request.get_method(), request.full_url, body, request.get_header("Prefer") or ""
        )
        payload = b"" if result is None else json.dumps(result).encode("utf-8")
        response = io.BytesIO(payload)
        response.__enter__ = lambda *a: response  # type: ignore[attr-defined]
        response.__exit__ = lambda *a: None  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(store_module.urllib.request, "urlopen", fake_urlopen)


@pytest.fixture
def fake_pg(monkeypatch):
    fake = FakePostgrest()
    _wire(monkeypatch, fake)
    return fake


@pytest.fixture
def supabase_store(fake_pg) -> SupabaseStore:
    return SupabaseStore("https://test.supabase.co", "service-key")


# ---------- контракт хранилища ----------


@pytest.mark.parametrize("kind", KINDS)
def test_crud_roundtrip(supabase_store, kind):
    assert supabase_store.list(kind) == []

    created = supabase_store.put(kind, {"title": "Тест", "price": 5000})
    assert created["id"] and created["created_at"]

    fetched = supabase_store.get(kind, created["id"])
    assert fetched == created

    fetched["price"] = 9000
    supabase_store.put(kind, fetched)
    assert supabase_store.get(kind, created["id"])["price"] == 9000
    assert len(supabase_store.list(kind)) == 1

    assert supabase_store.delete(kind, created["id"]) is True
    assert supabase_store.get(kind, created["id"]) is None
    assert supabase_store.delete(kind, created["id"]) is False


def test_list_sorted_and_isolated(supabase_store):
    supabase_store.put("products", {"id": "b", "created_at": "2026-01-02T00:00:00+00:00"})
    supabase_store.put("products", {"id": "a", "created_at": "2026-01-01T00:00:00+00:00"})
    supabase_store.put("slots", {"id": "s1"})
    assert [r["id"] for r in supabase_store.list("products")] == ["a", "b"]
    assert [r["id"] for r in supabase_store.list("slots")] == ["s1"]
    assert supabase_store.get("products", "s1") is None


def test_settings_roundtrip(supabase_store):
    assert supabase_store.get_setting("telemost_url") is None
    assert supabase_store.get_setting("x", "def") == "def"
    supabase_store.set_setting("telemost_url", "https://telemost.yandex.ru/j/1")
    assert supabase_store.get_setting("telemost_url") == "https://telemost.yandex.ru/j/1"
    supabase_store.set_setting("flags", {"seeded": True})
    assert supabase_store.get_setting("flags") == {"seeded": True}


def test_clear(supabase_store):
    supabase_store.put("products", {"id": "p1"})
    supabase_store.set_setting("k", "v")
    supabase_store.clear()
    assert supabase_store.list("products") == []
    assert supabase_store.get_setting("k") is None


def test_unicode_roundtrip(supabase_store):
    record = supabase_store.put("products", {"title": "Ведьма — «инициация» ₽"})
    assert supabase_store.get("products", record["id"])["title"] == "Ведьма — «инициация» ₽"


# ---------- ошибки ----------


def test_http_error_raises_storage_error(monkeypatch):
    import urllib.error

    def boom(request, timeout=0):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"message":"bad key"}')
        )

    monkeypatch.setattr(store_module.urllib.request, "urlopen", boom)
    broken = SupabaseStore("https://test.supabase.co", "bad")
    with pytest.raises(StorageError) as err:
        broken.get("products", "x")
    assert "401" in str(err.value)


def test_network_error_raises_storage_error(monkeypatch):
    def boom(request, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(store_module.urllib.request, "urlopen", boom)
    broken = SupabaseStore("https://test.supabase.co", "key")
    with pytest.raises(StorageError):
        broken.list("products")


# ---------- выбор бэкенда ----------


def test_get_store_supabase_auto(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "key")
    assert isinstance(get_store(), SupabaseStore)


def test_get_store_supabase_beats_upstash(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "key")
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.test")
    monkeypatch.setenv("KV_REST_API_TOKEN", "token")
    assert isinstance(get_store(), SupabaseStore)


def test_get_store_explicit_backend_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "key")
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.test")
    monkeypatch.setenv("KV_REST_API_TOKEN", "token")
    monkeypatch.setenv("STORAGE_BACKEND", "upstash")
    assert isinstance(get_store(), UpstashStore)
    monkeypatch.setenv("STORAGE_BACKEND", "file")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "s.json"))
    assert isinstance(get_store(), FileStore)


def test_get_store_supabase_without_creds_fails(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    with pytest.raises(StorageError):
        get_store()


# ---------- интеграция: полный флоу поверх Supabase-эмулятора ----------


def test_full_booking_and_payment_flow_on_supabase(supabase_store, monkeypatch, admin_notify):
    import json as json_module

    from core.app import dispatch

    body = json_module.dumps(
        {"name": "Анна", "contact": "@anna", "format": "Тест", "message": "x"}
    ).encode("utf-8")
    status, _, payload = dispatch(
        supabase_store, "POST", "/api/booking", {}, body, "application/json"
    )
    assert status == 200
    assert len(supabase_store.list("bookings")) == 1

    status, _, payload = dispatch(supabase_store, "GET", "/api/products", {}, b"", "")
    data = json_module.loads(payload)
    assert status == 200 and len(data["products"]) == 4  # автосид сработал в Postgres

    product_id = data["products"][0]["id"]
    status, _, payload = dispatch(
        supabase_store,
        "POST",
        "/api/payment/create",
        {},
        json_module.dumps({"product_id": product_id}).encode(),
        "application/json",
    )
    assert status == 200
    payment_id = json_module.loads(payload)["payment_id"]

    from core import payments

    signature = payments.stub_signature(payment_id)
    status, _, payload = dispatch(
        supabase_store,
        "POST",
        "/api/payment/webhook",
        {},
        json_module.dumps({"payment_id": payment_id, "signature": signature}).encode(),
        "application/json",
    )
    assert status == 200
    assert json_module.loads(payload)["status"] == "paid"
    assert supabase_store.get("payments", payment_id)["status"] == "paid"
