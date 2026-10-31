"""Витрина и превью (правки 26.10.2026 по итогам ручной проверки).

Было: /start показывал три лучшие машины, превью после триала и «Лучшее сейчас»
— те же три (все сортируют одно и то же по выгоде). «Показать ещё?» было отдельным
узким сообщением, и кнопка под ним — узкой. Стало: показанное чату запоминается,
кнопка «ещё» стоит под последней карточкой.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
from decimal import Decimal
from types import SimpleNamespace

import app.bot.db as botdb
from app.bot import showcase
from app.bot.keyboards import BestCB, HideCB, UnhideCB, best_more_kb, card_kb, more_rows, with_rows
from app.bot.matching import PendingNotification, preview_for_filter
from tests.test_bot_matching import (
    _add_listing,
    _add_stat,
    _add_user_with_filter,
    _session,
)


def _note(listing_id: int, chat_id: int = 42) -> PendingNotification:
    return PendingNotification(
        chat_id=chat_id,
        listing_id=listing_id,
        filter_id=0,
        title="Toyota Camry",
        year=2018,
        mileage_km=90_000,
        price_usd=10_000.0,
        city="Бишкек",
        url=f"https://www.mashina.kg/details/x-{listing_id}/",
        below_market_percent=20.0,
        market_avg_usd=12_500.0,
        brand_name="Toyota",
        model_name="Camry",
        image_url=f"https://img/{listing_id}.webp",
    )


# --- что уже показано чату ----------------------------------------------------


def test_seen_is_per_chat_and_expires(monkeypatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(showcase.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(showcase, "_seen", {})

    showcase.mark_seen(1, [10, 11])
    showcase.mark_seen(1, [12])
    assert showcase.seen_ids(1) == {10, 11, 12}
    assert showcase.seen_ids(2) == set()

    clock["now"] += showcase.SEEN_TTL_SECONDS + 1
    assert showcase.seen_ids(1) == set()


def test_first_unseen_offset_moves_forward_then_wraps(monkeypatch) -> None:
    monkeypatch.setattr(showcase, "_seen", {})
    items = [_note(i) for i in range(6)]
    assert showcase.first_unseen_offset(items, 42) == 0
    showcase.mark_seen(42, [0, 1, 2])
    assert showcase.first_unseen_offset(items, 42) == 3
    showcase.mark_seen(42, [3, 4, 5])
    assert showcase.first_unseen_offset(items, 42) == 0  # всё показано — сначала


def test_get_showcase_skip_seen_starts_after_shown(monkeypatch) -> None:
    items = [_note(i) for i in range(12)]
    monkeypatch.setattr(botdb, "_get_showcase", lambda *a, **k: items)
    monkeypatch.setattr(showcase, "_seen", {})
    first, nxt = botdb.get_showcase(42, 0, skip_seen=True)
    assert [n.listing_id for n in first] == [0, 1, 2] and nxt == 3
    showcase.mark_seen(42, [n.listing_id for n in first])
    second, nxt = botdb.get_showcase(42, 0, skip_seen=True)
    assert [n.listing_id for n in second] == [3, 4, 5] and nxt == 6
    # Без skip_seen (/start, кнопка «ещё» с явным смещением) — как раньше.
    assert [n.listing_id for n in botdb.get_showcase(42, 0)[0]] == [0, 1, 2]


# --- превью не повторяет витрину ----------------------------------------------


def _preview_world():
    session = _session()
    _add_stat(session, avg="12000", samples=10)
    user_filter = _add_user_with_filter(session, below_market_only=False)
    listings = [
        _add_listing(session, external_id=str(i), price_usd=Decimal(7000 + i * 500))
        for i in range(4)
    ]
    return session, user_filter, listings


def test_preview_skips_listings_already_shown() -> None:
    session, user_filter, listings = _preview_world()
    shown = {listings[0].id, listings[1].id}  # самые выгодные уже ушли витриной
    preview = preview_for_filter(session, user_filter.id, limit=3, exclude_listing_ids=frozenset(shown))
    assert {n.listing_id for n in preview}.isdisjoint(shown)
    assert len(preview) == 2


def test_preview_without_exclusion_is_unchanged() -> None:
    session, user_filter, listings = _preview_world()
    preview = preview_for_filter(session, user_filter.id, limit=3)
    assert len(preview) == 3


def test_db_preview_passes_exclusions_through(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        botdb, "_preview_for_filter", lambda session, filter_id, **kwargs: captured.update(kwargs) or []
    )
    # SessionLocal нужен здесь только как контекст-менеджер.
    monkeypatch.setattr(botdb, "SessionLocal", lambda: contextlib.nullcontext(object()))
    botdb.preview_for_filter(5, {7, 8})
    assert captured["exclude_listing_ids"] == frozenset({7, 8})


# --- «ещё» стоит под последней карточкой --------------------------------------


class _Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(("text", kwargs.get("reply_markup")))

    async def send_photo(self, chat_id, photo, **kwargs):
        self.sent.append(("photo", kwargs.get("reply_markup")))
        return SimpleNamespace(photo=[SimpleNamespace(file_id=f"id:{photo}")])


class _Message:
    def __init__(self, reply_markup=None) -> None:
        self.bot = _Bot()
        self.chat = SimpleNamespace(id=42)
        self.reply_markup = reply_markup
        self.answers: list[str] = []
        self.markups: list[object] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        self.markups.append(reply_markup)
        self.reply_markup = reply_markup


def test_showcase_more_button_is_under_last_card_not_a_separate_message(monkeypatch) -> None:
    from app.bot import handlers

    monkeypatch.setattr("app.bot.notifier.INTERACTIVE_DELAY_SECONDS", 0)
    monkeypatch.setattr("app.bot.notifier._photo_ids", collections.OrderedDict())
    monkeypatch.setattr(showcase, "_seen", {})
    page = [_note(i) for i in range(3)]
    monkeypatch.setattr(handlers.db, "get_showcase", lambda chat_id, offset, skip_seen=False: (page, 3))
    message = _Message()
    asyncio.run(handlers._send_showcase_page(message, offset=0, fresh=True))

    assert "Показать ещё?" not in message.answers  # отдельного узкого сообщения нет
    sent = message.bot.sent
    assert [kind for kind, _ in sent] == ["photo", "photo", "photo"]
    assert more_rows(sent[0][1]) == [] and more_rows(sent[1][1]) == []
    [row] = more_rows(sent[2][1])
    assert row[0].callback_data == BestCB(offset=3).pack()
    assert showcase.seen_ids(42) == {0, 1, 2}


def test_last_page_has_no_more_button(monkeypatch) -> None:
    from app.bot import handlers

    monkeypatch.setattr("app.bot.notifier.INTERACTIVE_DELAY_SECONDS", 0)
    monkeypatch.setattr(showcase, "_seen", {})
    monkeypatch.setattr(handlers.db, "get_showcase", lambda chat_id, offset, skip_seen=False: ([_note(9)], None))
    message = _Message()
    asyncio.run(handlers._send_showcase_page(message, offset=9))
    assert more_rows(message.bot.sent[-1][1]) == []


def test_pressing_more_strips_only_its_row(monkeypatch) -> None:
    from app.bot import handlers

    monkeypatch.setattr("app.bot.notifier.INTERACTIVE_DELAY_SECONDS", 0)
    monkeypatch.setattr(showcase, "_seen", {})
    monkeypatch.setattr(handlers.db, "get_showcase", lambda chat_id, offset, skip_seen=False: ([_note(5)], None))
    card = with_rows(card_kb(3, "https://x/3"), best_more_kb(3).inline_keyboard)
    message = _Message(reply_markup=card)
    query = SimpleNamespace(message=message, from_user=SimpleNamespace(id=42), answer=_noop)
    asyncio.run(handlers.cb_best_more(query, BestCB(offset=3)))
    assert message.markups == [card_kb(3, "https://x/3")]  # «Не то» и «Открыть» целы


async def _noop(*args, **kwargs):
    return None


def test_hide_and_unhide_keep_the_showcase_button(monkeypatch) -> None:
    from app.bot import handlers

    monkeypatch.setattr(
        handlers.db, "hide_from_listing",
        lambda tid, lid: botdb.BlockResult(ok=True, label="Toyota Camry", block_id=1),
    )
    monkeypatch.setattr(handlers.db, "delete_block", lambda tid, bid: True)
    card = with_rows(card_kb(3, "https://x/3"), best_more_kb(3).inline_keyboard)
    message = _Message(reply_markup=card)
    query = SimpleNamespace(message=message, from_user=SimpleNamespace(id=42), answer=_noop)

    asyncio.run(handlers.hide_cb(query, HideCB(listing_id=3)))
    assert more_rows(message.reply_markup)[0][0].callback_data == BestCB(offset=3).pack()
    assert message.reply_markup.inline_keyboard[0][0].text.startswith("↩️ Вернуть")

    asyncio.run(handlers.unhide_cb(query, UnhideCB(block_id=1, listing_id=3)))
    assert message.reply_markup.inline_keyboard == card.inline_keyboard
