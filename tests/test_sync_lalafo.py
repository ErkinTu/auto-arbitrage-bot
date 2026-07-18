from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Listing, ListingPriceHistory, Source, SyncTarget
from app.parsers.base import FetchPage, ParserAdapter
from app.services.catalog_service import seed_default_catalog
from app.services.normalizer import NormalizedListing, normalize_lalafo_listing
from app.services.sync import run_sync


def _session() -> Session:
    import app.models  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    seed_default_catalog(session)
    session.commit()
    return session


def _feed_item(ad_id: int, price: int, currency: str) -> dict[str, Any]:
    return {
        "id": ad_id,
        "title": "Toyota Camry: 2016 г., 2.5 л, Вариатор, Гибрид, Седан",
        "url": f"/bishkek/ads/toyota-camry-id-{ad_id}",
        "price": price,
        "currency": currency,
        "national_price": {"price": price * 87 if currency == "USD" else price},
        "city": "Бишкек",
        "created_time": 1784050897,
        "updated_time": 1784050897,
        "params": [],
    }


def _detail_item(ad_id: int, price: int, currency: str) -> dict[str, Any]:
    detail = _feed_item(ad_id, price, currency)
    detail["params"] = [
        {"name": "Модель", "value": "Camry"},
        {"name": "Год", "value": "2016"},
        {"name": "Пробег (км.)", "value": "182000"},
    ]
    return detail


class FakeLalafoParser(ParserAdapter):
    source_name = "lalafo.kg"
    needs_detail_enrichment = True

    def __init__(self, feed: list[dict], details: dict[int, dict], usd_rate_kgs: Decimal | None) -> None:
        self.feed = feed
        self.details = details
        self.usd_rate_kgs = usd_rate_kgs
        self.detail_calls: list[int] = []

    def fetch_listings(self, **_: Any) -> FetchPage:
        return FetchPage(items=self.feed, total=len(self.feed))

    def normalize(self, raw: dict[str, Any], *, fallback_make: str | None = None) -> NormalizedListing:
        return normalize_lalafo_listing(raw, fallback_make=fallback_make, usd_rate_kgs=self.usd_rate_kgs)

    def fetch_details(self, external_id: str | int) -> dict[str, Any]:
        self.detail_calls.append(int(external_id))
        return self.details[int(external_id)]

    def close(self) -> None:
        pass


def _setup(session: Session) -> tuple[Source, SyncTarget]:
    source = Source(name="lalafo.kg", base_url="https://lalafo.kg")
    session.add(source)
    session.flush()
    target = SyncTarget(source_id=source.id, brand_filter="Toyota", page_size=20, max_pages=1, max_items=50)
    session.add(target)
    session.commit()
    return source, target


def test_lalafo_sync_new_listing_enriched_and_converted() -> None:
    session = _session()
    source, target = _setup(session)
    parser = FakeLalafoParser(
        feed=[_feed_item(101, 395000, "KGS")],
        details={101: _detail_item(101, 395000, "KGS")},
        usd_rate_kgs=Decimal("87.45"),
    )

    summary = run_sync(session, parser, source, target, mode="fresh")

    assert summary.new == 1
    assert parser.detail_calls == [101]  # новое объявление -> дотянули карточку
    listing = session.scalar(select(Listing).where(Listing.external_id == "101"))
    assert listing is not None
    assert listing.mileage_km == 182000  # данные из карточки, не из ленты
    assert listing.current_price_usd == Decimal("4517")
    assert listing.current_price_kgs == Decimal("395000")


def test_lalafo_sync_unchanged_listing_skipped_without_detail_fetch() -> None:
    session = _session()
    source, target = _setup(session)
    detail = _detail_item(101, 395000, "KGS")
    parser = FakeLalafoParser(feed=[_feed_item(101, 395000, "KGS")], details={101: detail}, usd_rate_kgs=Decimal("87.45"))
    run_sync(session, parser, source, target, mode="fresh")

    # Второй цикл: то же объявление, та же цена, но курс NBKR изменился.
    parser2 = FakeLalafoParser(feed=[_feed_item(101, 395000, "KGS")], details={101: detail}, usd_rate_kgs=Decimal("88.10"))
    summary2 = run_sync(session, parser2, source, target, mode="fresh")

    assert parser2.detail_calls == []  # карточку не перечитывали
    assert summary2.new == 0 and summary2.price_changed == 0
    history = session.scalars(select(ListingPriceHistory)).all()
    assert len(history) == 1  # только initial: смена курса — не смена цены
    listing = session.scalar(select(Listing).where(Listing.external_id == "101"))
    assert listing.mileage_km == 182000  # данные карточки не затерты лентой


class PagedFakeParser(FakeLalafoParser):
    """Постраничная лента для проверки catch-up пагинации."""

    def __init__(self, pages: dict[int, list[dict]], details: dict[int, dict], usd_rate_kgs: Decimal | None) -> None:
        super().__init__(feed=[], details=details, usd_rate_kgs=usd_rate_kgs)
        self.pages = pages
        self.requested_pages: list[int] = []

    def fetch_listings(self, *, page: int, **_: Any) -> FetchPage:
        self.requested_pages.append(page)
        return FetchPage(items=self.pages.get(page, []), total=None)


def test_catchup_extends_until_known_listing() -> None:
    session = _session()
    source, target = _setup(session)
    # Объявление 100 уже известно с той же ценой (было в БД до простоя).
    known_detail = _detail_item(100, 395000, "KGS")
    run_sync(
        session,
        FakeLalafoParser(feed=[_feed_item(100, 395000, "KGS")], details={100: known_detail}, usd_rate_kgs=None),
        source,
        target,
        mode="fresh",
    )

    # После простоя страница 1 полностью новая; на странице 2 встречается известное.
    details = {ad_id: _detail_item(ad_id, 400000, "KGS") for ad_id in (101, 102, 103)}
    parser = PagedFakeParser(
        pages={
            1: [_feed_item(101, 400000, "KGS"), _feed_item(102, 400000, "KGS")],
            2: [_feed_item(103, 400000, "KGS"), _feed_item(100, 395000, "KGS")],
            3: [_feed_item(104, 400000, "KGS")],
        },
        details=details,
        usd_rate_kgs=None,
    )
    summary = run_sync(
        session,
        parser,
        source,
        target,
        mode="fresh",
        page_size_override=2,
        max_pages_override=1,
        max_items_override=2,
        catchup_max_pages=3,
    )

    # Страница 2 дочитана (вся 1-я новая), страница 3 — нет (на 2-й встретили известное).
    assert parser.requested_pages == [1, 2]
    assert summary.new == 3


def test_catchup_respects_page_cap() -> None:
    session = _session()
    source, target = _setup(session)
    details = {ad_id: _detail_item(ad_id, 400000, "KGS") for ad_id in range(101, 109)}
    parser = PagedFakeParser(
        pages={
            page: [_feed_item(101 + (page - 1) * 2, 400000, "KGS"), _feed_item(102 + (page - 1) * 2, 400000, "KGS")]
            for page in (1, 2, 3, 4)
        },
        details=details,
        usd_rate_kgs=None,
    )
    summary = run_sync(
        session,
        parser,
        source,
        target,
        mode="fresh",
        page_size_override=2,
        max_pages_override=1,
        max_items_override=2,
        catchup_max_pages=2,
    )

    # 1 базовая + максимум 2 catch-up страницы, хотя всё ещё «всё новое».
    assert parser.requested_pages == [1, 2, 3]
    assert summary.new == 6


def test_lalafo_sync_price_change_refetches_details() -> None:
    session = _session()
    source, target = _setup(session)
    parser = FakeLalafoParser(
        feed=[_feed_item(101, 395000, "KGS")],
        details={101: _detail_item(101, 395000, "KGS")},
        usd_rate_kgs=Decimal("87.45"),
    )
    run_sync(session, parser, source, target, mode="fresh")

    parser2 = FakeLalafoParser(
        feed=[_feed_item(101, 380000, "KGS")],
        details={101: _detail_item(101, 380000, "KGS")},
        usd_rate_kgs=Decimal("87.45"),
    )
    summary2 = run_sync(session, parser2, source, target, mode="fresh")

    assert parser2.detail_calls == [101]  # цена изменилась -> перечитали карточку
    assert summary2.price_changed == 1
    listing = session.scalar(select(Listing).where(Listing.external_id == "101"))
    assert listing.current_price_kgs == Decimal("380000")
    assert listing.current_price_usd == Decimal("4345")  # 380000 / 87.45
