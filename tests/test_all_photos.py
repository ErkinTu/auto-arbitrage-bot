"""Кнопка «📷 Все фото» (план 31.10.2026, п. 2.4): галерея в базе + запасной запрос.

Фикстуры — из `test_rich_cards` (урезанные живые ответы mashina/lalafo). Замер
31.10.2026: лента mashina отдаёт всю галерею (до 12), лента lalafo режет до 10,
в карточке lalafo — 11+; карточка ~7–13 КБ.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

import app.bot.db as botdb
import app.db.session as dbsession
from app.bot import handlers, photos
from app.bot.keyboards import PhotosCB, UnhideCB, card_kb, has_photos_button, hidden_kb
from app.bot.matching import PendingNotification
from app.bot.notifier import build_outgoing
from app.models import Listing
from app.services.images import MAX_GALLERY, extract_image_urls
from app.services.normalizer import normalize_lalafo_listing, normalize_mashina_listing
from app.services.sync import run_sync
from tests.test_rich_cards import (
    LALAFO_MAIN,
    LALAFO_OTHER,
    MASHINA_IMAGES,
    _FeedParser,
    _HandlerMessage,
    _lalafo_item,
    _lalafo_setup,
    _mashina_raw,
    _Query,
    _sync_session,
)

URL = "https://lalafo.kg/bishkek/ads/toyota-camry-id-101"

# --- извлечение галереи ----------------------------------------------------------


def test_gallery_main_first_without_duplicates() -> None:
    urls = extract_image_urls({"images": [LALAFO_OTHER, LALAFO_MAIN, LALAFO_OTHER]})
    assert urls == [LALAFO_MAIN["original_url"], LALAFO_OTHER["original_url"]]


def test_gallery_uses_same_size_as_the_card() -> None:
    assert extract_image_urls({"images": MASHINA_IMAGES}) == [i["medium"] for i in MASHINA_IMAGES]


def test_gallery_is_capped_and_handles_empty() -> None:
    many = [{"original_url": f"https://img.example/{n}.jpeg"} for n in range(MAX_GALLERY + 5)]
    assert len(extract_image_urls({"images": many})) == MAX_GALLERY
    assert extract_image_urls({"images": []}) == []
    assert extract_image_urls(None) == []
    assert extract_image_urls({"image_url": "https://img.example/one.jpeg"}) == [
        "https://img.example/one.jpeg"
    ]


def test_normalizers_fill_gallery_outside_payload_hash() -> None:
    mashina = normalize_mashina_listing(_mashina_raw())
    assert mashina.image_urls == [i["medium"] for i in MASHINA_IMAGES]
    assert mashina.payload_hash == normalize_mashina_listing(_mashina_raw(images=[])).payload_hash
    assert "image_urls" not in mashina.analytics_payload()
    lalafo = normalize_lalafo_listing(
        {"id": 1, "title": "Toyota Camry: 2016 г.", "images": [LALAFO_OTHER, LALAFO_MAIN]},
        fallback_make="Toyota",
    )
    assert lalafo.image_urls[0] == LALAFO_MAIN["original_url"]


def test_schema_upgrade_adds_image_urls_in_parsable_form() -> None:
    statement = "ALTER TABLE listings ADD COLUMN IF NOT EXISTS image_urls JSON"
    source = open(dbsession.__file__, encoding="utf-8").read()
    assert f'"{statement}"' in source
    assert dbsession._ADD_COLUMN_RE.match(statement).groups() == ("listings", "image_urls")


# --- синк ------------------------------------------------------------------------


def test_sync_stores_gallery_and_known_listing_gets_it_from_feed() -> None:
    session = _sync_session()
    source, target = _lalafo_setup(session)
    # Новое объявление: карточку тянем — галерея из неё.
    run_sync(
        session,
        _FeedParser([_lalafo_item(None)], {101: _lalafo_item([LALAFO_MAIN, LALAFO_OTHER])}),
        source,
        target,
        mode="fresh",
    )
    listing = session.scalar(select(Listing).where(Listing.external_id == "101"))
    assert listing.image_urls == [LALAFO_MAIN["original_url"], LALAFO_OTHER["original_url"]]

    # Цена та же — карточку не тянем; известная галерея не затирается лентой.
    parser = _FeedParser([_lalafo_item([LALAFO_MAIN])], {})
    run_sync(session, parser, source, target, mode="fresh")
    assert parser.detail_calls == []
    session.refresh(listing)
    assert len(listing.image_urls) == 2


def test_sync_old_listing_without_gallery_gets_it_from_feed_for_free() -> None:
    session = _sync_session()
    source, target = _lalafo_setup(session)
    run_sync(session, _FeedParser([_lalafo_item(None)], {101: _lalafo_item(None)}), source, target, mode="fresh")
    listing = session.scalar(select(Listing).where(Listing.external_id == "101"))
    assert listing.image_urls is None

    parser = _FeedParser([_lalafo_item([LALAFO_MAIN, LALAFO_OTHER])], {})
    run_sync(session, parser, source, target, mode="fresh")
    assert parser.detail_calls == []
    session.refresh(listing)
    assert len(listing.image_urls) == 2


# --- кнопка под карточкой --------------------------------------------------------


def _note(**overrides) -> PendingNotification:
    base = dict(
        chat_id=1,
        listing_id=42,
        filter_id=1,
        title="Toyota Camry",
        year=2016,
        mileage_km=100_000,
        price_usd=15_000.0,
        city="Бишкек",
        url=URL,
        below_market_percent=20.0,
        image_url=LALAFO_MAIN["original_url"],
    )
    base.update(overrides)
    return PendingNotification(**base)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, True),  # галерея неизвестна — кнопка её дотянет
        ({"photo_count": 5}, True),
        ({"photo_count": 1}, False),  # одно фото уже в карточке
        ({"image_url": None}, False),  # текстовая карточка
    ],
)
def test_photos_button_only_on_photo_cards_with_more_photos(overrides, expected) -> None:
    [message] = build_outgoing([_note(**overrides)], rich_cards=3)
    assert has_photos_button(message.reply_markup) is expected


def test_photos_button_is_not_on_text_cards_beyond_the_top() -> None:
    notes = [_note(listing_id=n, photo_count=5) for n in range(1, 5)]
    messages = build_outgoing(notes, rich_cards=3)
    assert [has_photos_button(m.reply_markup) for m in messages] == [True, True, True, False]


def test_photos_button_survives_hide_and_unhide(monkeypatch) -> None:
    card = card_kb(42, URL, photos=True)
    hidden = hidden_kb("Toyota Camry", 5, 42, URL, photos=has_photos_button(card))
    assert has_photos_button(hidden)

    monkeypatch.setattr(handlers.db, "delete_block", lambda telegram_id, block_id: True)
    message = _HandlerMessage(reply_markup=hidden)
    asyncio.run(handlers.unhide_cb(_Query(message), UnhideCB(block_id=5, listing_id=42)))
    assert message.reply_markup.inline_keyboard == card.inline_keyboard


def test_photos_callback_fits_telegram_limit() -> None:
    assert len(PhotosCB(listing_id=2_147_483_647).pack().encode()) <= 64


# --- альбомы, дебаунс, запасной запрос -----------------------------------------------


def test_albums_split_by_ten() -> None:
    urls = [f"u{n}" for n in range(21)]
    assert [len(a) for a in photos.albums(urls)] == [10, 10, 1]


def test_debounce_blocks_double_tap_only_for_a_while() -> None:
    photos._recent.clear()
    assert photos.debounce(1, 42, now=100.0)
    assert not photos.debounce(1, 42, now=105.0)
    assert photos.debounce(2, 42, now=105.0)  # другой чат
    assert photos.debounce(1, 42, now=100.0 + photos.DEBOUNCE_SECONDS)


class _FakeLalafo:
    def __init__(self, detail=None, status: int | None = None) -> None:
        self.detail = detail
        self.status = status
        self.closed = False

    def fetch_details(self, external_id):
        if self.status is not None:
            request = httpx.Request("GET", "https://lalafo.kg")
            raise httpx.HTTPStatusError(
                "gone", request=request, response=httpx.Response(self.status, request=request)
            )
        return self.detail

    def close(self):
        self.closed = True


def _patch_parser(monkeypatch, fake: _FakeLalafo) -> None:
    import app.parsers.lalafo as lalafo_module

    monkeypatch.setattr(lalafo_module, "LalafoParser", lambda: fake)


def test_fetch_gallery_reads_lalafo_card(monkeypatch) -> None:
    fake = _FakeLalafo(detail={"id": 101, "images": [LALAFO_OTHER, LALAFO_MAIN]})
    _patch_parser(monkeypatch, fake)
    assert photos.fetch_gallery("lalafo.kg", "101")[0] == LALAFO_MAIN["original_url"]
    assert fake.closed


@pytest.mark.parametrize("status", [404, 410, 417])
def test_fetch_gallery_maps_dead_card_to_gone(monkeypatch, status: int) -> None:
    _patch_parser(monkeypatch, _FakeLalafo(status=status))
    with pytest.raises(photos.ListingGone):
        photos.fetch_gallery("lalafo.kg", "101")


def test_fetch_gallery_does_nothing_for_mashina() -> None:
    assert photos.fetch_gallery("mashina.kg", "1") == []


# --- хендлер ---------------------------------------------------------------------


class _AlbumMessage(_HandlerMessage):
    def __init__(self) -> None:
        super().__init__()
        self.message_id = 555
        self.albums: list[list[str]] = []
        self.single_photos: list[str] = []
        self.reply_to: list[int] = []

    async def answer_media_group(self, media, reply_to_message_id=None, **kwargs):
        self.albums.append([item.media for item in media])
        self.reply_to.append(reply_to_message_id)

    async def answer_photo(self, photo, reply_to_message_id=None, **kwargs):
        self.single_photos.append(photo)
        self.reply_to.append(reply_to_message_id)


def _gallery(urls, source_name="lalafo.kg") -> botdb.Gallery:
    return botdb.Gallery(
        listing_id=42,
        urls=urls,
        image_url=urls[0] if urls else None,
        source_name=source_name,
        external_id="101",
        url=URL,
        is_active=True,
    )


def _press(monkeypatch, gallery, fetched=None, gone=False):
    photos._recent.clear()
    saved: list[list[str]] = []
    fetch_calls: list[str] = []
    monkeypatch.setattr(handlers.db, "listing_gallery", lambda listing_id: gallery)
    monkeypatch.setattr(handlers.db, "save_gallery", lambda listing_id, urls: saved.append(urls))

    def fake_fetch(source_name, external_id):
        fetch_calls.append(external_id)
        if gone:
            raise photos.ListingGone
        return fetched or []

    monkeypatch.setattr(handlers.photos, "fetch_gallery", fake_fetch)
    message = _AlbumMessage()
    query = _Query(message)
    asyncio.run(handlers.photos_cb(query, PhotosCB(listing_id=42)))
    return message, query, saved, fetch_calls


def test_handler_sends_albums_from_db_as_reply_to_card(monkeypatch) -> None:
    urls = [f"https://img.example/{n}.jpeg" for n in range(11)]
    message, query, saved, fetch_calls = _press(monkeypatch, _gallery(urls))
    assert message.albums == [urls[:10]]
    assert message.single_photos == [urls[10]]  # хвост из одного — отдельным фото
    assert set(message.reply_to) == {555}
    assert fetch_calls == [] and saved == []


def test_handler_fetches_and_saves_missing_lalafo_gallery(monkeypatch) -> None:
    fetched = ["https://img.example/a.jpeg", "https://img.example/b.jpeg"]
    message, _, saved, fetch_calls = _press(monkeypatch, _gallery([]), fetched=fetched)
    assert fetch_calls == ["101"]
    assert saved == [fetched]  # второе нажатие — бесплатное
    assert message.albums == [fetched]


def test_handler_reports_removed_listing(monkeypatch) -> None:
    message, _, saved, _ = _press(monkeypatch, _gallery([]), gone=True)
    assert message.answers[0][0] == handlers.PHOTOS_GONE_TEXT
    assert message.albums == [] and saved == []


def test_handler_mashina_without_gallery_points_to_site(monkeypatch) -> None:
    message, _, _, fetch_calls = _press(monkeypatch, _gallery([], source_name="mashina.kg"))
    assert fetch_calls == []
    text, markup = message.answers[0]
    assert text == handlers.PHOTOS_MISSING_TEXT
    assert markup.inline_keyboard[0][0].url == URL


def test_handler_double_tap_sends_once(monkeypatch) -> None:
    urls = ["https://img.example/a.jpeg", "https://img.example/b.jpeg"]
    message, query, _, _ = _press(monkeypatch, _gallery(urls))
    asyncio.run(handlers.photos_cb(query, PhotosCB(listing_id=42)))  # второй тап сразу
    assert len(message.albums) == 1
    assert query.alerts[-1] == "Уже отправляю"


def test_handler_survives_rejected_album(monkeypatch) -> None:
    from aiogram.exceptions import TelegramBadRequest

    class _Rejecting(_AlbumMessage):
        async def answer_media_group(self, media, **kwargs):
            raise TelegramBadRequest(method=SimpleNamespace(), message="wrong file identifier")

    photos._recent.clear()
    monkeypatch.setattr(
        handlers.db, "listing_gallery", lambda listing_id: _gallery(["https://x/a", "https://x/b"])
    )
    message = _Rejecting()
    asyncio.run(handlers.photos_cb(_Query(message), PhotosCB(listing_id=42)))
    assert message.answers[0][0] == handlers.PHOTOS_FAILED_TEXT


def test_db_gallery_round_trip(monkeypatch) -> None:
    from sqlalchemy.orm import sessionmaker

    session = _sync_session()
    source, target = _lalafo_setup(session)
    run_sync(session, _FeedParser([_lalafo_item(None)], {101: _lalafo_item(None)}), source, target, mode="fresh")
    listing_id = session.scalar(select(Listing.id).where(Listing.external_id == "101"))
    monkeypatch.setattr(botdb, "SessionLocal", sessionmaker(bind=session.get_bind()))

    gallery = botdb.listing_gallery(listing_id)
    assert gallery.urls == [] and gallery.source_name == "lalafo.kg" and gallery.external_id == "101"
    botdb.save_gallery(listing_id, ["https://x/a", "https://x/b"])
    assert botdb.listing_gallery(listing_id).urls == ["https://x/a", "https://x/b"]
    assert botdb.listing_gallery(999_999) is None
