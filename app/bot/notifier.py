"""Отправка уведомлений с учетом лимитов Telegram.

Лимиты: ~30 сообщений/сек глобально, ~1 сообщение/сек в один чат.
Принимает любой объект с async send_message(...) — в тестах используется фейк.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from app.bot.keyboards import block_menu_kb
from app.bot.matching import PendingNotification
from app.bot.messages import format_digest, format_notification

logger = logging.getLogger(__name__)

GLOBAL_DELAY_SECONDS = 0.05  # ~20 сообщений/сек < лимита 30/сек
PER_CHAT_DELAY_SECONDS = 1.05  # чуть больше лимита 1 сообщение/сек в чат
# Потолок позиций в одном сгруппированном сообщении: у Telegram лимит 4096
# символов на сообщение, строка занимает ~120. Сверх — следующим сообщением.
DIGEST_MAX_ITEMS = 10
# Тексты Bot API, означающие «чата больше нет»: удалённый аккаунт, кик из
# группы. Прилетают как TelegramBadRequest, а не TelegramForbiddenError, и
# сами по себе не повторяемы — адресата больше не существует.
#
# Сверяем именно по тексту, а не ловим TelegramBadRequest целиком: под то же
# исключение попадают НАШИ баги (битая разметка, сообщение длиннее 4096), и
# деактивация по ним молча отписала бы всю базу от одной ошибки форматирования.
GONE_CHAT_MARKERS = (
    "chat not found",
    "user is deactivated",
    "bot was kicked",
    "peer_id_invalid",
)


def is_gone_chat(error: TelegramBadRequest) -> bool:
    """Мёртвый ли адресат (в отличие от нашей же ошибки в запросе)."""
    text = str(error).lower()
    return any(marker in text for marker in GONE_CHAT_MARKERS)


@dataclass(slots=True)
class OutgoingMessage:
    """Готовое к отправке сообщение: одна карточка или список одной марки."""

    chat_id: int
    text: str
    reply_markup: object | None = None
    listing_id: int | None = None  # для лога; у сгруппированного None


def _card(note: PendingNotification) -> OutgoingMessage:
    # Кнопка «не показывать такие» живёт под самой карточкой: это единственный
    # момент, когда человек точно знает, что именно ему надоело.
    return OutgoingMessage(
        chat_id=note.chat_id,
        text=format_notification(note),
        reply_markup=block_menu_kb(note.listing_id),
        listing_id=note.listing_id,
    )


def build_outgoing(
    pending: list[PendingNotification], *, group_min: int = 0
) -> list[OutgoingMessage]:
    """Раскладывает совпадения по сообщениям.

    `group_min=0` — по сообщению на объявление (поведение по умолчанию).
    `group_min>0` — авто одной марки, которых у человека за цикл набралось
    столько и больше, уезжают одним списком; остальные идут карточками.

    Порядок результата — по кругу между чатами: соседние сообщения уходят
    разным людям, поэтому пауза «не чаще 1/с в один чат» почти не срабатывает
    и рассылка не встаёт из-за одного получателя.
    """
    by_chat: dict[int, list[PendingNotification]] = {}
    for note in pending:
        by_chat.setdefault(note.chat_id, []).append(note)

    per_chat: dict[int, list[OutgoingMessage]] = {}
    for chat_id, notes in by_chat.items():
        if group_min <= 0:
            per_chat[chat_id] = [_card(note) for note in notes]
            continue

        by_brand: dict[str, list[PendingNotification]] = {}
        for note in notes:
            by_brand.setdefault(note.brand_name or "", []).append(note)

        messages: list[OutgoingMessage] = []
        singles: list[PendingNotification] = []
        for brand_name, items in by_brand.items():
            # Без марки (нераспознанное объявление) группировать не по чему.
            if not brand_name or len(items) < group_min:
                singles.extend(items)
                continue
            for start in range(0, len(items), DIGEST_MAX_ITEMS):
                chunk = items[start : start + DIGEST_MAX_ITEMS]
                messages.append(
                    OutgoingMessage(chat_id=chat_id, text=format_digest(brand_name, chunk))
                )
        order = {id(note): index for index, note in enumerate(notes)}
        singles.sort(key=lambda note: order[id(note)])
        per_chat[chat_id] = messages + [_card(note) for note in singles]

    result: list[OutgoingMessage] = []
    queues = list(per_chat.values())
    for round_index in range(max((len(queue) for queue in queues), default=0)):
        for queue in queues:
            if round_index < len(queue):
                result.append(queue[round_index])
    return result


class BotLike(Protocol):
    def send_message(self, chat_id: int, text: str, **kwargs) -> Awaitable[object]: ...


async def send_notifications(
    bot: BotLike,
    pending: list[PendingNotification],
    *,
    on_blocked: Callable[[int], Awaitable[None]] | None = None,
    group_min: int = 0,
) -> int:
    """Шлет уведомления последовательно; возвращает число ОТПРАВЛЕННЫХ сообщений.

    При группировке (`group_min > 0`) сообщений меньше, чем объявлений, —
    возвращаемое число это подчёркивает: считаем сообщения, а не объявления.
    """
    sent = 0
    last_sent_per_chat: dict[int, float] = {}
    # Чаты, отвалившиеся прямо в этом цикле: очередь собрана заранее, и без
    # этого мы бы честно отстояли PER_CHAT_DELAY_SECONDS перед каждой из
    # оставшихся отправок в пустоту (до потолка на цикл штук на чат).
    gone_chats: set[int] = set()

    for message in build_outgoing(pending, group_min=group_min):
        if message.chat_id in gone_chats:
            continue
        elapsed = time.monotonic() - last_sent_per_chat.get(message.chat_id, 0.0)
        if elapsed < PER_CHAT_DELAY_SECONDS:
            await asyncio.sleep(PER_CHAT_DELAY_SECONDS - elapsed)

        try:
            try:
                await bot.send_message(
                    message.chat_id, message.text, reply_markup=message.reply_markup
                )
            except TelegramRetryAfter as exc:
                logger.warning("rate limited, retry after %s s", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
                await bot.send_message(
                    message.chat_id, message.text, reply_markup=message.reply_markup
                )
            sent += 1
            last_sent_per_chat[message.chat_id] = time.monotonic()
        except TelegramForbiddenError:
            logger.info("user %s blocked the bot", message.chat_id)
            gone_chats.add(message.chat_id)
            if on_blocked is not None:
                await on_blocked(message.chat_id)
        except TelegramBadRequest as exc:
            if not is_gone_chat(exc):
                # Наш собственный баг в запросе — деактивировать нельзя.
                logger.exception(
                    "bad request sending to %s (listing %s)",
                    message.chat_id,
                    message.listing_id,
                )
            else:
                logger.info("chat %s is gone (%s)", message.chat_id, exc)
                gone_chats.add(message.chat_id)
                if on_blocked is not None:
                    await on_blocked(message.chat_id)
        except Exception:
            logger.exception(
                "failed to send notification to %s (listing %s)",
                message.chat_id,
                message.listing_id,
            )

        await asyncio.sleep(GLOBAL_DELAY_SECONDS)

    return sent
