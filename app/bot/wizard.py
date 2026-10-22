"""Выход из визарда по любой команде.

Визард `/add_filter` держит состояние в MemoryStorage, пока человек не закончит
или не отправит `/cancel`. Уведомления о новых авто приходят в тот же чат и
уносят сообщение с кнопками наверх, так что брошенный визард остаётся висеть, а
любая команда упирается в «выберите вариант кнопкой» — выхода не видно.

Поэтому сообщение, начинающееся с `/`, — и кнопка постоянного меню (`menu.py`),
она та же команда — сбрасывает состояние и идёт дальше как обычная команда. Это же сохраняет правило для `/admin`: не-админ получает то же
молчание, что и вне визарда, а не отличающуюся подсказку.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

from app.bot.menu import menu_command


class WizardExitMiddleware(BaseMiddleware):
    """Сбрасывает FSM-состояние, если пришла команда."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if (
            state is not None
            and isinstance(event, Message)
            and event.text
            and (event.text.startswith("/") or menu_command(event.text) is not None)
            and await state.get_state() is not None
        ):
            await state.clear()
            # Фильтры состояний aiogram читают `raw_state`, который FSM-middleware
            # положил в data ДО нас. Без этой строки состояние в хранилище уже
            # пустое, а хендлеры шагов визарда всё ещё ловят сообщение (так
            # `/admin` внутри визарда отвечал «выберите вариант кнопкой»).
            data["raw_state"] = None
        return await handler(event, data)
