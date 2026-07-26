"""FSM-хранилище aiogram поверх нашего Store.

MemoryStorage не подходит: serverless-функции не держат память между
вызовами, а диалоги бота (добавление товара и т.п.) — многошаговые.
"""
from __future__ import annotations

from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from storage.store import BaseStore

KIND = "fsm"


class StoreFSMStorage(BaseStorage):
    """Хранит состояние и данные диалога в записи kind="fsm"."""

    def __init__(self, store: BaseStore) -> None:
        self.store = store

    @staticmethod
    def _id(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}"

    def _load(self, key: StorageKey) -> dict[str, Any]:
        return self.store.get(KIND, self._id(key)) or {"id": self._id(key)}

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        record = self._load(key)
        record["state"] = state.state if isinstance(state, State) else state
        self.store.put(KIND, record)

    async def get_state(self, key: StorageKey) -> str | None:
        return self._load(key).get("state")

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        record = self._load(key)
        record["data"] = dict(data)
        self.store.put(KIND, record)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return dict(self._load(key).get("data") or {})

    async def close(self) -> None:  # pragma: no cover - нечего закрывать
        return None
