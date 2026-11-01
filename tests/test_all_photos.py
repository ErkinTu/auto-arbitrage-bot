"""Кнопка «📷 Все фото» (план 31.10.2026, п. 2.4): галерея в базе + запасной запрос.

Фикстуры — из `test_rich_cards` (урезанные живые ответы mashina/lalafo). Замер
31.10.2026: лента mashina отдаёт всю галерею (до 12), лента lalafo режет до 10,
в карточке lalafo — 11+; карточка ~7–13 КБ.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import app.bot.db as botdb
import app.db.session as dbsession
from app.bot import handlers, notifier, photos
from app.bot.keyboards import PhotosCB, UnhideCB, card_kb, has_photos_button, hidden_kb
from app.bot.matching import PendingNotification
from app.bot.notifier import build_outgoing
from app.models import Listing, ListingStatusHistory
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
    assert photos.fetch_gallery("lalafo.kg", "101", URL)[0] == LALAFO_MAIN["original_url"]
    assert fake.closed


@pytest.mark.parametrize("status", [404, 410, 417])
def test_fetch_gallery_maps_dead_card_to_gone(monkeypatch, status: int) -> None:
    _patch_parser(monkeypatch, _FakeLalafo(status=status))
    with pytest.raises(photos.ListingGone):
        photos.fetch_gallery("lalafo.kg", "101", URL)


def _mashina_page(images: list[dict]) -> str:
    """Как на живой странице 01.11.2026: массив внутри JS-строки данных Next.js."""
    import json

    escaped = json.dumps({"images": images}, separators=(",", ":")).replace('"', '\\"')
    return f'<script>self.__next_f.push([1,"{{\\"ad\\":{escaped}}}"])</script>'


def test_mashina_gallery_is_parsed_from_the_listing_page() -> None:
    html = _mashina_page(MASHINA_IMAGES)
    assert photos.mashina_gallery_from_page(html) == [i["medium"] for i in MASHINA_IMAGES]
    assert photos.mashina_gallery_from_page("<html>нет галереи</html>") == []
    assert photos.mashina_gallery_from_page('\\"images\\":[{broken') == []


class _FakeResponse:
    def __init__(self, url: str, status: int = 200, text: str = "") -> None:
        self.url = url
        self.status_code = status
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _patch_mashina_http(monkeypatch, response: _FakeResponse) -> None:
    class _Client:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return response

    monkeypatch.setattr(photos.httpx, "Client", _Client)


def test_fetch_gallery_reads_mashina_page(monkeypatch) -> None:
    page = "https://mashina.kg/details/toyota-camry-1"
    _patch_mashina_http(monkeypatch, _FakeResponse(page, text=_mashina_page(MASHINA_IMAGES)))
    assert len(photos.fetch_gallery("mashina.kg", "1", page)) == len(MASHINA_IMAGES)


def test_removed_mashina_ad_redirects_home_and_counts_as_gone(monkeypatch) -> None:
    _patch_mashina_http(monkeypatch, _FakeResponse("https://mashina.kg/", text="<html/>"))
    with pytest.raises(photos.ListingGone):
        photos.fetch_gallery("mashina.kg", "1", "https://mashina.kg/details/gone-1")


# --- хендлер ---------------------------------------------------------------------


class _AlbumMessage(_HandlerMessage):
    def __init__(self) -> None:
        super().__init__()
        self.message_id = 555
        self.albums: list[list[str]] = []
        self.single_photos: list[str] = []
        self.reply_to: list[int] = []
        self.text_reply_to: list[int | None] = []

    async def answer_media_group(self, media, reply_to_message_id=None, **kwargs):
        self.albums.append([item.media for item in media])
        self.reply_to.append(reply_to_message_id)

    async def answer_photo(self, photo, reply_to_message_id=None, **kwargs):
        self.single_photos.append(photo)
        self.reply_to.append(reply_to_message_id)

    async def answer(self, text, reply_markup=None, reply_to_message_id=None, **kwargs):
        self.answers.append((text, reply_markup))
        self.text_reply_to.append(reply_to_message_id)


NOW = datetime.now(timezone.utc)
STALE = NOW - timedelta(hours=7)  # дольше BOT_PHOTOS_RECHECK_HOURS=6


PHONE = "996500328668"


def _gallery(
    urls, source_name="lalafo.kg", is_active=True, last_seen_at=NOW, seller_phone=PHONE
) -> botdb.Gallery:
    return botdb.Gallery(
        listing_id=42,
        urls=urls,
        image_url=urls[0] if urls else None,
        source_name=source_name,
        external_id="101",
        url=URL,
        is_active=is_active,
        last_seen_at=last_seen_at,
        seller_phone=seller_phone,
    )


def _reset_memory() -> None:
    photos._recent.clear()
    photos._albums_sent.clear()
    notifier._photo_ids.clear()


def _press(
    monkeypatch,
    gallery,
    fetched=None,
    gone=False,
    error=None,
    marked=None,
    reset=True,
    phone=None,
    phone_hidden=False,
    saved_phones=None,
):
    if reset:
        _reset_memory()
    else:
        photos._recent.clear()  # дебаунс — не предмет этих тестов
    saved: list[list[str]] = []
    fetch_calls: list[str] = []
    marked = [] if marked is None else marked
    monkeypatch.setattr(handlers.db, "listing_gallery", lambda listing_id: gallery)
    monkeypatch.setattr(handlers.db, "save_gallery", lambda listing_id, urls: saved.append(urls))
    monkeypatch.setattr(
        handlers.db,
        "mark_listing_gone",
        lambda listing_id, reason, status_id=None: marked.append((listing_id, reason, status_id)),
    )
    saved_phones = [] if saved_phones is None else saved_phones
    monkeypatch.setattr(
        handlers.db, "save_seller_phone", lambda listing_id, value: saved_phones.append(value)
    )

    def fake_fetch(source_name, external_id, url):
        fetch_calls.append(external_id)
        if gone:
            raise gone if isinstance(gone, photos.ListingGone) else photos.ListingGone()
        if error is not None:
            raise error
        return photos.Card(urls=fetched or [], phone=phone, phone_hidden=phone_hidden)

    monkeypatch.setattr(handlers.photos, "fetch_card", fake_fetch)
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


def test_handler_fetches_missing_mashina_gallery_from_the_page(monkeypatch) -> None:
    """Регрессия 01.11.2026: у старых mashina кнопка отвечала «других фото нет»,
    хотя на сайте их 12 — запасного пути для mashina не было."""
    fetched = ["https://s3.mashina.kg/a.webp", "https://s3.mashina.kg/b.webp"]
    message, _, saved, fetch_calls = _press(
        monkeypatch, _gallery([], source_name="mashina.kg"), fetched=fetched
    )
    assert fetch_calls == ["101"]
    assert saved == [fetched]
    assert message.albums == [fetched]


def test_handler_without_any_photos_points_to_site(monkeypatch) -> None:
    message, _, _, fetch_calls = _press(monkeypatch, _gallery([], source_name="mashina.kg"))
    assert fetch_calls == ["101"]  # попытались, но фото нет
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
    assert [text for text, _ in message.answers] == [handlers.PHOTOS_FAILED_TEXT]  # без «Связаться»


def test_db_gallery_round_trip(monkeypatch) -> None:
    from sqlalchemy.orm import sessionmaker

    session = _sync_session()
    source, target = _lalafo_setup(session)
    run_sync(session, _FeedParser([_lalafo_item(None)], {101: _lalafo_item(None)}), source, target, mode="fresh")
    listing_id = session.scalar(select(Listing.id).where(Listing.external_id == "101"))
    monkeypatch.setattr(botdb, "SessionLocal", sessionmaker(bind=session.get_bind()))

    gallery = botdb.listing_gallery(listing_id)
    assert gallery.urls == [] and gallery.source_name == "lalafo.kg" and gallery.external_id == "101"
    assert (gallery.brand_name, gallery.model_name, gallery.year, gallery.price_usd) == (
        "Toyota",
        "Camry",
        2016,
        15000.0,
    )
    botdb.save_gallery(listing_id, ["https://x/a", "https://x/b"])
    assert botdb.listing_gallery(listing_id).urls == ["https://x/a", "https://x/b"]
    assert botdb.listing_gallery(999_999) is None


# --- снятые объявления (план 01.11.2026, п. А) -----------------------------------------

TWO = ["https://img.example/a.jpeg", "https://img.example/b.jpeg"]


def test_needs_recheck_only_for_long_unseen() -> None:
    assert not photos.needs_recheck(NOW - timedelta(hours=5), hours=6, now=NOW)
    assert photos.needs_recheck(NOW - timedelta(hours=7), hours=6, now=NOW)
    assert photos.needs_recheck(None, hours=6, now=NOW)
    naive = (NOW - timedelta(hours=7)).replace(tzinfo=None)  # SQLite
    assert photos.needs_recheck(naive, hours=6, now=NOW)
    assert not photos.needs_recheck(None, hours=0, now=NOW)  # выключатель


def test_inactive_listing_gets_no_album_and_no_request(monkeypatch) -> None:
    """Регрессия 01.11.2026: альбом уходил и у снятого — флаг читался, но не проверялся."""
    message, _, _, fetch_calls = _press(monkeypatch, _gallery(TWO, is_active=False))
    assert message.answers[0][0] == handlers.PHOTOS_GONE_TEXT
    assert message.albums == [] and fetch_calls == []


def test_fresh_listing_with_gallery_is_not_rechecked(monkeypatch) -> None:
    message, _, _, fetch_calls = _press(monkeypatch, _gallery(TWO, last_seen_at=NOW - timedelta(hours=1)))
    assert fetch_calls == []
    assert message.albums == [TWO]


def test_stale_listing_removed_on_source_is_marked_and_gets_no_album(monkeypatch) -> None:
    marked: list = []
    message, _, saved, fetch_calls = _press(
        monkeypatch,
        _gallery(TWO, last_seen_at=STALE),
        gone=photos.ListingGone("card_inactive", 8),
        marked=marked,
    )
    assert fetch_calls == ["101"]
    assert marked == [(42, "card_inactive", 8)]
    assert message.answers[0][0] == handlers.PHOTOS_GONE_TEXT
    assert message.albums == [] and saved == []


def test_stale_listing_source_error_falls_back_to_db_gallery(monkeypatch) -> None:
    """Таймаут/5xx/прокси ≠ «снято»: альбом из базы, флаг не трогаем."""
    marked: list = []
    message, _, saved, fetch_calls = _press(
        monkeypatch, _gallery(TWO, last_seen_at=STALE), error=httpx.ConnectTimeout("t"), marked=marked
    )
    assert fetch_calls == ["101"]
    assert marked == [] and saved == []
    assert message.albums == [TWO]


def test_stale_listing_alive_refreshes_gallery(monkeypatch) -> None:
    fresh = TWO + ["https://img.example/c.jpeg"]
    message, _, saved, _ = _press(monkeypatch, _gallery(TWO, last_seen_at=STALE), fetched=fresh)
    assert saved == [fresh]
    assert message.albums == [fresh]


def test_stale_listing_same_gallery_is_not_rewritten(monkeypatch) -> None:
    _, _, saved, fetch_calls = _press(monkeypatch, _gallery(TWO, last_seen_at=STALE), fetched=list(TWO))
    assert fetch_calls == ["101"] and saved == []


def test_lalafo_card_with_dead_status_counts_as_gone(monkeypatch) -> None:
    """lalafo отдаёт снятое с 200 и status_id 8 (сайт — 410), как разбирает валидатор."""
    _patch_parser(monkeypatch, _FakeLalafo(detail={"id": 101, "status_id": 8, "images": [LALAFO_MAIN]}))
    with pytest.raises(photos.ListingGone) as caught:
        photos.fetch_gallery("lalafo.kg", "101", URL)
    assert (caught.value.reason, caught.value.status_id) == ("card_inactive", 8)


@pytest.mark.parametrize("status", [404, 410, 417])
def test_lalafo_http_gone_reason_is_card_gone(monkeypatch, status: int) -> None:
    _patch_parser(monkeypatch, _FakeLalafo(status=status))
    with pytest.raises(photos.ListingGone) as caught:
        photos.fetch_gallery("lalafo.kg", "101", URL)
    assert caught.value.reason == "card_gone"


def test_mark_listing_gone_writes_like_validator(monkeypatch) -> None:
    session = _sync_session()
    source, target = _lalafo_setup(session)
    run_sync(session, _FeedParser([_lalafo_item(None)], {101: _lalafo_item(None)}), source, target, mode="fresh")
    listing_id = session.scalar(select(Listing.id).where(Listing.external_id == "101"))
    monkeypatch.setattr(botdb, "SessionLocal", sessionmaker(bind=session.get_bind()))

    assert botdb.mark_listing_gone(listing_id, "card_inactive", 8)
    assert not botdb.mark_listing_gone(listing_id, "card_gone")  # повтор — не дублируем историю

    session.expire_all()
    listing = session.get(Listing, listing_id)
    assert listing.is_active is False
    history = session.scalars(
        select(ListingStatusHistory).where(
            ListingStatusHistory.listing_id == listing_id,
            ListingStatusHistory.event_type == "deactivated",
        )
    ).all()
    assert len(history) == 1


# --- «Связаться на сайте» после альбома (план 01.11.2026, п. Д) ------------------------


def _camry(**overrides) -> botdb.Gallery:
    gallery = _gallery(TWO, **overrides)
    gallery.brand_name, gallery.model_name, gallery.year, gallery.price_usd = "Toyota", "Camry", 2018, 15000.0
    return gallery


def test_album_is_followed_by_spoiler_number_and_whatsapp_button(monkeypatch) -> None:
    message, _, _, fetch_calls = _press(monkeypatch, _camry())
    assert message.albums == [TWO] and fetch_calls == []  # номер уже в базе
    [(text, markup)] = message.answers
    assert text == "Toyota Camry · 2018 · $15 000\n📞 <tg-spoiler>+996 500 328 668</tg-spoiler>"
    [[button]] = markup.inline_keyboard
    assert button.text == handlers.PHOTOS_WHATSAPP_BUTTON
    assert button.url.startswith("https://wa.me/996500328668?text=")
    from urllib.parse import unquote

    greeting = unquote(button.url.split("?text=", 1)[1])
    assert greeting == (
        "Здравствуйте! Пишу по объявлению Toyota Camry · 2018 · $15 000 на lalafo.kg. Ещё продаётся?"
    )
    assert message.text_reply_to == [555]  # тоже ответом на карточку


def test_mashina_number_is_fetched_after_album_and_remembered(monkeypatch) -> None:
    saved: list = []
    message, _, _, fetch_calls = _press(
        monkeypatch, _camry(source_name="mashina.kg", seller_phone=None), phone=PHONE, saved_phones=saved
    )
    assert message.albums == [TWO]  # фото не ждали телефона
    assert fetch_calls == ["101"] and saved == [PHONE]
    assert message.answers[0][1].inline_keyboard[0][0].url.startswith("https://wa.me/996500328668")


def test_without_number_contact_line_points_to_site(monkeypatch) -> None:
    saved: list = []
    message, *_ = _press(monkeypatch, _camry(seller_phone=None), saved_phones=saved)
    [(text, markup)] = message.answers
    assert text == "Toyota Camry · 2018 · $15 000"
    [[button]] = markup.inline_keyboard
    assert (button.text, button.url) == (handlers.PHOTOS_CONTACT_BUTTON, URL)
    assert saved == []  # в ответе номера нет — нечего записывать


def test_number_hidden_by_seller_is_erased_and_not_shown(monkeypatch) -> None:
    saved: list = []
    message, *_ = _press(
        monkeypatch,
        _camry(last_seen_at=STALE),
        fetched=TWO,
        phone_hidden=True,
        saved_phones=saved,
    )
    assert saved == [None]
    text, markup = message.answers[0]
    assert "📞" not in text and markup.inline_keyboard[0][0].text == handlers.PHOTOS_CONTACT_BUTTON


def test_phone_switch_off_restores_site_button_without_requests(monkeypatch) -> None:
    monkeypatch.setattr(handlers.get_settings(), "bot_seller_phone_enabled", False)
    message, _, _, fetch_calls = _press(monkeypatch, _camry(seller_phone=None))
    assert fetch_calls == []
    assert message.answers[0][1].inline_keyboard[0][0].text == handlers.PHOTOS_CONTACT_BUTTON


def test_listing_removed_when_fetching_number_after_album(monkeypatch) -> None:
    marked: list = []
    message, *_ = _press(monkeypatch, _camry(seller_phone=None), gone=True, marked=marked)
    assert message.albums == [TWO]
    assert marked and message.answers[-1][0] == handlers.PHOTOS_GONE_TEXT


def test_contact_line_falls_back_to_source_title_and_escapes() -> None:
    from app.bot.messages import photos_contact_text

    gallery = _gallery(TWO)
    gallery.title, gallery.year = "Lada <2107>: 1.5 л", None
    assert photos_contact_text(gallery) == "Lada &lt;2107&gt;"


def test_no_contact_line_when_album_is_not_sent(monkeypatch) -> None:
    message, *_ = _press(monkeypatch, _gallery(TWO, is_active=False))
    assert [text for text, _ in message.answers] == [handlers.PHOTOS_GONE_TEXT]


# --- лимит альбомов и кэш file_id (план 01.11.2026, п. Г) -------------------------------


def test_album_limit_window_and_wait() -> None:
    _reset_memory()
    for minute in range(3):
        photos.record_album(1, now=minute * 60.0)
    assert photos.album_wait_minutes(1, limit=4, now=180.0) == 0
    assert photos.album_wait_minutes(1, limit=3, now=180.0) == 57  # первый выйдет на 60-й мин
    assert photos.album_wait_minutes(2, limit=3, now=180.0) == 0  # другой человек
    assert photos.album_wait_minutes(1, limit=3, now=3600.0) == 0  # окно сдвинулось
    assert photos.album_wait_minutes(1, limit=0, now=180.0) == 0  # выключатель


def test_handler_stops_at_album_limit_without_touching_source(monkeypatch) -> None:
    monkeypatch.setattr(handlers.get_settings(), "bot_photos_albums_per_hour", 2)
    _reset_memory()
    stale = _gallery(TWO, last_seen_at=STALE)
    for _ in range(2):
        message, _, _, _ = _press(monkeypatch, _gallery(TWO), reset=False)
        assert message.albums == [TWO]
    message, _, _, fetch_calls = _press(monkeypatch, stale, fetched=TWO, reset=False)
    assert message.albums == [] and fetch_calls == []
    text, markup = message.answers[0]
    assert text.startswith("За час можно открыть 2 альбома")
    assert markup.inline_keyboard[0][0].url == URL


def test_gone_listing_does_not_spend_album_limit(monkeypatch) -> None:
    _reset_memory()
    _press(monkeypatch, _gallery(TWO, is_active=False), reset=False)
    assert photos._albums_sent == {}


class _Sent:
    def __init__(self, file_id: str) -> None:
        self.photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id=file_id)]


class _CachingMessage(_AlbumMessage):
    def __init__(self, reject_file_ids: bool = False) -> None:
        super().__init__()
        self.reject_file_ids = reject_file_ids

    async def answer_media_group(self, media, reply_to_message_id=None, **kwargs):
        from aiogram.exceptions import TelegramBadRequest

        items = [item.media for item in media]
        if self.reject_file_ids and any(i.startswith("fid-") for i in items):
            raise TelegramBadRequest(method=SimpleNamespace(), message="wrong file identifier")
        self.albums.append(items)
        return [_Sent(f"fid-{n}") for n, _ in enumerate(items)]


def test_album_remembers_file_ids_and_reuses_them() -> None:
    _reset_memory()
    first = _CachingMessage()
    asyncio.run(handlers._send_album(first, TWO, 555))
    assert first.albums == [TWO]
    assert notifier.cached_photo_id(TWO[0]) == "fid-0"  # крупный размер, а не превью

    second = _CachingMessage()
    asyncio.run(handlers._send_album(second, TWO, 555))
    assert second.albums == [["fid-0", "fid-1"]]  # повтор — без CDN


def test_album_falls_back_to_urls_when_file_id_is_rejected() -> None:
    _reset_memory()
    notifier.remember_photo_id(TWO[0], "fid-old")
    message = _CachingMessage(reject_file_ids=True)
    asyncio.run(handlers._send_album(message, TWO, 555))
    assert message.albums == [TWO]
    assert notifier.cached_photo_id(TWO[0]) == "fid-0"


def test_photos_limit_text_plurals() -> None:
    from app.bot.messages import photos_limit_text

    assert photos_limit_text(30, 1) == (
        "За час можно открыть 30 альбомов с фото — лимит исчерпан. "
        "Следующий — через 1 минуту. Пока все фото — на сайте объявления."
    )
    assert "21 альбом " in photos_limit_text(21, 5) and "5 минут." in photos_limit_text(21, 5)


# --- телефон продавца (п. Ж плана 01.11.2026) ---------------------------------------------


def test_mashina_page_phone_is_parsed() -> None:
    page = (
        '{\\"slug\\":\\"phone\\",\\"name\\":\\"Телефон\\",'
        '\\"value\\":\\"996500328668\\",\\"meta\\":null}'
    )
    assert photos.mashina_phone_from_page(page) == "996500328668"
    assert photos.mashina_phone_from_page("<html>без телефона</html>") is None


def test_fetch_card_reads_lalafo_phone_and_respects_hidden(monkeypatch) -> None:
    _patch_parser(monkeypatch, _FakeLalafo(detail={"id": 101, "mobile": "+996 500 328 668"}))
    card = photos.fetch_card("lalafo.kg", "101", URL)
    assert (card.phone, card.phone_hidden) == ("996500328668", False)
    _patch_parser(monkeypatch, _FakeLalafo(detail={"id": 101, "mobile": "+996500328668", "hide_phone": True}))
    card = photos.fetch_card("lalafo.kg", "101", URL)
    assert (card.phone, card.phone_hidden) == (None, True)


def test_fetch_card_reads_mashina_phone(monkeypatch) -> None:
    page = "https://mashina.kg/details/toyota-camry-1"
    html = _mashina_page(MASHINA_IMAGES) + '\\"slug\\":\\"phone\\",\\"value\\":\\"+996 555 12 34 56\\"'
    _patch_mashina_http(monkeypatch, _FakeResponse(page, text=html))
    card = photos.fetch_card("mashina.kg", "1", page)
    assert card.phone == "996555123456" and len(card.urls) == len(MASHINA_IMAGES)


# --- проверка витрины 01.11.2026: заглушка вместо фото, номер списком --------------------


def test_mashina_placeholder_is_not_a_photo() -> None:
    from app.services.images import extract_image_url, real_photo

    stub = "https://s3.mashina.kg/assets/preview.png"
    assert real_photo(stub) is None
    assert extract_image_url({"preview_image": stub}) is None
    assert extract_image_urls({"images": [{"medium": stub}]}) == []
    real = MASHINA_IMAGES[0]["medium"]
    assert extract_image_urls({"images": [{"medium": stub}, {"medium": real}]}) == [real]


def test_card_with_stored_placeholder_goes_out_without_photo() -> None:
    from app.bot.matching import build_pending

    listing = Listing(
        id=1, source_id=1, title="Mercedes C", year=1999, city="Бишкек",
        current_price_usd=1830, image_url="https://s3.mashina.kg/assets/preview.png",
    )
    note = build_pending(
        listing, chat_id=1, filter_id=1, below_percent=50.0, estimate=None,
        source_names={1: "mashina.kg"}, brand_names={}, model_names={},
    )
    assert note.image_url is None


def test_mashina_phone_as_list_is_parsed() -> None:
    """Регрессия 01.11.2026: у большинства страниц `"value":["996…"]` — номер не находился."""
    page = (
        '\\"attributes\\":[{\\"slug\\":\\"phone\\",\\"name\\":\\"Телефон\\",'
        '\\"value\\":[\\"996555123456\\"],\\"meta\\":null}]'
    )
    assert photos.mashina_phone_from_page(page) == "996555123456"
    json_variant = '\\"slug\\":\\"phone\\",\\"value_text\\":null,\\"value_json\\":[\\"+996 555 12 34 56\\"]'
    assert photos.mashina_phone_from_page(json_variant) == "996555123456"
