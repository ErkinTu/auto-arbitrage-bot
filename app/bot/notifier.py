"""Отправка уведомлений с учетом лимитов Telegram.

Лимиты: ~30 сообщений/сек глобально, ~1 сообщение/сек в один чат.
Принимает любой объект с async send_message(...) — в тестах используется фейк.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Protocol

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from app.bot.matching import PendingNotification
from app.bot.messages import format_notification

logger = logging.getLogger(__name__)

GLOBAL_DELAY_SECONDS = 0.05  # ~20 сообщений/сек < лимита 30/сек
PER_CHAT_DELAY_SECONDS = 1.05  # чуть больше лимита 1 сообщение/сек в чат


class BotLike(Protocol):
    def send_message(self, chat_id: int, text: str, **kwargs) -> Awaitable[object]: ...


async def send_notifications(
    bot: BotLike,
    pending: list[PendingNotification],
    *,
    on_blocked: Callable[[int], Awaitable[None]] | None = None,
) -> int:
    """Шлет уведомления последовательно; возвращает число доставленных."""
    sent = 0
    last_sent_per_chat: dict[int, float] = {}

    for note in pending:
        elapsed = time.monotonic() - last_sent_per_chat.get(note.chat_id, 0.0)
        if elapsed < PER_CHAT_DELAY_SECONDS:
            await asyncio.sleep(PER_CHAT_DELAY_SECONDS - elapsed)

        text = format_notification(note)
        try:
            try:
                await bot.send_message(note.chat_id, text)
            except TelegramRetryAfter as exc:
                logger.warning("rate limited, retry after %s s", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
                await bot.send_message(note.chat_id, text)
            sent += 1
            last_sent_per_chat[note.chat_id] = time.monotonic()
        except TelegramForbiddenError:
            logger.info("user %s blocked the bot", note.chat_id)
            if on_blocked is not None:
                await on_blocked(note.chat_id)
        except Exception:
            logger.exception(
                "failed to send notification to %s (listing %s)", note.chat_id, note.listing_id
            )

        await asyncio.sleep(GLOBAL_DELAY_SECONDS)

    return sent
