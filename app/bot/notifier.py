"""Отправка уведомлений с учетом лимитов Telegram.

Лимиты: ~30 сообщений/сек глобально, ~1 сообщение/сек в один чат.
Принимает любой объект с async send_message(...)/send_photo(...) — в тестах
используется фейк.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from app.bot.digest import DIGEST_MAX_ITEMS, LEGACY_DIGEST_MAX_ITEMS, order_digest
from app.bot.keyboards import block_menu_kb, card_kb, digest_kb, with_rows
from app.bot.matching import PendingNotification
from app.bot.messages import (
    format_digest,
    format_digest_legacy,
    format_notification,
    format_notification_legacy,
)

logger = logging.getLogger(__name__)

GLOBAL_DELAY_SECONDS = 0.05  # ~20 сообщений/сек < лимита 30/сек
PER_CHAT_DELAY_SECONDS = 1.05  # чуть больше лимита 1 сообщение/сек в чат
# Ответ на действие человека (кнопка «Показать с фото», /best, витрина, превью):
# 2–3 карточки в один чат, внахлёст — следующая стартует через столько после
# предыдущей, не дожидаясь её. Лимит «1/с в чат» — для массовых рассылок;
# короткие всплески Telegram допускает (FAQ Bot API), на 429 ждём и повторяем.
# Замер 22.10.2026: send_photo по ссылке — ~1 с (Telegram качает фото с сайта
# источника), последовательно 3 карточки шли 3.5–4 с.
INTERACTIVE_DELAY_SECONDS = 0.25
# Кэш file_id: фото, уже скачанное Telegram, уходит повторно мгновенно (витрина
# одна на всех, одно объявление — нескольким людям за цикл). Память процесса:
# после рестарта первая отправка снова по ссылке.
PHOTO_CACHE_SIZE = 5000
_photo_ids: OrderedDict[str, str] = OrderedDict()
# Лимит подписи к фото в Bot API. Считается по видимому тексту, наш HTML
# длиннее видимого — сравнение по нему с запасом. Длиннее — шлём текстом.
CAPTION_MAX_LENGTH = 1024
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
    # Ссылка на фото: есть — уходит send_photo с подписью `text`, при отказе
    # Telegram (битая ссылка, снятое фото) — обычным текстом.
    photo_url: str | None = None
    # Объявления дайджеста — им до отправки ставится номер пачки (кнопка).
    digest_listing_ids: tuple[int, ...] = ()


def _card(note: PendingNotification, *, rich: bool = False, legacy: bool = False) -> OutgoingMessage:
    """Карточка объявления.

    `legacy` — вид до 22.10.2026 (`BOT_RICH_CARDS_PER_CYCLE=0`): прежний текст
    и кнопка «не показывать такие» с двухшаговым меню. Иначе — новый текст и
    «Не то» одним тапом; `rich` — ещё и фото, если оно есть.
    """
    if legacy:
        # Кнопка «не показывать такие» живёт под самой карточкой: это единственный
        # момент, когда человек точно знает, что именно ему надоело.
        return OutgoingMessage(
            chat_id=note.chat_id,
            text=format_notification_legacy(note),
            reply_markup=block_menu_kb(note.listing_id),
            listing_id=note.listing_id,
        )
    text = format_notification(note)
    photo_url = note.image_url if rich and len(text) <= CAPTION_MAX_LENGTH else None
    # «Все фото» — только у карточки с фото и только если фото не одно (None —
    # галерея неизвестна, кнопка её дотянет).
    photos = photo_url is not None and (note.photo_count is None or note.photo_count > 1)
    return OutgoingMessage(
        chat_id=note.chat_id,
        text=text,
        reply_markup=card_kb(note.listing_id, note.url, photos=photos),
        listing_id=note.listing_id,
        photo_url=photo_url,
    )


def build_outgoing(
    pending: list[PendingNotification],
    *,
    group_min: int = 0,
    rich_cards: int = 0,
    batch_id: int | None = None,
) -> list[OutgoingMessage]:
    """Раскладывает совпадения по сообщениям.

    `rich_cards=0` — карточки в прежнем виде (откат флагом). `rich_cards>0` —
    новый вид, и первые столько объявлений каждого человека (топ: порядок уже
    задан ранжированием в `matching.py`) уходят карточкой с фото; остальное —
    как раньше, текстом или списком.

    `group_min=0` — по сообщению на объявление (поведение по умолчанию).
    `group_min>0` — новый вид: если сверх топа набралось столько и больше,
    всё «остальное» уходит ОДНИМ списком с разделами по маркам и одной кнопкой
    «Показать с фото» (при известном `batch_id`; превью и витрина шлют без
    него — кнопки нет); меньше — карточками. Прежний вид — список на каждую
    марку от `group_min` штук, без кнопки.

    Порядок результата — по кругу между чатами: соседние сообщения уходят
    разным людям, поэтому пауза «не чаще 1/с в один чат» почти не срабатывает
    и рассылка не встаёт из-за одного получателя.
    """
    legacy = rich_cards <= 0
    by_chat: dict[int, list[PendingNotification]] = {}
    for note in pending:
        by_chat.setdefault(note.chat_id, []).append(note)

    per_chat: dict[int, list[OutgoingMessage]] = {}
    for chat_id, all_notes in by_chat.items():
        top = [] if legacy else all_notes[:rich_cards]
        notes = all_notes[len(top) :]
        top_cards = [_card(note, rich=True) for note in top]
        if group_min <= 0:
            per_chat[chat_id] = top_cards + [_card(note, legacy=legacy) for note in notes]
        elif legacy:
            per_chat[chat_id] = _legacy_digests(chat_id, notes, group_min)
        elif len(notes) >= group_min:
            per_chat[chat_id] = top_cards + _digest(chat_id, notes, batch_id)
        else:
            per_chat[chat_id] = top_cards + [_card(note) for note in notes]

    result: list[OutgoingMessage] = []
    queues = list(per_chat.values())
    for round_index in range(max((len(queue) for queue in queues), default=0)):
        for queue in queues:
            if round_index < len(queue):
                result.append(queue[round_index])
    return result


def _digest(chat_id: int, notes: list[PendingNotification], batch_id: int | None) -> list[OutgoingMessage]:
    """Всё сверх топа — одним списком с разделами по маркам (вариант Б, 22.10.2026).

    Длинный список режется по DIGEST_MAX_ITEMS, но кнопка одна — под последней
    частью: она ведёт по всему списку и после нажатия переезжает вниз, под
    присланные карточки. Номер пачки нужен всем частям — им помечаются строки.
    """
    ordered = order_digest(notes, key=lambda n: (n.brand_name, n.below_market_percent, n.listing_id))
    brand_counts = Counter(note.brand_name for note in ordered)
    messages = []
    for start in range(0, len(ordered), DIGEST_MAX_ITEMS):
        chunk = ordered[start : start + DIGEST_MAX_ITEMS]
        messages.append(
            OutgoingMessage(
                chat_id=chat_id,
                text=format_digest(
                    chunk, total=len(ordered), start_index=start + 1, brand_counts=brand_counts
                ),
                digest_listing_ids=tuple(note.listing_id for note in chunk)
                if batch_id is not None
                else (),
            )
        )
    if batch_id is not None:
        messages[-1].reply_markup = digest_kb(batch_id, 0)
    return messages


def _legacy_digests(chat_id: int, notes: list[PendingNotification], group_min: int) -> list[OutgoingMessage]:
    """Прежняя группировка (откат флагом): список на марку от group_min штук, без кнопки."""
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
        for start in range(0, len(items), LEGACY_DIGEST_MAX_ITEMS):
            chunk = items[start : start + LEGACY_DIGEST_MAX_ITEMS]
            messages.append(OutgoingMessage(chat_id=chat_id, text=format_digest_legacy(brand_name, chunk)))
    order = {id(note): index for index, note in enumerate(notes)}
    singles.sort(key=lambda note: order[id(note)])
    return messages + [_card(note, legacy=True) for note in singles]


class BotLike(Protocol):
    def send_message(self, chat_id: int, text: str, **kwargs) -> Awaitable[object]: ...

    def send_photo(self, chat_id: int, photo: str, **kwargs) -> Awaitable[object]: ...


def _remember_photo(url: str, sent: object) -> None:
    photos = getattr(sent, "photo", None)
    if photos:
        remember_photo_id(url, photos[-1].file_id)


# Кэш общий с «📷 Все фото» (handlers.photos_cb): главное фото альбома — та же
# ссылка, что у карточки, и повторный альбом не качается с CDN заново.
def cached_photo_id(url: str) -> str | None:
    return _photo_ids.get(url)


def remember_photo_id(url: str, file_id: str) -> None:
    _photo_ids[url] = file_id
    _photo_ids.move_to_end(url)
    while len(_photo_ids) > PHOTO_CACHE_SIZE:
        _photo_ids.popitem(last=False)


def forget_photo_ids(urls: list[str]) -> None:
    for url in urls:
        _photo_ids.pop(url, None)


async def _send_photo(bot: BotLike, message: OutgoingMessage) -> None:
    """send_photo по file_id из кэша, иначе по ссылке (и запомнить file_id).

    Отвергнутый file_id (Telegram его забыл) — не повод терять фото: забываем
    и повторяем по ссылке.
    """
    url = message.photo_url
    cached = _photo_ids.get(url)
    if cached is not None:
        try:
            await bot.send_photo(
                message.chat_id, photo=cached, caption=message.text, reply_markup=message.reply_markup
            )
            return
        except TelegramBadRequest as exc:
            if is_gone_chat(exc):
                raise
            _photo_ids.pop(url, None)
    sent = await bot.send_photo(
        message.chat_id, photo=url, caption=message.text, reply_markup=message.reply_markup
    )
    _remember_photo(url, sent)


async def _deliver(bot: BotLike, message: OutgoingMessage) -> None:
    """Одна отправка: фото с подписью, при отказе Telegram в фото — текстом.

    Ошибку фото разбираем ДО общей ветки «наш баг в запросе»: битая или
    снятая с CDN картинка — не баг, и карточка должна дойти хотя бы текстом.
    «Чата больше нет» пробрасываем — текст туда тоже не дойдёт.
    """
    if message.photo_url:
        try:
            await _send_photo(bot, message)
            return
        except TelegramBadRequest as exc:
            if is_gone_chat(exc):
                raise
            logger.warning(
                "photo rejected for listing %s (%s), sending as text", message.listing_id, exc
            )
    await bot.send_message(message.chat_id, message.text, reply_markup=message.reply_markup)


async def send_notifications(
    bot: BotLike,
    pending: list[PendingNotification],
    *,
    on_blocked: Callable[[int], Awaitable[None]] | None = None,
    group_min: int = 0,
    rich_cards: int = 0,
    batch_id: int | None = None,
    on_digest: Callable[[int, list[tuple[int, list[int]]]], Awaitable[bool]] | None = None,
    tail_markup: InlineKeyboardMarkup | None = None,
    interactive: bool = False,
) -> int:
    """Шлет уведомления последовательно; возвращает число ОТПРАВЛЕННЫХ сообщений.

    При группировке (`group_min > 0`) сообщений меньше, чем объявлений, —
    возвращаемое число это подчёркивает: считаем сообщения, а не объявления.

    `on_digest(batch_id, [(chat_id, listing_ids), …])` — пометить строки
    дайджестов номером пачки ДО отправки: кнопка «Показать с фото» читает их
    из базы. Не вышло — списки уходят без кнопки: кнопка «в никуда» хуже.

    `tail_markup` — строки, дописываемые под ПОСЛЕДНЕЕ сообщение (кнопка
    дайджеста переезжает вниз, под присланные по ней карточки).

    `interactive` — ответ на действие человека: сообщения стартуют внахлёст
    с шагом INTERACTIVE_DELAY_SECONDS, а последнее (на нём кнопка «Ещё») — только
    после всех остальных, чтобы точно оказаться внизу. Иначе — рассылка:
    строго по очереди, не чаще PER_CHAT_DELAY_SECONDS в один чат.
    """
    sent = 0
    last_sent_per_chat: dict[int, float] = {}
    # Чаты, отвалившиеся прямо в этом цикле: очередь собрана заранее, и без
    # этого мы бы честно отстояли PER_CHAT_DELAY_SECONDS перед каждой из
    # оставшихся отправок в пустоту (до потолка на цикл штук на чат).
    gone_chats: set[int] = set()

    messages = build_outgoing(
        pending, group_min=group_min, rich_cards=rich_cards, batch_id=batch_id
    )
    if tail_markup is not None and messages:
        messages[-1].reply_markup = with_rows(messages[-1].reply_markup, tail_markup.inline_keyboard)
    digests = [message for message in messages if message.digest_listing_ids]
    if digests:
        tagged = False
        if batch_id is not None and on_digest is not None:
            try:
                tagged = await on_digest(
                    batch_id,
                    [(message.chat_id, list(message.digest_listing_ids)) for message in digests],
                )
            except Exception:
                logger.exception("failed to tag digest rows with batch %s", batch_id)
        if not tagged:
            for message in digests:
                message.reply_markup = None

    async def send_one(message: OutgoingMessage) -> bool:
        if message.chat_id in gone_chats:
            return False
        try:
            try:
                await _deliver(bot, message)
            except TelegramRetryAfter as exc:
                if interactive:
                    # Ответ на нажатие (п. К плана 01.11.2026): не спим и не
                    # повторяем — при флуде одним человеком копились спящие
                    # задачи, а рассылка всем ждала ту же квоту. Повтор — только
                    # у рассылки.
                    logger.warning(
                        "rate limited on interactive send to %s, dropped (retry after %s s)",
                        message.chat_id,
                        exc.retry_after,
                    )
                    return False
                logger.warning("rate limited, retry after %s s", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
                await _deliver(bot, message)
            return True
        except TelegramForbiddenError:
            logger.info("user %s blocked the bot", message.chat_id)
            await mark_gone(message.chat_id)
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
                await mark_gone(message.chat_id)
        except Exception:
            logger.exception(
                "failed to send notification to %s (listing %s)",
                message.chat_id,
                message.listing_id,
            )
        return False

    async def mark_gone(chat_id: int) -> None:
        # Внахлёст две отправки в один мёртвый чат падают почти разом —
        # отключаем получателя один раз.
        if chat_id in gone_chats:
            return
        gone_chats.add(chat_id)
        if on_blocked is not None:
            await on_blocked(chat_id)

    if interactive:
        head, last = messages[:-1], messages[-1:]
        tasks = []
        for index, message in enumerate(head):
            if index:
                await asyncio.sleep(INTERACTIVE_DELAY_SECONDS)
            tasks.append(asyncio.create_task(send_one(message)))
        results = list(await asyncio.gather(*tasks))
        if last:
            results.append(await send_one(last[0]))
        return sum(results)

    for message in messages:
        if message.chat_id in gone_chats:
            continue
        elapsed = time.monotonic() - last_sent_per_chat.get(message.chat_id, 0.0)
        if elapsed < PER_CHAT_DELAY_SECONDS:
            await asyncio.sleep(PER_CHAT_DELAY_SECONDS - elapsed)
        if await send_one(message):
            sent += 1
            last_sent_per_chat[message.chat_id] = time.monotonic()
        await asyncio.sleep(GLOBAL_DELAY_SECONDS)

    return sent
