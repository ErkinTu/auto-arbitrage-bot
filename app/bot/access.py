"""Контроль доступа: бот отвечает только участникам закрытой группы/канала."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.db import get_or_create_bot_user
from app.bot.messages import ACCESS_DENIED_TEXT
from app.core.config import get_settings

logger = logging.getLogger(__name__)

ALLOWED_STATUSES = {"creator", "administrator", "member"}


class AccessMiddleware(BaseMiddleware):
    """Пропускает апдейт только если автор состоит в TELEGRAM_ACCESS_CHAT_ID.

    Результат проверки кэшируется на BOT_ACCESS_CACHE_TTL_SECONDS, чтобы не
    дергать get_chat_member на каждое сообщение.
    """

    def __init__(self) -> None:
        self._cache: dict[int, tuple[bool, float]] = {}
        self._warned_no_chat = False

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.is_bot:
            return None

        # Бот работает только в личных сообщениях; апдейты из групп игнорируем,
        # иначе он реагировал бы на команды внутри самой закрытой группы.
        chat = event.chat if isinstance(event, Message) else None
        if isinstance(event, CallbackQuery) and event.message is not None:
            chat = event.message.chat
        if chat is not None and chat.type != "private":
            return None

        allowed, fresh_check = await self._check_access(data["bot"], user.id)
        if not allowed:
            await self._deny(event)
            return None

        if fresh_check:
            await asyncio.to_thread(
                get_or_create_bot_user, user.id, user.username, user.first_name
            )
        return await handler(event, data)

    async def _check_access(self, bot: Any, user_id: int) -> tuple[bool, bool]:
        """Возвращает (доступ разрешен, проверка была свежей, а не из кэша)."""
        settings = get_settings()
        if not settings.telegram_access_chat_id:
            if not self._warned_no_chat:
                logger.warning("TELEGRAM_ACCESS_CHAT_ID is not set: access check is disabled")
                self._warned_no_chat = True
            return True, True

        cached = self._cache.get(user_id)
        now = time.monotonic()
        if cached is not None and cached[1] > now:
            return cached[0], False

        try:
            member = await bot.get_chat_member(settings.telegram_access_chat_id, user_id)
            allowed = member.status in ALLOWED_STATUSES or (
                member.status == "restricted" and getattr(member, "is_member", False)
            )
        except TelegramBadRequest:
            allowed = False
        except TelegramAPIError:
            logger.exception("get_chat_member failed for user %s", user_id)
            allowed = False

        self._cache[user_id] = (allowed, now + settings.bot_access_cache_ttl_seconds)
        return allowed, True

    async def _deny(self, event: TelegramObject) -> None:
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(ACCESS_DENIED_TEXT, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(ACCESS_DENIED_TEXT)
        except TelegramAPIError:
            logger.debug("failed to deliver access-denied message", exc_info=True)
