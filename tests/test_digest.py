"""Дайджест (план 22.10.2026, этап 2, вариант Б): топ-3 с фото + ОДИН список.

Всё сверх топа — одним списком с разделами по маркам и одной кнопкой «Показать
с фото», которая после нажатия переезжает под последнюю присланную карточку.
Главное: кнопка отдаёт ровно позиции списка в его порядке, не повторяет
показанное карточками, пропускает снятые и честно говорит, когда нечего.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.bot.db as botdb
from app.bot.digest import DIGEST_MAX_ITEMS, batch_id_for, order_digest
from app.bot.keyboards import DigestCB, card_kb, digest_kb, digest_rows, with_rows
from app.bot.matching import PendingNotification
from app.bot.messages import format_digest, format_digest_legacy
from app.bot.notifier import build_outgoing, send_notifications
from app.db.base import Base
from app.models import BotUser, Brand, CarModel, Listing, SentNotification, Source


def _note(listing_id: int, *, chat_id: int = 42, brand: str = "Toyota", brand_id: int = 7,
          percent: float | None = 20.0, **overrides) -> PendingNotification:
    values = dict(
        chat_id=chat_id,
        listing_id=listing_id,
        filter_id=1,
        title=f"{brand} Camry",
        year=2018,
        mileage_km=90_000,
        price_usd=10_000.0,
        city="Бишкек",
        url=f"https://www.mashina.kg/details/x-{listing_id}/",
        below_market_percent=percent,
        market_avg_usd=12_500.0,
        brand_name=brand,
        model_name="Camry",
        brand_id=brand_id,
        image_url=f"https://img/{listing_id}.webp",
    )
    values.update(overrides)
    return PendingNotification(**values)


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# --- раскладка ----------------------------------------------------------------


def test_everything_beyond_top_goes_into_one_sectioned_list() -> None:
    notes = [_note(i, percent=50 - i) for i in range(3)] + [
        _note(10, brand="Kia", brand_id=8, percent=21.0),
        _note(11, percent=35.0),
        _note(12, brand="Kia", brand_id=8, percent=40.0),
        _note(13, brand=None, brand_id=None, percent=45.0),  # без марки — в конец
        _note(14, percent=28.0),
    ]
    messages = build_outgoing(notes, rich_cards=3, group_min=3, batch_id=29_000_000)

    assert [m.photo_url is not None for m in messages] == [True, True, True, False]
    digest = messages[3]
    # Kia (лучшая −40%) раньше Toyota (лучшая −35%), «Другие марки» — последними.
    assert digest.digest_listing_ids == (12, 10, 11, 14, 13)
    assert digest.text.startswith("📋 <b>Ещё 5 объявлений по вашим фильтрам</b>")
    assert digest.text.index("🚗 <b>Kia</b> · 2") < digest.text.index("🚗 <b>Toyota</b> · 2")
    assert digest.text.index("🚗 <b>Toyota</b>") < digest.text.index("🚗 <b>Другие марки</b>")
    assert "5. <a" in digest.text  # нумерация сквозная
    assert _callbacks(digest.reply_markup) == [DigestCB(batch=29_000_000, offset=0).pack()]


def test_below_threshold_rest_goes_as_cards() -> None:
    notes = [_note(i) for i in range(5)]  # сверх топа 2 < порога 3
    messages = build_outgoing(notes, rich_cards=3, group_min=3, batch_id=1)
    assert len(messages) == 5 and not any(m.digest_listing_ids for m in messages)


def test_long_list_split_but_single_button_under_last_part() -> None:
    notes = [_note(i) for i in range(3)] + [_note(100 + i, percent=40 - i / 10) for i in range(DIGEST_MAX_ITEMS + 5)]
    messages = build_outgoing(notes, rich_cards=3, group_min=3, batch_id=1)
    parts = [m for m in messages if m.digest_listing_ids]
    assert [len(m.digest_listing_ids) for m in parts] == [DIGEST_MAX_ITEMS, 5]
    assert parts[0].reply_markup is None
    assert _callbacks(parts[1].reply_markup) == [DigestCB(batch=1, offset=0).pack()]
    assert parts[1].text.startswith("\n🚗 <b>Toyota</b>")  # продолжение: без «Ещё N», с маркой
    assert f"{DIGEST_MAX_ITEMS + 1}. <a" in parts[1].text


def test_no_batch_means_no_button() -> None:
    """Превью фильтра и витрина шлют без номера пачки — кнопке некуда вести."""
    notes = [_note(i) for i in range(6)]
    [*_, digest] = build_outgoing(notes, rich_cards=3, group_min=3)
    assert digest.reply_markup is None and digest.digest_listing_ids == ()


def test_rollback_flag_keeps_previous_per_brand_digest() -> None:
    notes = [_note(i, percent=15.0) for i in range(4)]
    [digest] = build_outgoing(notes, rich_cards=0, group_min=3, batch_id=1)
    assert digest.text == format_digest_legacy("Toyota", notes)
    assert "ниже средней" in digest.text
    assert digest.reply_markup is None and digest.photo_url is None


def test_digest_lines_follow_card_format() -> None:
    notes = [_note(1, percent=17.6), _note(2, percent=None, mileage_km=None, city=None)]
    text = format_digest(notes, total=2)
    assert '1. <a href="https://www.mashina.kg/details/x-1/">Toyota Camry · 2018</a>' in text
    assert "💰 $10 000 · на 18% ниже рынка · 90 000 км · Бишкек" in text
    assert "—" not in text  # без прочерков на месте пустых полей


def test_order_digest_is_shared_rule() -> None:
    items = [("Kia", 10.0, 1), ("Toyota", 30.0, 2), (None, 90.0, 3), ("Kia", 50.0, 4), ("Toyota", 30.0, 0)]
    assert [i[2] for i in order_digest(items, key=lambda item: item)] == [4, 1, 0, 2, 3]


# --- пометка пачки до отправки ------------------------------------------------


class _Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int, object]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(("text", chat_id, kwargs.get("reply_markup")))

    async def send_photo(self, chat_id, photo, **kwargs):
        self.sent.append(("photo", chat_id, kwargs.get("reply_markup")))


def _fast(monkeypatch) -> None:
    monkeypatch.setattr("app.bot.notifier.PER_CHAT_DELAY_SECONDS", 0)
    monkeypatch.setattr("app.bot.notifier.GLOBAL_DELAY_SECONDS", 0)


def test_digest_rows_are_tagged_before_sending(monkeypatch) -> None:
    _fast(monkeypatch)
    calls: list = []
    bot = _Bot()

    async def on_digest(batch_id, groups):
        calls.append((batch_id, groups, len(bot.sent)))
        return True

    notes = [_note(i) for i in range(5)]
    asyncio.run(send_notifications(bot, notes, rich_cards=3, group_min=2, batch_id=5, on_digest=on_digest))
    assert calls == [(5, [(42, [3, 4])], 0)]  # до первой отправки
    assert bot.sent[-1][2] is not None  # кнопка на месте


def test_failed_tagging_sends_digest_without_button(monkeypatch) -> None:
    _fast(monkeypatch)
    bot = _Bot()

    async def on_digest(batch_id, groups):
        raise RuntimeError("db down")

    notes = [_note(i) for i in range(5)]
    sent = asyncio.run(
        send_notifications(bot, notes, rich_cards=3, group_min=2, batch_id=5, on_digest=on_digest)
    )
    assert sent == 4
    assert bot.sent[-1] == ("text", 42, None)


# --- выдача по кнопке ---------------------------------------------------------


class _SharedSession:
    def __init__(self, session: Session) -> None:
        self.session = session

    def __call__(self):
        return self

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *exc) -> bool:
        return False


def _db(monkeypatch) -> tuple[Session, BotUser, int]:
    import app.models  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Source(name="mashina.kg"))
    session.add_all([Brand(name="Toyota"), Brand(name="Kia")])
    session.flush()
    toyota = session.scalar(select(Brand).where(Brand.name == "Toyota"))
    session.add(CarModel(brand_id=toyota.id, name="Camry"))
    user = BotUser(telegram_id=42)
    session.add_all([user, BotUser(telegram_id=43)])
    session.commit()
    monkeypatch.setattr(botdb, "SessionLocal", _SharedSession(session))
    return session, user, toyota.id


def _sent(session: Session, user: BotUser, *, brand: str = "Toyota", percent: float, batch: int | None,
          active: bool = True) -> Listing:
    brand_row = session.scalar(select(Brand).where(Brand.name == brand))
    listing = Listing(
        source_id=session.scalar(select(Source.id)),
        external_id=f"e{percent}{brand}{batch}{active}",
        brand_id=brand_row.id,
        car_model_id=session.scalar(select(CarModel.id)) if brand == "Toyota" else None,
        title=f"{brand} Camry",
        source_slug="slug",
        year=2018,
        mileage_km=90_000,
        city="Бишкек",
        current_price_usd=Decimal("10000"),
        is_active=active,
        image_url="https://img/x.webp",
        payload_hash="x",
    )
    session.add(listing)
    session.flush()
    session.add(
        SentNotification(
            user_id=user.id,
            listing_id=listing.id,
            filter_id=None,
            below_market_percent=Decimal(str(percent)),
            batch_id=batch,
        )
    )
    session.commit()
    return listing


def test_tag_digest_rows_marks_only_given_listings(monkeypatch) -> None:
    session, user, _ = _db(monkeypatch)
    first = _sent(session, user, percent=30, batch=None)
    second = _sent(session, user, percent=25, batch=None)
    assert botdb.tag_digest_rows(77, [(42, [first.id]), (999, [second.id])]) is True
    batches = dict(session.execute(select(SentNotification.listing_id, SentNotification.batch_id)).all())
    assert batches == {first.id: 77, second.id: None}


def test_digest_page_pages_in_list_order_and_skips_sold(monkeypatch) -> None:
    session, user, _ = _db(monkeypatch)
    batch = batch_id_for(datetime.now(timezone.utc))
    for percent in (40, 35, 30, 25):
        _sent(session, user, percent=percent, batch=batch, active=percent != 35)
    _sent(session, user, brand="Kia", percent=50, batch=batch)  # раздел Kia — первым
    _sent(session, user, percent=99, batch=None)  # топ-карточка той же пачки — не из списка

    first = botdb.digest_page(42, batch, 0)
    assert first.status == botdb.DIGEST_OK
    assert [(n.brand_name, n.below_market_percent) for n in first.notes] == [
        ("Kia", 50.0), ("Toyota", 40.0), ("Toyota", 30.0)
    ]
    assert first.notes[0].image_url and first.notes[0].chat_id == 42
    assert (first.next_offset, first.remaining) == (4, 1)

    second = botdb.digest_page(42, batch, first.next_offset)
    assert [n.below_market_percent for n in second.notes] == [25.0]
    assert second.next_offset is None

    # Чужой пользователь ту же пачку не видит.
    assert botdb.digest_page(43, batch, 0).status == botdb.DIGEST_EMPTY


def test_digest_page_expired_gone_empty(monkeypatch) -> None:
    session, user, _ = _db(monkeypatch)
    old = batch_id_for(datetime.now(timezone.utc) - timedelta(hours=25))
    assert botdb.digest_page(42, old, 0).status == botdb.DIGEST_EXPIRED

    batch = batch_id_for(datetime.now(timezone.utc))
    _sent(session, user, percent=30, batch=batch, active=False)
    assert botdb.digest_page(42, batch, 0).status == botdb.DIGEST_GONE
    assert botdb.digest_page(42, batch, 5).status == botdb.DIGEST_EMPTY


# --- хендлер: кнопка переезжает вниз -------------------------------------------


class _Message:
    def __init__(self, reply_markup=None) -> None:
        self.bot = _Bot()
        self.reply_markup = reply_markup
        self.markups: list[object] = []

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        self.markups.append(reply_markup)
        self.reply_markup = reply_markup


class _Query:
    def __init__(self, message: _Message | None = None) -> None:
        self.message = message or _Message()
        self.from_user = SimpleNamespace(id=42)
        self.alerts: list[str | None] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.alerts.append(text)


def test_button_moves_under_last_sent_card(monkeypatch) -> None:
    from app.bot import handlers

    _fast(monkeypatch)
    page = botdb.DigestPage(status=botdb.DIGEST_OK, notes=[_note(i) for i in range(3)], next_offset=3, remaining=4)
    monkeypatch.setattr(handlers.db, "digest_page", lambda *args: page)
    # Нажата под самим списком: там только эта кнопка.
    query = _Query(_Message(reply_markup=digest_kb(5, 0)))
    asyncio.run(handlers.digest_more_cb(query, DigestCB(batch=5, offset=0)))

    assert query.message.markups == [None]  # со списка кнопка снята
    sent = query.message.bot.sent
    assert [kind for kind, _, _ in sent] == ["photo", "photo", "photo"]
    assert digest_rows(sent[0][2]) == [] and digest_rows(sent[1][2]) == []
    [row] = digest_rows(sent[2][2])  # переехала под последнюю карточку
    assert row[0].callback_data == DigestCB(batch=5, offset=3).pack()
    assert "осталось 4" in row[0].text
    assert _callbacks(sent[2][2])[0].startswith("hide:")  # «Не то» на месте


def test_pressing_on_card_strips_only_the_moved_row(monkeypatch) -> None:
    from app.bot import handlers

    _fast(monkeypatch)
    page = botdb.DigestPage(status=botdb.DIGEST_OK, notes=[_note(9)], next_offset=None, remaining=0)
    monkeypatch.setattr(handlers.db, "digest_page", lambda *args: page)
    card = with_rows(card_kb(3, "https://x/3"), digest_kb(5, 3, 1).inline_keyboard)
    query = _Query(_Message(reply_markup=card))
    asyncio.run(handlers.digest_more_cb(query, DigestCB(batch=5, offset=3)))

    [left] = query.message.markups
    assert left.inline_keyboard == card_kb(3, "https://x/3").inline_keyboard
    assert digest_rows(query.message.bot.sent[-1][2]) == []  # последняя порция — без кнопки


def test_hide_keeps_moved_digest_button(monkeypatch) -> None:
    from app.bot import handlers
    from app.bot.keyboards import HideCB

    monkeypatch.setattr(
        handlers.db,
        "hide_from_listing",
        lambda tid, lid: botdb.BlockResult(ok=True, label="Toyota Camry", block_id=1),
    )
    card = with_rows(card_kb(3, "https://x/3"), digest_kb(5, 3, 1).inline_keyboard)
    query = _Query(_Message(reply_markup=card))
    asyncio.run(handlers.hide_cb(query, HideCB(listing_id=3)))
    [after] = query.message.markups
    assert after.inline_keyboard[0][0].text.startswith("↩️ Вернуть")
    assert digest_rows(after)[0][0].callback_data == DigestCB(batch=5, offset=3).pack()


def test_button_on_sold_out_list_says_so_and_disappears(monkeypatch) -> None:
    from app.bot import handlers
    from app.bot.messages import DIGEST_GONE_TEXT

    monkeypatch.setattr(handlers.db, "digest_page", lambda *args: botdb.DigestPage(status=botdb.DIGEST_GONE, notes=[]))
    query = _Query(_Message(reply_markup=digest_kb(5, 0)))
    asyncio.run(handlers.digest_more_cb(query, DigestCB(batch=5, offset=0)))
    assert query.alerts == [DIGEST_GONE_TEXT]
    assert query.message.markups == [None]
    assert query.message.bot.sent == []


def test_digest_callback_fits_telegram_limit() -> None:
    data = DigestCB(batch=batch_id_for(datetime(2030, 1, 1, tzinfo=timezone.utc)), offset=990).pack()
    assert len(data.encode()) <= 64


class _TimedBot:
    """Фиксирует момент старта и окончания каждой отправки; send_photo «качает» 0.05 с."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []
        self.photos: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.events.append(("text", chat_id))

    async def send_photo(self, chat_id, photo, **kwargs):
        caption = kwargs["caption"]
        self.events.append(("start", caption))
        await asyncio.sleep(0.05)
        self.events.append(("end", caption))
        self.photos.append(photo)
        return SimpleNamespace(photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id=f"id:{photo}")])


def test_interactive_cards_overlap_but_last_goes_after_all(monkeypatch) -> None:
    """Внахлёст: вторая стартует до конца первой; последняя (с кнопкой) — после всех."""
    monkeypatch.setattr("app.bot.notifier.INTERACTIVE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr("app.bot.notifier._photo_ids", __import__("collections").OrderedDict())
    bot = _TimedBot()
    notes = [_note(i, title=f"T{i}", brand_name=None, model_name=None, image_url=f"https://img/t{i}") for i in range(3)]
    sent = asyncio.run(send_notifications(bot, notes, rich_cards=3, interactive=True))
    assert sent == 3
    starts = [i for i, (kind, _) in enumerate(bot.events) if kind == "start"]
    ends = [i for i, (kind, _) in enumerate(bot.events) if kind == "end"]
    assert starts[1] < ends[0]  # вторая пошла, не дожидаясь первой
    assert starts[2] > max(ends[0], ends[1])  # последняя — после всех остальных


def test_photo_file_id_is_reused(monkeypatch) -> None:
    """Второй раз то же фото уходит по file_id, без повторного скачивания."""
    from aiogram.exceptions import TelegramBadRequest

    _fast(monkeypatch)
    monkeypatch.setattr("app.bot.notifier._photo_ids", __import__("collections").OrderedDict())
    bot = _TimedBot()
    note = _note(1, image_url="https://img/same.webp")
    asyncio.run(send_notifications(bot, [note], rich_cards=3))
    asyncio.run(send_notifications(bot, [_note(1, chat_id=43, image_url="https://img/same.webp")], rich_cards=3))
    assert bot.photos == ["https://img/same.webp", "id:https://img/same.webp"]

    # Telegram забыл file_id — повторяем по ссылке, карточка не теряет фото.
    class Forgetful(_TimedBot):
        async def send_photo(self, chat_id, photo, **kwargs):
            if photo.startswith("id:"):
                raise TelegramBadRequest(method=None, message="Bad Request: wrong file identifier")
            return await super().send_photo(chat_id, photo, **kwargs)

    forgetful = Forgetful()
    asyncio.run(send_notifications(forgetful, [note], rich_cards=3))
    assert forgetful.photos == ["https://img/same.webp"]
