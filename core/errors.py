"""Доменные ошибки. API превращает их в 400/404/409, бот — в понятные сообщения."""
from __future__ import annotations


class ValidationError(ValueError):
    """Невалидные входные данные."""


class NotFoundError(LookupError):
    """Запись не найдена."""


class ConflictError(RuntimeError):
    """Конфликт состояния (например, слот уже забронирован)."""
