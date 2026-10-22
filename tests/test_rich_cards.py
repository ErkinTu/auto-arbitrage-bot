"""Карточки с фото (план 22.10.2026, этап 1): фото, данные карточки, «Не то», меню.

Фикстуры фото — урезанные живые ответы лент mashina и lalafo от 22.10.2026
(только поля галереи): именно на них старый экстрактор давал 0 из 24 у lalafo.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from aiogram.methods import SendPhoto
from aiogram.types import Message
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.bot.db as botdb
import app.db.session as dbsession
from app.bot.access import AccessMiddleware
from app.bot.keyboards import HideCB, UnhideCB, card_kb, hidden_kb, markup_url
from app.bot.matching import market_average, market_estimate
from app.bot.menu import (
    LEGACY_MENU_HELP,
    LEGACY_MENU_SUBSCRIBE,
    MENU_ACCESS,
    MENU_ADD_FILTER,
    MENU_BEST,
    MENU_FILTERS,
    main_menu_kb,
    menu_command,
)
from app.bot.protection import ContentProtectionMiddleware
from app.bot.showcase import compute_showcase
from app.core.config import Settings
from app.db.base import Base
from app.models import BotUser, Brand, CarModel, DailyMarketStat, Listing, Source, SyncTarget
from app.parsers.base import FetchPage, ParserAdapter
from app.services.blocklist import KIND_BRAND, KIND_MODEL
from app.services.images import extract_image_url
from app.services.normalizer import (
    NormalizedListing,
    normalize_lalafo_listing,
    normalize_mashina_listing,
)
from app.services.sync import run_sync

# --- фикстуры живых ответов (22.10.2026) ------------------------------------

MASHINA_IMAGES = [
    {
        "id": 1579396,
        "hash": "fcd4b371898a72b32e92c76ce76d1fd8",
        "thumb": "https://s3.mashina.kg/images/2026-10-21/10165863/fcd4b371898a72b32e92c76ce76d1fd8_small.webp",
        "medium": "https://s3.mashina.kg/images/2026-10-21/10165863/fcd4b371898a72b32e92c76ce76d1fd8_medium.webp",
        "big": "https://s3.mashina.kg/images/2026-10-21/10165863/fcd4b371898a72b32e92c76ce76d1fd8_large.webp",
    },
    {
        "id": 1579397,
        "hash": "ffd44efe0c6f453b0c96649853ab476e",
        "thumb": "https://s3.mashina.kg/images/2026-10-21/10165863/ffd44efe0c6f453b0c96649853ab476e_small.webp",
        "medium": "https://s3.mashina.kg/images/2026-10-21/10165863/ffd44efe0c6f453b0c96649853ab476e_medium.webp",
        "big": "https://s3.mashina.kg/images/2026-10-21/10165863/ffd44efe0c6f453b0c96649853ab476e_large.webp",
    },
]

LALAFO_MAIN = {
    "id": 907412996,
    "is_main": True,
    "is_cv_image": False,
    "original_url": "https://img5.lalafo.com/i/posters/original/08/23/eb/6e7b3663bba3d017f9f052c574.jpeg",
    "thumbnail_url": "https://img5.lalafo.com/i/posters/api/08/23/eb/6e7b3663bba3d017f9f052c574.jpeg",
    "original_webp_url": "https://img5.lalafo.com/i/posters/original_webp/08/23/eb/6e7b3663bba3d017f9f052c574.webp",
    "thumbnail_webp_url": "https://img5.lalafo.com/i/posters/api_webp/08/23/eb/6e7b3663bba3d017f9f052c574.webp",
    "p_hash": "18074046622664300544",
}
LALAFO_OTHER = {
    "id": 907412997,
    "is_main": False,
    "is_cv_image": False,
    "original_url": "https://img5.lalafo.com/i/posters/original/77/7c/f4/30024b37ff207e487cbbed4c4c.jpeg",
    "thumbnail_url": "https://img5.lalafo.com/i/posters/api/77/7c/f4/30024b37ff207e487cbbed4c4c.jpeg",
    "original_webp_url": "https://img5.lalafo.com/i/posters/original_webp/77/7c/f4/30024b37ff207e487cbbed4c4c.webp",
    "thumbnail_webp_url": "https://img5.lalafo.com/i/posters/api_webp/77/7c/f4/30024b37ff207e487cbbed4c4c.webp",
    "p_hash": "11566579456613906432",
}


# --- 1.1 экстрактор ---------------------------------------------------------


def test_extract_mashina_medium() -> None:
    assert extract_image_url({"images": MASHINA_IMAGES}) == MASHINA_IMAGES[0]["medium"]


def test_extract_lalafo_main_jpeg_even_if_not_first() -> None:
    payload = {"images": [LALAFO_OTHER, LALAFO_MAIN]}
    assert extract_image_url(payload) == LALAFO_MAIN["original_url"]


def test_extract_lalafo_without_main_takes_first_and_prefers_jpeg() -> None:
    other = dict(LALAFO_OTHER)
    assert extract_image_url({"images": [other]}) == other["original_url"]
    webp_only = {"is_main": True, "original_webp_url": LALAFO_MAIN["original_webp_url"]}
    assert extract_image_url({"images": [webp_only]}) == LALAFO_MAIN["original_webp_url"]


def test_extract_nothing_usable() -> None:
    assert extract_image_url(None) is None
    assert extract_image_url({}) is None
    assert extract_image_url({"images": []}) is None
    assert extract_image_url({"images": [{"is_main": True, "original_url": "ftp://x"}]}) is None
    # Старые снимки raw_payloads (safe_payload) галереи не содержат — None, не падение.
    assert extract_image_url({"id": 1, "title": "Toyota", "attributes": []}) is None


def test_vehicle_summary_keeps_old_name_for_admin() -> None:
    from app.services.vehicle_summary import _extract_image_url

    assert _extract_image_url({"images": MASHINA_IMAGES}) == MASHINA_IMAGES[0]["medium"]


# --- 1.2 нормализация и sync ------------------------------------------------


def _mashina_raw(**overrides) -> dict[str, Any]:
    raw = {
        "id": 10165863,
        "title": "Toyota Camry 70 2.5 AT",
        "slug": "toyota-camry-70",
        "prices": [{"currency": "USD", "amount": 15900, "is_original": True}],
        "status": "active",
        "attributes": [{"slug": "year", "value_number": 2018}],
        "images": MASHINA_IMAGES,
    }
    raw.update(overrides)
    return raw


def test_normalizers_fill_image_url() -> None:
    assert normalize_mashina_listing(_mashina_raw()).image_url == MASHINA_IMAGES[0]["medium"]
    lalafo = normalize_lalafo_listing(
        {"id": 1, "title": "Toyota Camry: 2016 г.", "images": [LALAFO_OTHER, LALAFO_MAIN]},
        fallback_make="Toyota",
    )
    assert lalafo.image_url == LALAFO_MAIN["original_url"]


def test_image_url_is_outside_payload_hash() -> None:
    """Иначе первый обход после выката пометил бы всю базу как «updated»."""
    with_photo = normalize_mashina_listing(_mashina_raw())
    without_photo = normalize_mashina_listing(_mashina_raw(images=[]))
    assert with_photo.payload_hash == without_photo.payload_hash
    assert "image_url" not in with_photo.analytics_payload()


def _sync_session() -> Session:
    import app.models  # noqa: F401
    from app.services.catalog_service import seed_default_catalog

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    seed_default_catalog(session)
    session.commit()
    return session


class _FeedParser(ParserAdapter):
    """Лента lalafo: карточку дотягиваем только для новых/подешевевших."""

    source_name = "lalafo.kg"
    needs_detail_enrichment = True

    def __init__(self, feed: list[dict], details: dict[int, dict]) -> None:
        self.feed = feed
        self.details = details
        self.detail_calls: list[int] = []

    def fetch_listings(self, **_: Any) -> FetchPage:
        return FetchPage(items=self.feed, total=len(self.feed))

    def normalize(self, raw: dict[str, Any], *, fallback_make: str | None = None) -> NormalizedListing:
        return normalize_lalafo_listing(raw, fallback_make=fallback_make)

    def fetch_details(self, external_id: str | int) -> dict[str, Any]:
        self.detail_calls.append(int(external_id))
        return self.details[int(external_id)]

    def close(self) -> None:
        pass


def _lalafo_item(images: list[dict] | None) -> dict[str, Any]:
    item = {
        "id": 101,
        "title": "Toyota Camry: 2016 г., 2.5 л",
        "url": "/bishkek/ads/toyota-camry-id-101",
        "price": 15000,
        "currency": "USD",
        "city": "Бишкек",
        "created_time": 1784050897,
        "params": [],
    }
    if images is not None:
        item["images"] = images
    return item


def _lalafo_setup(session: Session) -> tuple[Source, SyncTarget]:
    source = Source(name="lalafo.kg", base_url="https://lalafo.kg")
    session.add(source)
    session.flush()
    target = SyncTarget(source_id=source.id, brand_filter="Toyota", page_size=20, max_pages=1, max_items=50)
    session.add(target)
    session.commit()
    return source, target


def test_sync_known_listing_gets_photo_from_feed_without_detail_fetch() -> None:
    """Объявление, увиденное до колонки image_url, получает фото из ленты бесплатно."""
    session = _sync_session()
    source, target = _lalafo_setup(session)
    run_sync(session, _FeedParser([_lalafo_item(None)], {101: _lalafo_item(None)}), source, target, mode="fresh")
    listing = session.scalar(select(Listing).where(Listing.external_id == "101"))
    assert listing.image_url is None

    parser = _FeedParser([_lalafo_item([LALAFO_MAIN])], {})
    run_sync(session, parser, source, target, mode="fresh")
    assert parser.detail_calls == []
    session.refresh(listing)
    assert listing.image_url == LALAFO_MAIN["original_url"]


def test_sync_missing_photo_in_answer_keeps_known_one() -> None:
    session = _sync_session()
    source, target = _lalafo_setup(session)
    run_sync(
        session,
        _FeedParser([_lalafo_item([LALAFO_MAIN])], {101: _lalafo_item([LALAFO_MAIN])}),
        source,
        target,
        mode="fresh",
    )
    # Цена сменилась → карточку перечитали, а в ней фото нет: известное не затираем.
    repriced = _lalafo_item(None) | {"price": 14000}
    run_sync(session, _FeedParser([repriced], {101: repriced}), source, target, mode="fresh")
    listing = session.scalar(select(Listing).where(Listing.external_id == "101"))
    assert listing.current_price_amount == Decimal("14000")
    assert listing.image_url == LALAFO_MAIN["original_url"]


def test_schema_upgrade_adds_image_url_in_parsable_form() -> None:
    """Колонку добавляет `migrate` строкой, которую разбирает `_already_applied`."""
    statement = "ALTER TABLE listings ADD COLUMN IF NOT EXISTS image_url TEXT"
    source = open(dbsession.__file__, encoding="utf-8").read()
    assert f'"{statement}"' in source
    assert dbsession._ADD_COLUMN_RE.match(statement).groups() == ("listings", "image_url")


# --- 1.3 цена рынка + число образцов, витрина -------------------------------


def _bot_session() -> Session:
    import app.models  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Source(name="mashina.kg"))
    session.add_all([Brand(name="Toyota"), Brand(name="Lada")])
    session.flush()
    toyota = session.scalar(select(Brand).where(Brand.name == "Toyota"))
    session.add(CarModel(brand_id=toyota.id, name="Camry"))
    session.commit()
    return session


def _listing(session: Session, *, external_id: str = "1", with_model: bool = True, **overrides) -> Listing:
    brand = session.scalar(select(Brand).where(Brand.name == "Toyota"))
    model = session.scalar(select(CarModel))
    values = dict(
        source_id=session.scalar(select(Source.id)),
        external_id=external_id,
        brand_id=brand.id,
        car_model_id=model.id if with_model else None,
        title="Toyota Camry 70",
        source_slug=f"toyota-camry-{external_id}",
        year=2019,
        mileage_km=90_000,
        city="Бишкек",
        current_price_usd=Decimal("10000"),
        analytics_eligible=True,
        is_active=True,
        source_created_at=datetime.now(timezone.utc),
        image_url=MASHINA_IMAGES[0]["medium"],
        payload_hash="x" * 8,
    )
    values.update(overrides)
    listing = Listing(**values)
    session.add(listing)
    session.commit()
    return listing


def _stat(
    session: Session, *, avg: str, samples: int, bucket: str = "50-100k", median: str | None = None
) -> None:
    brand = session.scalar(select(Brand).where(Brand.name == "Toyota"))
    session.add(
        DailyMarketStat(
            stat_date=date.today(),
            brand_id=brand.id,
            car_model_id=session.scalar(select(CarModel.id)),
            year=2019,
            mileage_bucket=bucket,
            customs_status="unknown",
            avg_price_usd=Decimal(avg),
            median_price_usd=Decimal(median or avg),
            min_price_usd=Decimal(avg),
            max_price_usd=Decimal(avg),
            sample_count=samples,
        )
    )
    session.commit()


def test_market_estimate_returns_avg_and_samples() -> None:
    session = _bot_session()
    _stat(session, avg="12000", samples=10)
    _stat(session, avg="15000", samples=5, bucket="100-150k")
    listing = _listing(session)
    estimate = market_estimate(session, listing, min_samples=5)
    assert (estimate.avg_usd, estimate.samples) == (12000.0, 10)
    # NULL-пробег — все бакеты: образцы суммируются.
    no_mileage = _listing(session, external_id="2", mileage_km=None)
    assert market_estimate(session, no_mileage, min_samples=5).samples == 15
    # Обёртка для старых вызовов отдаёт ту же среднюю.
    assert market_average(session, listing, min_samples=5) == 12000.0


def test_market_estimate_median_basis_is_opt_in() -> None:
    """Рассылка остаётся на средней; медиана — только по явному basis (замер 1.5)."""
    from app.bot.matching import BASIS_MEDIAN

    session = _bot_session()
    _stat(session, avg="12000", median="11000", samples=10)
    listing = _listing(session)
    assert market_estimate(session, listing, min_samples=5).avg_usd == 12000.0
    assert market_estimate(session, listing, min_samples=5, basis=BASIS_MEDIAN).avg_usd == 11000.0


def test_market_basis_volume_counts_both_bases() -> None:
    from app.bot.campaigns import market_basis_volume

    session = _bot_session()
    _stat(session, avg="12500", median="11000", samples=10)
    _listing(session, current_price_usd=Decimal("9900"))  # −21% от средней, −10% от медианы
    total, rows = market_basis_volume(session, days=1)
    by_label = {row.label: row for row in rows}
    assert total == 1
    assert (by_label["ниже рынка на 20%+"].avg_per_day, by_label["ниже рынка на 20%+"].median_per_day) == (1, 0)
    assert (by_label["ниже рынка на 5%+"].avg_per_day, by_label["ниже рынка на 5%+"].median_per_day) == (1, 1)
    assert by_label["порог 20%: только при этой базе"].avg_per_day == 1


def test_showcase_card_carries_photo_model_and_samples() -> None:
    session = _bot_session()
    _stat(session, avg="12500", samples=31)
    _listing(session, current_price_usd=Decimal("10000"))
    [card] = compute_showcase(session, min_samples=5)
    assert (card.brand_name, card.model_name, card.year) == ("Toyota", "Camry", 2019)
    assert card.market_samples == 31
    assert card.below_market_percent == 20.0
    assert card.image_url == MASHINA_IMAGES[0]["medium"]


# --- 1.8 «Не то» одним тапом ------------------------------------------------


class _SharedSession:
    def __init__(self, session: Session) -> None:
        self.session = session

    def __call__(self):
        return self

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *exc) -> bool:
        return False


def _user(session: Session, telegram_id: int = 777) -> BotUser:
    user = BotUser(telegram_id=telegram_id)
    session.add(user)
    session.commit()
    return user


def test_hide_from_listing_blocks_model_and_undo_returns_it(monkeypatch) -> None:
    session = _bot_session()
    user = _user(session)
    listing = _listing(session)
    monkeypatch.setattr(botdb, "SessionLocal", _SharedSession(session))

    hidden = botdb.hide_from_listing(user.telegram_id, listing.id)
    assert (hidden.ok, hidden.label) == (True, "Toyota Camry")
    [block] = botdb.list_blocks(user.telegram_id)
    assert (block.id, block.kind) == (hidden.block_id, KIND_MODEL)

    # Повторный тап (другая карточка той же модели) — тот же запрет, «Вернуть» работает.
    again = botdb.hide_from_listing(user.telegram_id, listing.id)
    assert (again.already, again.block_id) == (True, hidden.block_id)

    assert botdb.delete_block(user.telegram_id, hidden.block_id) is True
    assert botdb.list_blocks(user.telegram_id) == []


def test_hide_from_listing_without_model_blocks_brand(monkeypatch) -> None:
    session = _bot_session()
    user = _user(session)
    listing = _listing(session, with_model=False)
    monkeypatch.setattr(botdb, "SessionLocal", _SharedSession(session))

    hidden = botdb.hide_from_listing(user.telegram_id, listing.id)
    assert (hidden.ok, hidden.label) == (True, "Toyota")
    assert botdb.list_blocks(user.telegram_id)[0].kind == KIND_BRAND
    assert botdb.hide_from_listing(user.telegram_id, 9999).unavailable is True


def test_hide_respects_block_limit(monkeypatch) -> None:
    session = _bot_session()
    user = _user(session)
    listing = _listing(session)
    monkeypatch.setattr(botdb, "SessionLocal", _SharedSession(session))
    monkeypatch.setattr(botdb, "MAX_BLOCKS_PER_USER", 0)
    assert botdb.hide_from_listing(user.telegram_id, listing.id).limit_reached is True


def test_card_keyboards_fit_callback_limit_and_keep_link() -> None:
    url = "https://www.mashina.kg/details/toyota-camry-1/"
    card = card_kb(2_147_483_647, url)
    hidden = hidden_kb("Toyota Land Cruiser Prado", 2_147_483_647, 2_147_483_647, markup_url(card))
    for markup in (card, hidden):
        for row in markup.inline_keyboard:
            for button in row:
                assert button.callback_data is None or len(button.callback_data.encode()) <= 64
    assert markup_url(hidden) == url
    assert hidden.inline_keyboard[0][0].callback_data == UnhideCB(
        block_id=2_147_483_647, listing_id=2_147_483_647
    ).pack()
    assert card.inline_keyboard[0][0].callback_data == HideCB(listing_id=2_147_483_647).pack()


# --- 1.9 меню ---------------------------------------------------------------


def test_menu_has_owner_buttons() -> None:
    texts = [button.text for row in main_menu_kb().keyboard for button in row]
    assert texts == [MENU_BEST, MENU_ADD_FILTER, MENU_FILTERS, MENU_ACCESS]
    assert texts == ["🔥 Лучшее сейчас", "➕ Фильтр", "📋 Мои фильтры", "💎 Доступ"]


def test_menu_buttons_equal_commands_including_old_keyboards() -> None:
    assert menu_command(MENU_BEST) == "best"
    assert menu_command(MENU_ACCESS) == "subscribe"
    # Старые клавиатуры живут в чатах, пока их не заменят, — кнопки обязаны работать.
    assert menu_command(LEGACY_MENU_SUBSCRIBE) == "subscribe"
    assert menu_command(LEGACY_MENU_HELP) == "help"


def _message(text: str) -> Message:
    return Message.model_construct(text=text)


def test_access_button_works_without_access_but_best_does_not() -> None:
    """«Доступ» ведёт к оплате и без доступа; витрина по кнопке — только с доступом
    (открытый вопрос 1: пейвол, кроме /start)."""
    assert AccessMiddleware._is_entry(_message(MENU_ACCESS)) is True
    assert AccessMiddleware._is_entry(_message(LEGACY_MENU_SUBSCRIBE)) is True
    assert AccessMiddleware._is_entry(_message(MENU_BEST)) is False


# --- 1.6 protect_content у send_photo ---------------------------------------


def test_send_photo_gets_protect_content(monkeypatch) -> None:
    settings = Settings(_env_file=None, bot_protect_content=True, bot_share_whitelist_ids="")
    monkeypatch.setattr("app.bot.protection.get_settings", lambda: settings)
    captured = {}

    async def make_request(bot, method):
        captured["method"] = method

    method = SendPhoto(chat_id=555, photo="https://img/x.jpg", caption="c")
    asyncio.run(ContentProtectionMiddleware()(make_request, bot=None, method=method))
    assert captured["method"].protect_content is True


def test_rich_cards_default_is_three() -> None:
    assert Settings(_env_file=None).bot_rich_cards_per_cycle == 3


# --- хендлеры: «Не то» → «Вернуть», /start, «Доступ» -------------------------


class _HandlerBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))

    async def send_photo(self, chat_id, photo, **kwargs):
        self.photos.append((chat_id, photo))


class _HandlerMessage:
    def __init__(self, bot: _HandlerBot | None = None, reply_markup=None) -> None:
        from types import SimpleNamespace

        self.bot = bot or _HandlerBot()
        self.chat = SimpleNamespace(id=777)
        self.from_user = SimpleNamespace(id=777, username=None, first_name="T")
        self.reply_markup = reply_markup
        self.answers: list[tuple[str, object]] = []
        self.edited_markups: list[object] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        self.edited_markups.append(reply_markup)
        self.reply_markup = reply_markup


class _Query:
    def __init__(self, message: _HandlerMessage) -> None:
        from types import SimpleNamespace

        self.message = message
        self.from_user = SimpleNamespace(id=777)
        self.alerts: list[str | None] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.alerts.append(text)


def test_hide_then_unhide_redraws_card_buttons(monkeypatch) -> None:
    from app.bot import handlers

    url = "https://www.mashina.kg/details/toyota-camry-1/"
    monkeypatch.setattr(
        handlers.db,
        "hide_from_listing",
        lambda telegram_id, listing_id: botdb.BlockResult(ok=True, label="Toyota Camry", block_id=5),
    )
    monkeypatch.setattr(handlers.db, "delete_block", lambda telegram_id, block_id: block_id == 5)
    message = _HandlerMessage(reply_markup=card_kb(42, url))
    query = _Query(message)

    asyncio.run(handlers.hide_cb(query, HideCB(listing_id=42)))
    assert query.alerts == ["Скрыто: Toyota Camry. Больше не покажу"]
    [after_hide] = message.edited_markups
    assert after_hide.inline_keyboard[0][0].text == "↩️ Вернуть Toyota Camry"
    assert markup_url(after_hide) == url  # ссылка на объявление не пропала

    asyncio.run(handlers.unhide_cb(query, UnhideCB(block_id=5, listing_id=42)))
    assert query.alerts[-1] == "Вернул в выдачу"
    assert message.reply_markup.inline_keyboard == card_kb(42, url).inline_keyboard


def _start_info(**overrides) -> botdb.StartInfo:
    values = dict(
        access_status="none",
        trial_used=False,
        trial_active=False,
        trial_expires_at=None,
        subscription_active=False,
        referrer_attached=False,
        first_start=True,
    )
    values.update(overrides)
    return botdb.StartInfo(**values)


def _showcase_page(count: int):
    from app.bot.matching import PendingNotification

    return [
        PendingNotification(
            chat_id=777,
            listing_id=index,
            filter_id=0,
            title="Toyota Camry",
            year=2019,
            mileage_km=90_000,
            price_usd=10_000.0,
            city="Бишкек",
            url=f"https://www.mashina.kg/details/x-{index}/",
            below_market_percent=20.0,
            market_avg_usd=12_500.0,
            market_samples=31,
            brand_name="Toyota",
            model_name="Camry",
            image_url=f"https://img/{index}.webp",
        )
        for index in range(count)
    ]


def _run_start(monkeypatch, info: botdb.StartInfo, page: list) -> _HandlerMessage:
    from app.bot import handlers

    monkeypatch.setattr(handlers.db, "handle_start", lambda *args, **kwargs: info)
    monkeypatch.setattr(handlers.db, "get_showcase", lambda chat_id, offset: (page, None))
    monkeypatch.setattr("app.bot.notifier.PER_CHAT_DELAY_SECONDS", 0)
    monkeypatch.setattr("app.bot.notifier.GLOBAL_DELAY_SECONDS", 0)
    message = _HandlerMessage()
    asyncio.run(handlers.cmd_start(message, None))
    return message


def test_start_shows_three_photo_cards_then_trial_button(monkeypatch) -> None:
    from app.bot.keyboards import TrialCB
    from app.bot.messages import START_TRIAL_PROMPT_TEXT

    message = _run_start(monkeypatch, _start_info(), _showcase_page(3))
    assert [photo for _, photo in message.bot.photos] == [f"https://img/{i}.webp" for i in range(3)]
    last_text, last_markup = message.answers[-1]
    assert last_text == START_TRIAL_PROMPT_TEXT
    assert last_markup.inline_keyboard[0][0].callback_data.startswith(TrialCB.__prefix__)


def test_start_without_showcase_still_offers_trial(monkeypatch) -> None:
    from app.bot.messages import DEMO_UNAVAILABLE_TEXT

    message = _run_start(monkeypatch, _start_info(), [])
    assert message.bot.photos == []
    assert message.answers[-1][0] == DEMO_UNAVAILABLE_TEXT
    assert message.answers[-1][1] is not None


def test_start_shows_showcase_to_users_with_access_too(monkeypatch) -> None:
    message = _run_start(monkeypatch, _start_info(trial_active=True), _showcase_page(3))
    assert len(message.bot.photos) == 3


def test_access_button_shows_status_then_plans(monkeypatch) -> None:
    from app.bot import handlers

    info = botdb.SubscriptionInfo(access_status="none", expires_at=None, active=False)
    monkeypatch.setattr(handlers.db, "get_subscription_info", lambda telegram_id: info)
    settings = Settings(_env_file=None)
    monkeypatch.setattr(settings.__class__, "subscription_allowed_for", lambda self, tid: True)
    monkeypatch.setattr(handlers, "get_settings", lambda: settings)
    message = _HandlerMessage()
    asyncio.run(handlers.menu_access(message))
    texts = [text for text, _ in message.answers]
    assert texts[0].startswith("❌ Подписка неактивна")
    assert message.answers[-1][1] is not None  # кнопки тарифов
