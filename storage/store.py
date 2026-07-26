"""Абстракция хранилища.

Бэкенды:
  * FileStore     — один JSON-файл (локальная разработка; на Vercel — /tmp,
                    данные не переживают cold start — только для демо без KV).
  * UpstashStore  — Vercel KV / Upstash Redis через REST API (постоянное
                    хранение в serverless). Включается автоматически, если
                    заданы KV_REST_API_URL/KV_REST_API_TOKEN (или UPSTASH_*).
  * SupabaseStore — Postgres на Supabase через PostgREST: одна таблица
                    olga_records (kind, id, data jsonb). Включается
                    автоматически при SUPABASE_URL/SUPABASE_KEY и имеет
                    приоритет над Upstash.

Явный выбор: STORAGE_BACKEND=file|upstash|supabase.

Данные — записи (dict) в неймспейсах (kind): products, bookings, payments,
slots, jobs (отложенные отправки Телемоста), fsm (состояние диалогов бота).
Отдельно — settings (ключ-значение).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

SETTINGS_KIND = "_settings"


class StorageError(Exception):
    """Ошибка обращения к хранилищу."""


def new_id() -> str:
    """Короткий уникальный id записи."""
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    """Текущее время в UTC, ISO-8601."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sort_key(record: dict[str, Any]) -> tuple[str, str]:
    return (str(record.get("created_at", "")), str(record.get("id", "")))


class BaseStore(ABC):
    """Интерфейс хранилища записей."""

    @abstractmethod
    def list(self, kind: str) -> list[dict[str, Any]]:
        """Все записи вида kind, отсортированные по created_at."""

    @abstractmethod
    def get(self, kind: str, item_id: str) -> dict[str, Any] | None:
        """Запись по id или None."""

    @abstractmethod
    def put(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        """Upsert записи целиком. Проставляет id и created_at, если их нет."""

    @abstractmethod
    def delete(self, kind: str, item_id: str) -> bool:
        """Удалить запись. True, если запись существовала."""

    @abstractmethod
    def get_setting(self, key: str, default: Any = None) -> Any:
        """Значение настройки key или default."""

    @abstractmethod
    def set_setting(self, key: str, value: Any) -> None:
        """Установить настройку key."""

    @abstractmethod
    def clear(self) -> None:
        """Полностью очистить хранилище (тесты/обслуживание)."""

    def _prepare(self, record: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(record)
        if not prepared.get("id"):
            prepared["id"] = new_id()
        prepared["id"] = str(prepared["id"])
        if not prepared.get("created_at"):
            prepared["created_at"] = now_iso()
        return prepared


class FileStore(BaseStore):
    """Хранилище в одном JSON-файле с атомарной записью."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()

    # -- низкий уровень --

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("store root is not a dict")
            return data
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.error("FileStore: не удалось прочитать %s: %s", self.path, exc)
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.path}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, self.path)
        except OSError as exc:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise StorageError(f"не удалось записать {self.path}: {exc}") from exc

    # -- API --

    def list(self, kind: str) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._load().get(kind, {}).values())
        return sorted(records, key=_sort_key)

    def get(self, kind: str, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._load().get(kind, {}).get(str(item_id))
        return dict(record) if record is not None else None

    def put(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        prepared = self._prepare(record)
        with self._lock:
            data = self._load()
            data.setdefault(kind, {})[prepared["id"]] = prepared
            self._save(data)
        return dict(prepared)

    def delete(self, kind: str, item_id: str) -> bool:
        with self._lock:
            data = self._load()
            existed = str(item_id) in data.get(kind, {})
            if existed:
                del data[kind][str(item_id)]
                self._save(data)
        return existed

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._load().get(SETTINGS_KIND, {}).get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            data = self._load()
            data.setdefault(SETTINGS_KIND, {})[key] = value
            self._save(data)

    def clear(self) -> None:
        with self._lock:
            self._save({})


class UpstashStore(BaseStore):
    """Vercel KV / Upstash Redis через REST API (без внешних зависимостей).

    Схема: Redis-hash на каждый kind (HSET kind id json-записи),
    настройки — hash "_settings".
    """

    def __init__(self, url: str, token: str, key_prefix: str = "olga") -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.key_prefix = key_prefix

    def _key(self, kind: str) -> str:
        return f"{self.key_prefix}:{kind}"

    def _cmd(self, *args: str) -> Any:
        """Выполнить одну Redis-команду через REST. Возвращает поле result."""
        payload = json.dumps(list(args)).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - любая сетевая ошибка -> StorageError
            raise StorageError(f"Upstash: ошибка запроса {args[0]}: {exc}") from exc
        if isinstance(body, dict) and "error" in body:
            raise StorageError(f"Upstash: {body['error']}")
        return body.get("result") if isinstance(body, dict) else None

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any] | None:
        if raw is None:
            return None
        try:
            record = json.loads(raw)
            return record if isinstance(record, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    def list(self, kind: str) -> list[dict[str, Any]]:
        flat = self._cmd("HGETALL", self._key(kind)) or []
        records = []
        for raw in flat[1::2]:
            record = self._decode(raw)
            if record is not None:
                records.append(record)
        return sorted(records, key=_sort_key)

    def get(self, kind: str, item_id: str) -> dict[str, Any] | None:
        return self._decode(self._cmd("HGET", self._key(kind), str(item_id)))

    def put(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        prepared = self._prepare(record)
        self._cmd(
            "HSET",
            self._key(kind),
            prepared["id"],
            json.dumps(prepared, ensure_ascii=False),
        )
        return dict(prepared)

    def delete(self, kind: str, item_id: str) -> bool:
        return bool(self._cmd("HDEL", self._key(kind), str(item_id)))

    def get_setting(self, key: str, default: Any = None) -> Any:
        raw = self._cmd("HGET", self._key(SETTINGS_KIND), key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self._cmd(
            "HSET",
            self._key(SETTINGS_KIND),
            key,
            json.dumps(value, ensure_ascii=False),
        )

    def clear(self) -> None:
        keys = self._cmd("KEYS", f"{self.key_prefix}:*") or []
        for key in keys:
            self._cmd("DEL", key)


class SupabaseStore(BaseStore):
    """Supabase (Postgres) через PostgREST — без внешних зависимостей.

    Схема: одна таблица olga_records(kind text, id text, data jsonb,
    primary key(kind, id)). Настройки — строки с kind="_settings",
    id=<ключ>, data={"value": ...}.
    """

    def __init__(self, url: str, key: str, table: str = "olga_records") -> None:
        self.base = url.rstrip("/") + "/rest/v1/" + table
        self.key = key

    def _request(
        self,
        method: str,
        query: str,
        body: Any = None,
        prefer: str = "",
    ) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        request = urllib.request.Request(
            self.base + query, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise StorageError(f"Supabase: HTTP {exc.code} {method} {query}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001 - сеть/таймаут -> StorageError
            raise StorageError(f"Supabase: ошибка запроса {method} {query}: {exc}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(f"Supabase: некорректный ответ: {exc}") from exc

    @staticmethod
    def _q(value: str) -> str:
        return urllib.parse.quote(str(value), safe="")

    def list(self, kind: str) -> list[dict[str, Any]]:
        rows = self._request("GET", f"?kind=eq.{self._q(kind)}&select=data") or []
        records = [row["data"] for row in rows if isinstance(row.get("data"), dict)]
        return sorted(records, key=_sort_key)

    def get(self, kind: str, item_id: str) -> dict[str, Any] | None:
        rows = self._request(
            "GET", f"?kind=eq.{self._q(kind)}&id=eq.{self._q(item_id)}&select=data"
        ) or []
        if not rows:
            return None
        data = rows[0].get("data")
        return data if isinstance(data, dict) else None

    def put(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        prepared = self._prepare(record)
        self._request(
            "POST",
            "",
            body=[{"kind": kind, "id": prepared["id"], "data": prepared}],
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return dict(prepared)

    def delete(self, kind: str, item_id: str) -> bool:
        rows = self._request(
            "DELETE",
            f"?kind=eq.{self._q(kind)}&id=eq.{self._q(item_id)}",
            prefer="return=representation",
        ) or []
        return len(rows) > 0

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.get(SETTINGS_KIND, key)
        if row is None:
            return default
        return row.get("value", default)

    def set_setting(self, key: str, value: Any) -> None:
        self._request(
            "POST",
            "",
            body=[{"kind": SETTINGS_KIND, "id": key, "data": {"value": value}}],
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def clear(self) -> None:
        # PostgREST требует фильтр для DELETE — kind=not.is.null покрывает всё
        self._request("DELETE", "?kind=not.is.null", prefer="return=minimal")


def _default_file_path() -> str:
    configured = os.environ.get("STORAGE_PATH", "").strip()
    if configured:
        return configured
    if os.environ.get("VERCEL"):
        return "/tmp/olga_store.json"
    return os.path.join("data", "store.json")


def get_store() -> BaseStore:
    """Выбрать бэкенд по env. Новый инстанс на вызов — безопасно для serverless.

    Порядок: явный STORAGE_BACKEND → Supabase (по кредам) → Upstash (по кредам) → файл.
    """
    backend = os.environ.get("STORAGE_BACKEND", "").strip().lower()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    kv_url = (
        os.environ.get("KV_REST_API_URL", "").strip()
        or os.environ.get("UPSTASH_REDIS_REST_URL", "").strip()
    )
    kv_token = (
        os.environ.get("KV_REST_API_TOKEN", "").strip()
        or os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    )

    if backend == "supabase" or (not backend and supabase_url and supabase_key):
        if not supabase_url or not supabase_key:
            raise StorageError(
                "STORAGE_BACKEND=supabase, но SUPABASE_URL/SUPABASE_KEY не заданы"
            )
        return SupabaseStore(supabase_url, supabase_key)
    if backend == "upstash" or (not backend and kv_url and kv_token):
        if not kv_url or not kv_token:
            raise StorageError(
                "STORAGE_BACKEND=upstash, но KV_REST_API_URL/KV_REST_API_TOKEN не заданы"
            )
        return UpstashStore(kv_url, kv_token)
    return FileStore(_default_file_path())
