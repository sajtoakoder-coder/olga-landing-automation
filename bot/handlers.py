"""Обработчики Telegram-бота (админ-панель Ольги).

Меню: Товары (CRUD), Слоты (расписание), Статистика, Настройки.
Диалоги — на FSM aiogram с хранением состояния в Store (serverless-safe).
"""
from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)

from bot import products, slots, stats
from bot.fsm_storage import StoreFSMStorage
from core import config
from core.errors import ConflictError, NotFoundError, ValidationError
from seed import ensure_seed
from storage.store import BaseStore

logger = logging.getLogger(__name__)

BTN_PRODUCTS = "📦 Товары"
BTN_SLOTS = "🗓 Слоты"
BTN_STATS = "📊 Статистика"
BTN_SETTINGS = "⚙️ Настройки"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_PRODUCTS), KeyboardButton(text=BTN_SLOTS)],
        [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_SETTINGS)],
    ],
    resize_keyboard=True,
)

SKIP_WORDS = {"-", "—", "нет", "пропустить"}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def is_admin_chat(chat_id: int | str) -> bool:
    admin = config.admin_chat_id()
    return bool(admin) and str(chat_id) == admin


# ---------- FSM ----------


class AddProduct(StatesGroup):
    title = State()
    description = State()
    price = State()
    kind = State()
    delivery = State()
    delivery_content = State()
    requires_slot = State()


class EditProduct(StatesGroup):
    value = State()  # data: product_id, field


class AddSlots(StatesGroup):
    dates = State()
    telemost = State()


class SettingsForm(StatesGroup):
    telemost_url = State()


# ---------- клавиатуры ----------


def products_keyboard(items: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for product in items:
        eye = "🙈 Скрыть" if product.get("is_visible", True) else "👁 Показать"
        rows.append(
            [
                InlineKeyboardButton(text=f"✏️ {product['title'][:20]}", callback_data=f"pr:edit:{product['id']}"),
                InlineKeyboardButton(text=eye, callback_data=f"pr:vis:{product['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"pr:del:{product['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить товар", callback_data="pr:add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slots_keyboard(items: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for slot in items:
        if slot.get("status") == slots.STATUS_FREE:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🗑 {slots.format_slot(slot)}", callback_data=f"sl:del:{slot['id']}"
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="➕ Добавить слоты", callback_data="sl:add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


STATS_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Сегодня", callback_data="st:today"),
            InlineKeyboardButton(text="Неделя", callback_data="st:week"),
        ],
        [
            InlineKeyboardButton(text="Месяц", callback_data="st:month"),
            InlineKeyboardButton(text="Всё время", callback_data="st:all"),
        ],
    ]
)

SETTINGS_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🎥 Ссылка на Телемост", callback_data="cfg:telemost")]
    ]
)

KIND_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 Оплата", callback_data="pr:kind:payment"),
            InlineKeyboardButton(text="📝 Заявка", callback_data="pr:kind:request"),
        ]
    ]
)

DELIVERY_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="✉️ Email", callback_data="pr:dlv:email"),
            InlineKeyboardButton(text="🔗 Ссылка", callback_data="pr:dlv:link"),
            InlineKeyboardButton(text="∅ Ничего", callback_data="pr:dlv:none"),
        ]
    ]
)

SLOT_REQ_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Да, по слотам", callback_data="pr:slot:yes"),
            InlineKeyboardButton(text="Нет", callback_data="pr:slot:no"),
        ]
    ]
)

EDIT_FIELDS = {
    "title": "Название",
    "description": "Описание",
    "price": "Цена",
    "delivery": "Тип выдачи",
    "delivery_content": "Содержимое выдачи",
}


def edit_fields_keyboard(product_id: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"pr:field:{product_id}:{field}")]
        for field, label in EDIT_FIELDS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- тексты ----------


def product_line(product: dict[str, Any]) -> str:
    status = "" if product.get("is_visible", True) else " · <i>скрыт</i>"
    kind_label = products.KIND_LABELS.get(product.get("kind", ""), "?")
    delivery_label = products.DELIVERY_LABELS.get(product.get("delivery", ""), "?")
    slot_mark = " · слоты" if product.get("requires_slot") else ""
    return (
        f"<b>{esc(product['title'])}</b> — {products.format_price(int(product.get('price', 0)))}\n"
        f"{kind_label} · выдача: {delivery_label}{slot_mark}{status}"
    )


def products_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Товаров пока нет. Нажми «➕ Добавить товар»."
    parts = ["<b>Твои товары:</b>", ""]
    for i, product in enumerate(items, 1):
        parts.append(f"{i}. {product_line(product)}")
        if product.get("kind") == "payment":
            parts.append(f"Оплата: {products.payment_link(product)}")
        parts.append("")
    return "\n".join(parts).strip()


def slots_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Слотов пока нет. Нажми «➕ Добавить слоты»."
    free = [s for s in items if s.get("status") == slots.STATUS_FREE]
    booked = [s for s in items if s.get("status") == slots.STATUS_BOOKED]
    parts = ["<b>Твои слоты:</b>", ""]
    if free:
        parts.append("Свободные:")
        parts.extend(f"· {slots.format_slot(s)}" for s in free)
    if booked:
        parts.append("")
        parts.append("Забронированные:")
        for s in booked:
            who = (s.get("booked_by") or {}).get("name") or (s.get("booked_by") or {}).get(
                "contact"
            ) or "клиент"
            parts.append(f"· {slots.format_slot(s)} — {esc(who)}")
    return "\n".join(parts)


def settings_text(store: BaseStore) -> str:
    telemost = (
        str(store.get_setting("telemost_url") or "").strip()
        or config.default_telemost_url()
        or "не задана"
    )
    payment = "заглушка (stub)" if config.payment_mode() != "live" else "боевой (live)"
    email_mode = "заглушка (stub)" if config.email_mode() != "live" else "боевой (live)"
    return (
        "<b>Настройки</b>\n"
        f"🎥 Ссылка на Телемост: {esc(telemost)}\n"
        f"💳 Оплата: {payment}\n"
        f"✉️ Email: {email_mode}"
    )


# ---------- роутер ----------


def create_router() -> Router:
    router = Router()

    # --- /start: доступен всем, показывает chat_id ---

    @router.message(Command("start"))
    async def cmd_start(message: Message, store: BaseStore, state: FSMContext) -> None:
        chat_id = message.chat.id
        if is_admin_chat(chat_id):
            await state.clear()
            ensure_seed(store)
            await message.answer(
                "С возвращением! Я твоя админ-панель: товары, слоты, статистика, настройки.",
                reply_markup=MAIN_KB,
            )
            return
        await message.answer(
            "Привет! Это бот-админка сайта Ольги Андреевой.\n"
            f"Твой chat_id: <code>{chat_id}</code>\n\n"
            "Если ты Ольга — вставь этот chat_id в переменную окружения "
            "TELEGRAM_ADMIN_CHAT_ID и отправь /start ещё раз."
        )

    # --- отмена ---

    @router.message(Command("cancel"))
    @router.message(F.text.casefold() == "отмена")
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        if not is_admin_chat(message.chat.id):
            return
        await state.clear()
        await message.answer("Ок, отменила.", reply_markup=MAIN_KB)

    # --- защита: всё остальное только для Ольги ---

    admin = Router()
    admin.message.filter(lambda message: is_admin_chat(message.chat.id))
    admin.callback_query.filter(
        lambda callback: callback.message is not None
        and is_admin_chat(callback.message.chat.id)
    )
    router.include_router(admin)

    # ===== ТОВАРЫ =====

    @admin.message(Command("products"))
    @admin.message(F.text == BTN_PRODUCTS, StateFilter(None))
    async def show_products(message: Message, store: BaseStore) -> None:
        items = products.list_products(store)
        await message.answer(products_text(items), reply_markup=products_keyboard(items))

    @admin.callback_query(F.data == "pr:add")
    async def cb_add_product(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddProduct.title)
        await callback.message.answer("Название товара?")
        await callback.answer()

    @admin.message(AddProduct.title, F.text)
    async def add_title(message: Message, state: FSMContext) -> None:
        await state.update_data(title=message.text.strip())
        await state.set_state(AddProduct.description)
        await message.answer("Короткое описание? (или «-», чтобы пропустить)")

    @admin.message(AddProduct.description, F.text)
    async def add_description(message: Message, state: FSMContext) -> None:
        text = message.text.strip()
        await state.update_data(description="" if text.lower() in SKIP_WORDS else text)
        await state.set_state(AddProduct.price)
        await message.answer("Цена в рублях? Например: 5000")

    @admin.message(AddProduct.price, F.text)
    async def add_price(message: Message, state: FSMContext) -> None:
        try:
            price = products.parse_price(message.text)
        except ValidationError as exc:
            await message.answer(f"Не поняла: {esc(str(exc))}. Попробуй ещё раз, например 5000.")
            return
        await state.update_data(price=price)
        await state.set_state(AddProduct.kind)
        await message.answer(
            "Тип товара: клиент сразу платит или оставляет заявку?", reply_markup=KIND_KB
        )

    @admin.callback_query(AddProduct.kind, F.data.startswith("pr:kind:"))
    async def add_kind(callback: CallbackQuery, state: FSMContext) -> None:
        kind = callback.data.split(":")[-1]
        await state.update_data(kind=kind)
        if kind == "request":
            # для заявок выдача не нужна
            await state.update_data(delivery="none", delivery_content="")
            await state.set_state(AddProduct.requires_slot)
            await callback.message.answer(
                "Это индивидуальная сессия по слотам расписания?", reply_markup=SLOT_REQ_KB
            )
        else:
            await state.set_state(AddProduct.delivery)
            await callback.message.answer(
                "Что выдать клиенту после оплаты?", reply_markup=DELIVERY_KB
            )
        await callback.answer()

    @admin.callback_query(AddProduct.delivery, F.data.startswith("pr:dlv:"))
    async def add_delivery(callback: CallbackQuery, state: FSMContext) -> None:
        delivery = callback.data.split(":")[-1]
        await state.update_data(delivery=delivery)
        if delivery == "none":
            await state.update_data(delivery_content="")
            await state.set_state(AddProduct.requires_slot)
            await callback.message.answer(
                "Это индивидуальная сессия по слотам расписания?", reply_markup=SLOT_REQ_KB
            )
        else:
            await state.set_state(AddProduct.delivery_content)
            hint = (
                "Текст письма клиенту (например, ссылка на закрытый чат):"
                if delivery == "email"
                else "Ссылка, которую показать клиенту после оплаты (или «-» — тогда возьмём ссылку Телемоста из настроек):"
            )
            await callback.message.answer(hint)
        await callback.answer()

    @admin.message(AddProduct.delivery_content, F.text)
    async def add_delivery_content(message: Message, state: FSMContext) -> None:
        text = message.text.strip()
        await state.update_data(delivery_content="" if text.lower() in SKIP_WORDS else text)
        await state.set_state(AddProduct.requires_slot)
        await message.answer(
            "Это индивидуальная сессия по слотам расписания?", reply_markup=SLOT_REQ_KB
        )

    @admin.callback_query(AddProduct.requires_slot, F.data.startswith("pr:slot:"))
    async def add_requires_slot(
        callback: CallbackQuery, state: FSMContext, store: BaseStore
    ) -> None:
        requires_slot = callback.data.endswith(":yes")
        data = await state.get_data()
        await state.clear()
        try:
            product = products.add_product(
                store,
                title=data.get("title", ""),
                description=data.get("description", ""),
                price=data.get("price", 0),
                kind=data.get("kind", "payment"),
                delivery=data.get("delivery", "none"),
                delivery_content=data.get("delivery_content", ""),
                requires_slot=requires_slot,
            )
        except ValidationError as exc:
            await callback.message.answer(f"Не сохранилось: {esc(str(exc))}")
            await callback.answer()
            return
        await callback.message.answer(
            "Готово! Товар уже на сайте:\n\n" + product_line(product) +
            f"\nОплата: {products.payment_link(product)}",
            reply_markup=MAIN_KB,
        )
        await callback.answer("Сохранено")

    # --- скрыть/показать ---

    @admin.callback_query(F.data.startswith("pr:vis:"))
    async def cb_toggle_visibility(callback: CallbackQuery, store: BaseStore) -> None:
        product_id = callback.data.split(":")[-1]
        try:
            product = products.get_product(store, product_id)
            product = products.set_visibility(store, product_id, not product.get("is_visible", True))
        except NotFoundError:
            await callback.answer("Товар не найден", show_alert=True)
            return
        items = products.list_products(store)
        await callback.message.edit_text(
            products_text(items), reply_markup=products_keyboard(items)
        )
        await callback.answer("Скрыт с сайта" if not product["is_visible"] else "Снова на сайте")

    # --- удаление ---

    @admin.callback_query(F.data.startswith("pr:del:"))
    async def cb_delete_ask(callback: CallbackQuery, store: BaseStore) -> None:
        product_id = callback.data.split(":")[-1]
        try:
            product = products.get_product(store, product_id)
        except NotFoundError:
            await callback.answer("Уже удалён", show_alert=True)
            return
        await callback.message.answer(
            f"Точно удалить «{esc(product['title'])}»?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🗑 Да, удалить", callback_data=f"pr:delok:{product_id}"
                        ),
                        InlineKeyboardButton(text="Отмена", callback_data="pr:list"),
                    ]
                ]
            ),
        )
        await callback.answer()

    @admin.callback_query(F.data.startswith("pr:delok:"))
    async def cb_delete_confirm(callback: CallbackQuery, store: BaseStore) -> None:
        product_id = callback.data.split(":")[-1]
        products.delete_product(store, product_id)
        items = products.list_products(store)
        await callback.message.edit_text(
            "Удалила. " + ("Список товаров:" if items else "Товаров больше нет."),
        )
        await callback.message.answer(products_text(items), reply_markup=products_keyboard(items))
        await callback.answer("Удалено")

    @admin.callback_query(F.data == "pr:list")
    async def cb_products_list(callback: CallbackQuery, store: BaseStore) -> None:
        items = products.list_products(store)
        await callback.message.edit_text(
            products_text(items), reply_markup=products_keyboard(items)
        )
        await callback.answer()

    # --- редактирование ---

    @admin.callback_query(F.data.startswith("pr:edit:"))
    async def cb_edit_product(callback: CallbackQuery, store: BaseStore) -> None:
        product_id = callback.data.split(":")[-1]
        try:
            product = products.get_product(store, product_id)
        except NotFoundError:
            await callback.answer("Товар не найден", show_alert=True)
            return
        await callback.message.answer(
            f"Что меняем в «{esc(product['title'])}»?",
            reply_markup=edit_fields_keyboard(product_id),
        )
        await callback.answer()

    @admin.callback_query(F.data.startswith("pr:field:"))
    async def cb_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, product_id, field = callback.data.split(":")
        await state.set_state(EditProduct.value)
        await state.update_data(product_id=product_id, field=field)
        if field == "delivery":
            await callback.message.answer("Новый тип выдачи:", reply_markup=DELIVERY_KB)
        else:
            prompts = {
                "title": "Новое название?",
                "description": "Новое описание?",
                "price": "Новая цена в рублях?",
                "delivery_content": "Новое содержимое выдачи (текст письма или ссылка)?",
            }
            await callback.message.answer(prompts.get(field, "Новое значение?"))
        await callback.answer()

    @admin.callback_query(EditProduct.value, F.data.startswith("pr:dlv:"))
    async def edit_delivery_choice(
        callback: CallbackQuery, state: FSMContext, store: BaseStore
    ) -> None:
        delivery = callback.data.split(":")[-1]
        data = await state.get_data()
        await state.clear()
        try:
            product = products.update_product(store, data["product_id"], delivery=delivery)
        except (NotFoundError, ValidationError) as exc:
            await callback.message.answer(f"Не получилось: {esc(str(exc))}")
            await callback.answer()
            return
        await callback.message.answer("Обновила:\n\n" + product_line(product), reply_markup=MAIN_KB)
        await callback.answer("Сохранено")

    @admin.message(EditProduct.value, F.text)
    async def edit_value(message: Message, state: FSMContext, store: BaseStore) -> None:
        data = await state.get_data()
        field = data.get("field", "")
        try:
            product = products.update_product(
                store, data.get("product_id", ""), **{field: message.text.strip()}
            )
        except ValidationError as exc:
            await message.answer(f"Не поняла: {esc(str(exc))}. Попробуй ещё раз или /cancel.")
            return
        except NotFoundError:
            await state.clear()
            await message.answer("Товар не найден — возможно, удалён.", reply_markup=MAIN_KB)
            return
        await state.clear()
        await message.answer("Обновила:\n\n" + product_line(product), reply_markup=MAIN_KB)

    # ===== СЛОТЫ =====

    @admin.message(Command("slots"))
    @admin.message(F.text == BTN_SLOTS, StateFilter(None))
    async def show_slots(message: Message, store: BaseStore) -> None:
        items = slots.list_slots(store)
        await message.answer(slots_text(items), reply_markup=slots_keyboard(items))

    @admin.callback_query(F.data == "sl:add")
    async def cb_add_slots(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddSlots.dates)
        await callback.message.answer(
            "Пришли даты и время свободных слотов — по одному на строку.\n"
            "Формат: <code>01.08.2026 15:00</code> или <code>2026-08-01 15:00</code>"
        )
        await callback.answer()

    @admin.message(AddSlots.dates, F.text)
    async def add_slots_dates(message: Message, state: FSMContext, store: BaseStore) -> None:
        added, errors = slots.add_slots_bulk(store, message.text)
        if not added and errors:
            await message.answer(
                "Не добавила ни одного слота:\n" + "\n".join(esc(e) for e in errors[:5]) +
                "\nПопробуй ещё раз или /cancel."
            )
            return
        await state.update_data(slot_ids=[s["id"] for s in added])
        await state.set_state(AddSlots.telemost)
        report = f"Добавила слотов: {len(added)}."
        if errors:
            report += "\nНе получилось:\n" + "\n".join(esc(e) for e in errors[:5])
        await message.answer(
            report + "\n\nСсылка на Телемост для этих слотов? "
            "(пришли ссылку или «-», чтобы использовать дефолтную из настроек)"
        )

    @admin.message(AddSlots.telemost, F.text)
    async def add_slots_telemost(message: Message, state: FSMContext, store: BaseStore) -> None:
        text = message.text.strip()
        data = await state.get_data()
        await state.clear()
        if text.lower() not in SKIP_WORDS:
            if not text.lower().startswith(("http://", "https://")):
                await message.answer(
                    "Ссылка должна начинаться с http(s)://. Слоты сохранены с дефолтной ссылкой.",
                    reply_markup=MAIN_KB,
                )
            else:
                for slot_id in data.get("slot_ids", []):
                    slot = store.get("slots", slot_id)
                    if slot is not None:
                        slot["telemost_url"] = text
                        store.put("slots", slot)
        items = slots.list_slots(store)
        await message.answer(slots_text(items), reply_markup=slots_keyboard(items))

    @admin.callback_query(F.data.startswith("sl:del:"))
    async def cb_delete_slot(callback: CallbackQuery, store: BaseStore) -> None:
        slot_id = callback.data.split(":")[-1]
        slots.delete_slot(store, slot_id)
        items = slots.list_slots(store)
        await callback.message.edit_text(slots_text(items), reply_markup=slots_keyboard(items))
        await callback.answer("Удалила")

    # ===== СТАТИСТИКА =====

    @admin.message(Command("stats"))
    @admin.message(F.text == BTN_STATS, StateFilter(None))
    async def show_stats(message: Message, store: BaseStore) -> None:
        await message.answer(
            stats.format_stats(stats.collect_stats(store, "today")), reply_markup=STATS_KB
        )

    @admin.callback_query(F.data.startswith("st:"))
    async def cb_stats_period(callback: CallbackQuery, store: BaseStore) -> None:
        period = callback.data.split(":")[-1]
        text = stats.format_stats(stats.collect_stats(store, period))
        if callback.message.text != text:
            await callback.message.edit_text(text, reply_markup=STATS_KB)
        await callback.answer()

    # ===== НАСТРОЙКИ =====

    @admin.message(Command("settings"))
    @admin.message(F.text == BTN_SETTINGS, StateFilter(None))
    async def show_settings(message: Message, store: BaseStore) -> None:
        await message.answer(settings_text(store), reply_markup=SETTINGS_KB)

    @admin.callback_query(F.data == "cfg:telemost")
    async def cb_set_telemost(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SettingsForm.telemost_url)
        await callback.message.answer(
            "Пришли дефолтную ссылку на Телемост (https://telemost.yandex.ru/...)."
        )
        await callback.answer()

    @admin.message(SettingsForm.telemost_url, F.text)
    async def set_telemost_url(message: Message, state: FSMContext, store: BaseStore) -> None:
        url = message.text.strip()
        if not url.lower().startswith(("http://", "https://")):
            await message.answer("Ссылка должна начинаться с http(s)://. Попробуй ещё раз или /cancel.")
            return
        store.set_setting("telemost_url", url)
        await state.clear()
        await message.answer("Сохранила! " + settings_text(store), reply_markup=MAIN_KB)

    # ===== fallback =====

    @admin.message(StateFilter(None), F.text)
    async def admin_fallback(message: Message) -> None:
        await message.answer("Выбери раздел на клавиатуре ниже 👇", reply_markup=MAIN_KB)

    # ВАЖНО: хендлеры родительского роутера проверяются раньше вложенного admin,
    # поэтому этот fallback обязан не совпадать для чата Ольги.
    @router.message(F.text, lambda message: not is_admin_chat(message.chat.id))
    async def non_admin_fallback(message: Message) -> None:
        await message.answer(
            "Это личный бот Ольги. Если хочешь записаться — оставь заявку на сайте."
        )

    return router


def create_dispatcher(store: BaseStore) -> Dispatcher:
    """Диспетчер с FSM-хранилищем в Store и инъекцией store в хендлеры."""
    dispatcher = Dispatcher(storage=StoreFSMStorage(store), store=store)
    dispatcher.include_router(create_router())
    return dispatcher


async def process_update(store: BaseStore, update: dict[str, Any]) -> None:
    """Обработать один webhook-апдейт Telegram (вызывается из api/bot/webhook.py)."""
    token = config.bot_token()
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN не задан — апдейт пропущен")
        return
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession

    dispatcher = create_dispatcher(store)
    # таймаут короче лимита serverless-функции: лучше потерять ответ, чем словить
    # kill функции и бесконечные ретраи апдейта от Telegram
    async with Bot(
        token=token,
        session=AiohttpSession(timeout=8),
        default=DefaultBotProperties(parse_mode="HTML"),
    ) as bot:
        await dispatcher.feed_raw_update(bot, update)
