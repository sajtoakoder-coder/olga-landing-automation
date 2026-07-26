"""Тесты хранилища: FileStore, UpstashStore (мок REST), выбор бэкенда."""
from __future__ import annotations

import io
import json
from typing import Any
from unittest import mock

import pytest

from storage import store as store_module
from storage.store import (
    FileStore,
    StorageError,
    UpstashStore,
    get_store,
    new_id,
    now_iso,
)

KINDS = ["products", "bookings", "payments", "slots", "jobs"]


# ---------- helpers ----------


class FakeUpstash:
    """Эмуляция Upstash REST: принимает команды, хранит данные в памяти."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, str]] = {}
        self.commands: list[list[str]] = []

    def execute(self, args: list[str]) -> Any:
        self.commands.append(args)
        cmd = args[0].upper()
        if cmd == "HSET":
            _, key, field, value = args
            self.data.setdefault(key, {})[field] = value
            return 1
        if cmd == "HGET":
            _, key, field = args
            return self.data.get(key, {}).get(field)
        if cmd == "HGETALL":
            _, key = args
            flat: list[str] = []
            for field, value in self.data.get(key, {}).items():
                flat.extend([field, value])
            return flat
        if cmd == "HDEL":
            _, key, field = args
            if field in self.data.get(key, {}):
                del self.data[key][field]
                return 1
            return 0
        if cmd == "KEYS":
            _, pattern = args
            prefix = pattern.rstrip("*")
            return [k for k in self.data if k.startswith(prefix)]
        if cmd == "DEL":
            _, key = args
            return 1 if self.data.pop(key, None) is not None else 0
        raise AssertionError(f"неожиданная команда: {args}")


@pytest.fixture
def fake_upstash(monkeypatch: pytest.MonkeyPatch) -> FakeUpstash:
    fake = FakeUpstash()

    def fake_urlopen(request, timeout=0):
        assert request.get_header("Authorization") == "Bearer test-token"
        args = json.loads(request.data.decode("utf-8"))
        result = fake.execute(args)
        body = json.dumps({"result": result}).encode("utf-8")
        response = io.BytesIO(body)
        response.__enter__ = lambda *a: response  # type: ignore[attr-defined]
        response.__exit__ = lambda *a: None  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(store_module.urllib.request, "urlopen", fake_urlopen)
    return fake


@pytest.fixture
def upstash_store(fake_upstash: FakeUpstash) -> UpstashStore:
    return UpstashStore("https://kv.example.test", "test-token")


# ---------- обе реализации: общий контракт ----------


@pytest.fixture(params=["file", "upstash"])
def any_store(request, tmp_path):
    if request.param == "file":
        yield FileStore(str(tmp_path / "store.json"))
    else:
        fake = FakeUpstash()

        def fake_urlopen(req, timeout=0):
            args = json.loads(req.data.decode("utf-8"))
            body = json.dumps({"result": fake.execute(args)}).encode("utf-8")
            response = io.BytesIO(body)
            response.__enter__ = lambda *a: response  # type: ignore[attr-defined]
            response.__exit__ = lambda *a: None  # type: ignore[attr-defined]
            return response

        with mock.patch.object(store_module.urllib.request, "urlopen", fake_urlopen):
            yield UpstashStore("https://kv.example.test", "test-token")


@pytest.mark.parametrize("kind", KINDS)
def test_crud_roundtrip(any_store, kind):
    assert any_store.list(kind) == []

    created = any_store.put(kind, {"title": "Тест", "price": 5000})
    assert created["id"]
    assert created["created_at"]
    assert created["title"] == "Тест"

    fetched = any_store.get(kind, created["id"])
    assert fetched == created

    fetched["price"] = 9000
    any_store.put(kind, fetched)
    assert any_store.get(kind, created["id"])["price"] == 9000
    assert len(any_store.list(kind)) == 1

    assert any_store.delete(kind, created["id"]) is True
    assert any_store.get(kind, created["id"]) is None
    assert any_store.delete(kind, created["id"]) is False
    assert any_store.list(kind) == []


def test_put_preserves_explicit_id(any_store):
    record = any_store.put("products", {"id": "fixed-id", "title": "x"})
    assert record["id"] == "fixed-id"
    assert any_store.get("products", "fixed-id")["title"] == "x"


def test_list_sorted_by_created_at(any_store):
    any_store.put("products", {"id": "b", "created_at": "2026-01-02T00:00:00+00:00"})
    any_store.put("products", {"id": "a", "created_at": "2026-01-01T00:00:00+00:00"})
    any_store.put("products", {"id": "c", "created_at": "2026-01-03T00:00:00+00:00"})
    assert [r["id"] for r in any_store.list("products")] == ["a", "b", "c"]


def test_kinds_are_isolated(any_store):
    any_store.put("products", {"id": "p1"})
    any_store.put("slots", {"id": "s1"})
    assert [r["id"] for r in any_store.list("products")] == ["p1"]
    assert [r["id"] for r in any_store.list("slots")] == ["s1"]
    assert any_store.get("products", "s1") is None


def test_settings(any_store):
    assert any_store.get_setting("telemost_url") is None
    assert any_store.get_setting("telemost_url", "default") == "default"
    any_store.set_setting("telemost_url", "https://telemost.yandex.ru/x")
    assert any_store.get_setting("telemost_url") == "https://telemost.yandex.ru/x"
    any_store.set_setting("flags", {"seeded": True})
    assert any_store.get_setting("flags") == {"seeded": True}


def test_clear(any_store):
    any_store.put("products", {"id": "p1"})
    any_store.set_setting("k", "v")
    any_store.clear()
    assert any_store.list("products") == []
    assert any_store.get_setting("k") is None


def test_unicode_roundtrip(any_store):
    record = any_store.put("products", {"title": "Ведьма — «инициация» ₽"})
    assert any_store.get("products", record["id"])["title"] == "Ведьма — «инициация» ₽"


# ---------- FileStore-специфичное ----------


def test_file_persistence_across_instances(tmp_path):
    path = str(tmp_path / "store.json")
    FileStore(path).put("products", {"id": "p1", "title": "x"})
    assert FileStore(path).get("products", "p1")["title"] == "x"


def test_file_corrupt_file_treated_as_empty(tmp_path, caplog):
    path = tmp_path / "store.json"
    path.write_text("{broken json", encoding="utf-8")
    file_store = FileStore(str(path))
    assert file_store.list("products") == []
    file_store.put("products", {"id": "p1"})
    assert file_store.get("products", "p1") is not None


def test_file_creates_parent_directory(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "store.json")
    FileStore(path).put("products", {"id": "p1"})
    assert FileStore(path).get("products", "p1") is not None


# ---------- UpstashStore-специфичное ----------


def test_upstash_sends_expected_commands(upstash_store, fake_upstash):
    record = upstash_store.put("products", {"id": "p1", "title": "x"})
    assert record["id"] == "p1"
    assert fake_upstash.commands[0][:3] == ["HSET", "olga:products", "p1"]
    stored = json.loads(fake_upstash.data["olga:products"]["p1"])
    assert stored["title"] == "x"

    upstash_store.get("products", "p1")
    assert fake_upstash.commands[-1] == ["HGET", "olga:products", "p1"]

    upstash_store.list("products")
    assert fake_upstash.commands[-1] == ["HGETALL", "olga:products"]

    upstash_store.delete("products", "p1")
    assert fake_upstash.commands[-1] == ["HDEL", "olga:products", "p1"]


def test_upstash_skips_broken_records(upstash_store, fake_upstash):
    fake_upstash.data["olga:products"] = {
        "ok": json.dumps({"id": "ok", "created_at": "2026-01-01"}),
        "broken": "{not json",
    }
    assert [r["id"] for r in upstash_store.list("products")] == ["ok"]


def test_upstash_network_error_raises_storage_error(monkeypatch):
    def boom(request, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(store_module.urllib.request, "urlopen", boom)
    broken = UpstashStore("https://kv.example.test", "test-token")
    with pytest.raises(StorageError):
        broken.get("products", "x")


def test_upstash_api_error_raises_storage_error(monkeypatch):
    def error_response(request, timeout=0):
        body = json.dumps({"error": "WRONGPASS"}).encode("utf-8")
        response = io.BytesIO(body)
        response.__enter__ = lambda *a: response  # type: ignore[attr-defined]
        response.__exit__ = lambda *a: None  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(store_module.urllib.request, "urlopen", error_response)
    broken = UpstashStore("https://kv.example.test", "test-token")
    with pytest.raises(StorageError):
        broken.set_setting("k", "v")


# ---------- выбор бэкенда ----------


def test_get_store_defaults_to_file(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "s.json"))
    assert isinstance(get_store(), FileStore)


def test_get_store_uses_upstash_when_kv_env_present(monkeypatch):
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.test")
    monkeypatch.setenv("KV_REST_API_TOKEN", "token")
    selected = get_store()
    assert isinstance(selected, UpstashStore)
    assert selected.url == "https://kv.example.test"


def test_get_store_explicit_file_overrides_kv(monkeypatch, tmp_path):
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.test")
    monkeypatch.setenv("KV_REST_API_TOKEN", "token")
    monkeypatch.setenv("STORAGE_BACKEND", "file")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "s.json"))
    assert isinstance(get_store(), FileStore)


def test_get_store_upstash_without_credentials_fails(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "upstash")
    with pytest.raises(StorageError):
        get_store()


def test_get_store_vercel_defaults_to_tmp(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    selected = get_store()
    assert isinstance(selected, FileStore)
    assert selected.path == "/tmp/olga_store.json"


# ---------- утилиты ----------


def test_new_id_unique():
    ids = {new_id() for _ in range(200)}
    assert len(ids) == 200


def test_now_iso_is_utc():
    assert now_iso().endswith("+00:00")
