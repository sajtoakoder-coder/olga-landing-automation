"""Тесты Telegram-бота: FSM-хранилище и обработчики (через feed_raw_update)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Chat, Message

from bot import handlers, products, slots
from bot.fsm_storage import StoreFSMStorage

ADMIN_ID = 777
STRANGER_ID = 555


class MockedSession(BaseSession):
    """Сессия aiogram, которая не ходит в сеть и записывает вызовы API."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[Any] = []

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message.model_construct(
                message_id=len(self.requests),
                date=datetime.now(timezone.utc),
                chat=Chat.model_construct(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        yield b""


class BotEnv(SimpleNamespace):
    """Обёртка: диспетчер + мок-бот + помощники для отправки апдейтов."""

    def __init__(self, store) -> None:
        session = MockedSession()
        super().__init__(
            store=store,
            session=session,
            bot=Bot(token="123:ABC", session=session),
            dp=handlers.create_dispatcher(store),
            _counter=0,
        )

    def _next(self) -> int:
        self._counter += 1
        return self._counter

    async def send(self, text: str, chat_id: int = ADMIN_ID) -> None:
        n = self._next()
        await self.dp.feed_raw_update(
            self.bot,
            {
                "update_id": n,
                "message": {
                    "message_id": n,
                    "date": 1750000000,
                    "chat": {"id": chat_id, "type": "private", "first_name": "O"},
                    "from": {"id": chat_id, "is_bot": False, "first_name": "O"},
                    "text": text,
                },
            },
        )

    async def click(self, data: str, chat_id: int = ADMIN_ID, message_text: str = "меню") -> None:
        n = self._next()
        await self.dp.feed_raw_update(
            self.bot,
            {
                "update_id": n,
                "callback_query": {
                    "id": f"cb{n}",
                    "from": {"id": chat_id, "is_bot": False, "first_name": "O"},
                    "chat_instance": "ci",
                    "data": data,
                    "message": {
                        "message_id": 10_000 + n,
                        "date": 1750000000,
                        "chat": {"id": chat_id, "type": "private", "first_name": "O"},
                        "text": message_text,
                    },
                },
            },
        )

    @property
    def sent(self) -> list[str]:
        return [m.text for m in self.session.requests if isinstance(m, SendMessage)]

    @property
    def edited(self) -> list[str]:
        return [m.text for m in self.session.requests if isinstance(m, EditMessageText)]

    @property
    def last(self) -> str:
        return self.sent[-1] if self.sent else ""

    def reset(self) -> None:
        self.session.requests.clear()


@pytest.fixture
def env(store, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", str(ADMIN_ID))
    return BotEnv(store)


# ---------- FSM-хранилище ----------


async def test_fsm_storage_roundtrip(store):
    fsm = StoreFSMStorage(store)
    key = StorageKey(bot_id=1, chat_id=777, user_id=777)

    assert await fsm.get_state(key) is None
    assert await fsm.get_data(key) == {}

    await fsm.set_state(key, "SomeState:step")
    await fsm.set_data(key, {"a": 1})
    assert await fsm.get_state(key) == "SomeState:step"
    assert await fsm.get_data(key) == {"a": 1}

    # состояние переживает "перезапуск" (новый инстанс поверх того же store)
    fsm2 = StoreFSMStorage(store)
    assert await fsm2.get_state(key) == "SomeState:step"

    await fsm.set_state(key, None)
    assert await fsm.get_state(key) is None
    # данные разных ключей изолированы
    other = StorageKey(bot_id=1, chat_id=888, user_id=888)
    assert await fsm.get_data(other) == {}


# ---------- /start и доступ ----------


async def test_start_stranger_gets_chat_id(env):
    await env.send("/start", chat_id=STRANGER_ID)
    assert str(STRANGER_ID) in env.last
    assert "TELEGRAM_ADMIN_CHAT_ID" in env.last


async def test_start_without_admin_env_shows_chat_id(env, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID")
    await env.send("/start")
    assert str(ADMIN_ID) in env.last


async def test_start_admin_seeds_and_greets(env):
    await env.send("/start")
    assert "админ-панель" in env.last
    titles = [p["title"] for p in products.list_products(env.store)]
    assert len(titles) == 4
    assert "Разовая консультация" in titles


async def test_stranger_message_rejected(env):
    await env.send("привет", chat_id=STRANGER_ID)
    assert "личный бот Ольги" in env.last
    # и колбэки чужаков игнорируются
    env.reset()
    await env.click("pr:add", chat_id=STRANGER_ID)
    assert env.sent == []


# ---------- товары ----------


async def test_products_list_shown(env):
    product = products.add_product(env.store, title="Тестовый", price=1000)
    await env.send("📦 Товары")
    assert "Тестовый" in env.last
    assert "1 000 ₽" in env.last
    assert "/api/payment/create?product_id=" + product["id"] in env.last


async def test_add_product_full_flow(env):
    await env.click("pr:add")
    assert "Название" in env.last
    await env.send("Марафон женственности")
    assert "описание" in env.last.lower()
    await env.send("21 день практик")
    assert "Цена" in env.last
    await env.send("7 900")
    assert "Тип товара" in env.last
    await env.click("pr:kind:payment")
    assert "выдать" in env.last.lower()
    await env.click("pr:dlv:email")
    assert "письма" in env.last.lower()
    await env.send("Ссылка на чат: https://t.me/+abc")
    assert "сессия по слотам" in env.last.lower()
    await env.click("pr:slot:no")
    assert "Готово" in env.last

    items = products.list_products(env.store)
    assert len(items) == 1
    saved = items[0]
    assert saved["title"] == "Марафон женственности"
    assert saved["description"] == "21 день практик"
    assert saved["price"] == 7900
    assert saved["kind"] == "payment"
    assert saved["delivery"] == "email"
    assert saved["delivery_content"] == "Ссылка на чат: https://t.me/+abc"
    assert saved["requires_slot"] is False
    assert saved["is_visible"] is True


async def test_add_product_request_kind_skips_delivery(env):
    await env.click("pr:add")
    await env.send("Сопровождение")
    await env.send("-")
    await env.send("50000")
    await env.click("pr:kind:request")
    # сразу вопрос про слоты, без выдачи
    assert "сессия по слотам" in env.last.lower()
    await env.click("pr:slot:no")
    saved = products.list_products(env.store)[0]
    assert saved["kind"] == "request"
    assert saved["delivery"] == "none"


async def test_add_product_invalid_price_retries(env):
    await env.click("pr:add")
    await env.send("Товар")
    await env.send("-")
    await env.send("дорого")
    assert "Не поняла" in env.last
    await env.send("5000")
    assert "Тип товара" in env.last


async def test_edit_product_price(env):
    product = products.add_product(env.store, title="Товар", price=1000)
    await env.click(f"pr:edit:{product['id']}")
    assert "Что меняем" in env.last
    await env.click(f"pr:field:{product['id']}:price")
    assert "цена" in env.last.lower()
    await env.send("2 500")
    assert "Обновила" in env.last
    assert products.get_product(env.store, product["id"])["price"] == 2500


async def test_edit_product_delivery_via_buttons(env):
    product = products.add_product(env.store, title="Товар", price=1000, delivery="none")
    await env.click(f"pr:field:{product['id']}:delivery")
    await env.click("pr:dlv:link")
    assert products.get_product(env.store, product["id"])["delivery"] == "link"


async def test_edit_product_image(env):
    product = products.add_product(env.store, title="Товар", price=1000)
    await env.click(f"pr:field:{product['id']}:image")
    assert "фото" in env.last.lower()
    await env.send("https://example.com/photo.jpg")
    assert products.get_product(env.store, product["id"])["image"] == "https://example.com/photo.jpg"
    # «-» убирает фото
    await env.click(f"pr:field:{product['id']}:image")
    await env.send("-")
    assert products.get_product(env.store, product["id"])["image"] == ""


async def test_edit_validation_error_keeps_state(env):
    product = products.add_product(env.store, title="Товар", price=1000)
    await env.click(f"pr:field:{product['id']}:title")
    await env.send("   ")
    assert "Не поняла" in env.last
    await env.send("Новое имя")
    assert products.get_product(env.store, product["id"])["title"] == "Новое имя"


async def test_toggle_visibility(env):
    product = products.add_product(env.store, title="Товар", price=1000)
    await env.click(f"pr:vis:{product['id']}")
    assert products.get_product(env.store, product["id"])["is_visible"] is False
    # товар пропал с сайта, но остался в списке бота
    assert products.visible_products(env.store) == []
    assert len(products.list_products(env.store)) == 1
    await env.click(f"pr:vis:{product['id']}")
    assert products.get_product(env.store, product["id"])["is_visible"] is True


async def test_delete_product_with_confirmation(env):
    product = products.add_product(env.store, title="Удаляемый", price=1000)
    await env.click(f"pr:del:{product['id']}")
    assert "Точно удалить" in env.last
    assert len(products.list_products(env.store)) == 1  # ещё не удалён
    await env.click(f"pr:delok:{product['id']}")
    assert products.list_products(env.store) == []


# ---------- слоты ----------


async def test_add_slots_flow_with_default_link(env):
    await env.click("sl:add")
    assert "по одному на строку" in env.last
    await env.send("01.08.2026 15:00\n2026-08-02 12:30\nмусор")
    assert "Добавила слотов: 2" in env.last
    assert "мусор" in env.last
    await env.send("-")
    stored = slots.list_slots(env.store)
    assert len(stored) == 2
    assert all(s["telemost_url"] == "" for s in stored)
    assert "Твои слоты" in env.last


async def test_add_slots_flow_with_custom_link(env):
    await env.click("sl:add")
    await env.send("01.08.2026 15:00")
    await env.send("https://telemost.yandex.ru/j/room1")
    assert slots.list_slots(env.store)[0]["telemost_url"] == "https://telemost.yandex.ru/j/room1"


async def test_slots_list_shows_booked(env):
    free = slots.add_slot(env.store, "2126-08-01 15:00")
    booked = slots.add_slot(env.store, "2126-08-02 15:00")
    slots.book_slot(env.store, booked["id"], customer={"name": "Анна"}, payment_id="p1")
    await env.send("🗓 Слоты")
    assert "Свободные" in env.last
    assert "Забронированные" in env.last
    assert "Анна" in env.last
    assert slots.format_slot(free) in env.last


async def test_delete_slot_via_button(env):
    slot = slots.add_slot(env.store, "2126-08-01 15:00")
    await env.click(f"sl:del:{slot['id']}")
    assert slots.list_slots(env.store) == []


# ---------- статистика ----------


async def test_stats_command_and_periods(env):
    env.store.put("bookings", {"name": "A", "created_at": datetime.now(timezone.utc).isoformat()})
    await env.send("/stats")
    assert "Статистика за сегодня" in env.last
    assert "Заявок: <b>1</b>" in env.last

    await env.click("st:all", message_text=env.last)
    assert any("за всё время" in t for t in env.edited)


# ---------- настройки ----------


async def test_settings_show_and_set_telemost(env):
    await env.send("⚙️ Настройки")
    assert "Настройки" in env.last
    assert "не задана" in env.last

    await env.click("cfg:telemost")
    await env.send("не ссылка")
    assert "http(s)://" in env.last
    await env.send("https://telemost.yandex.ru/j/main")
    assert env.store.get_setting("telemost_url") == "https://telemost.yandex.ru/j/main"
    assert "Сохранила" in env.last


# ---------- отмена и fallback ----------


async def test_cancel_resets_state(env):
    await env.click("pr:add")
    await env.send("/cancel")
    assert "отменила" in env.last.lower()
    await env.send("какой-то текст")
    assert "Выбери раздел" in env.last
    assert products.list_products(env.store) == []


async def test_menu_works_after_cancel_mid_dialog(env):
    await env.click("pr:add")
    await env.send("Отмена")
    await env.send("📦 Товары")
    assert "Товаров пока нет" in env.last
