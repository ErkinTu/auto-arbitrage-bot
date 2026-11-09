"""Публичная лента сайта: фильтры, курсор, аналитика поверх, отсутствие ПДн."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    Brand,
    CarModel,
    DailyMarketStat,
    Listing,
    ListingPriceHistory,
    Source,
)
from app.schemas import PublicListingDetail
from app.services.public_feed import FeedFilter, decode_cursor, feed_count, feed_page


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def db():
    import app.models  # noqa: F401

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Factory() as session:
        mashina = Source(name="mashina.kg")
        lalafo = Source(name="lalafo.kg")
        toyota = Brand(name="Toyota")
        lexus = Brand(name="Lexus")
        session.add_all([mashina, lalafo, toyota, lexus])
        session.flush()
        camry = CarModel(brand_id=toyota.id, name="Camry")
        rx = CarModel(brand_id=lexus.id, name="RX")
        session.add_all([camry, rx])
        session.commit()
        ids = {
            "mashina": mashina.id,
            "lalafo": lalafo.id,
            "toyota": toyota.id,
            "lexus": lexus.id,
            "camry": camry.id,
            "rx": rx.id,
        }
    return Factory, ids


_counter = iter(range(1, 10_000))


def _listing(session: Session, ids: dict, **kw) -> Listing:
    n = next(_counter)
    defaults = dict(
        source_id=ids["mashina"],
        external_id=f"ext{n}",
        brand_id=ids["toyota"],
        car_model_id=ids["camry"],
        title="Toyota Camry",
        source_slug=f"toyota-camry-{n}",
        year=2019,
        mileage_km=90_000,
        city="Бишкек",
        customs_status="unknown",
        current_price_usd=Decimal("20000"),
        is_active=True,
        payload_hash="x" * 8,
        first_seen_at=_now() - timedelta(minutes=n),
        seller_phone="996555123456",
        image_url="https://cdn.example/photo.jpg",
    )
    defaults.update(kw)
    listing = Listing(**defaults)
    session.add(listing)
    session.commit()
    return listing


def _stat(session: Session, ids: dict, *, avg: str, samples: int = 20, year: int = 2019) -> None:
    session.add(
        DailyMarketStat(
            stat_date=date.today(),
            brand_id=ids["toyota"],
            car_model_id=ids["camry"],
            generation_id=None,
            year=year,
            mileage_bucket="50-100k",
            customs_status="unknown",
            avg_price_usd=Decimal(avg),
            median_price_usd=Decimal(avg),
            min_price_usd=Decimal(avg),
            max_price_usd=Decimal(avg),
            sample_count=samples,
        )
    )
    session.commit()


@pytest.fixture()
def client(db):
    Factory, ids = db

    def _dep():
        with Factory() as session:
            yield session

    app.dependency_overrides[get_session] = _dep
    yield TestClient(app), Factory, ids
    app.dependency_overrides.clear()


# --- безопасность -------------------------------------------------------------


def test_no_personal_data_in_public_schema() -> None:
    fields = set(PublicListingDetail.model_fields)
    assert "seller_phone" not in fields
    assert not {"raw_payload", "title", "description", "quality_flags"} & fields


def test_feed_response_never_contains_phone(client) -> None:
    http, Factory, ids = client
    with Factory() as s:
        _listing(s, ids)
    body = http.get("/api/v1/public/feed").text
    assert "996555123456" not in body
    assert "seller_phone" not in body


# --- лента ----------------------------------------------------------------------


def test_feed_returns_market_deviation(client) -> None:
    http, Factory, ids = client
    with Factory() as s:
        _stat(s, ids, avg="25000")
        _listing(s, ids, current_price_usd=Decimal("20000"))
    item = http.get("/api/v1/public/feed").json()["items"][0]
    assert item["brand"] == "Toyota" and item["model"] == "Camry"
    assert item["market"] == {
        "avg_price_usd": 25000,
        "samples": 20,
        "diff_usd": -5000,
        "diff_percent": -20.0,
        "verdict": "good",
    }
    assert item["source"] == "mashina"


def test_market_is_none_without_enough_samples_or_for_garbage_price(client) -> None:
    http, Factory, ids = client
    with Factory() as s:
        _stat(s, ids, avg="25000", samples=2)
        _listing(s, ids)
    assert http.get("/api/v1/public/feed").json()["items"][0]["market"] is None


def test_garbage_discount_is_not_shown(db) -> None:
    Factory, ids = db
    with Factory() as s:
        _stat(s, ids, avg="25000")
        _listing(s, ids, current_price_usd=Decimal("2500"))  # −90 %: мусор цены
        page = feed_page(s, FeedFilter())
    assert page["items"][0]["market"] is None


def test_verdict_threshold(db) -> None:
    Factory, ids = db
    with Factory() as s:
        _stat(s, ids, avg="20000")
        _listing(s, ids, current_price_usd=Decimal("20400"))  # +2 % — в рынке
        _listing(s, ids, current_price_usd=Decimal("21000"))  # +5 % — дорого
        items = feed_page(s, FeedFilter(), sort="price_asc")["items"]
    assert [i["market"]["verdict"] for i in items] == ["market", "high"]


def test_inactive_and_unrecognized_are_hidden_from_feed(db) -> None:
    Factory, ids = db
    with Factory() as s:
        _listing(s, ids, is_active=False)
        _listing(s, ids, car_model_id=None)
        _listing(s, ids, current_price_usd=None)
        visible = _listing(s, ids)
        items = feed_page(s, FeedFilter())["items"]
    assert [i["id"] for i in items] == [visible.id]


def test_filters(db) -> None:
    Factory, ids = db
    with Factory() as s:
        cheap = _listing(s, ids, current_price_usd=Decimal("9000"), city="Ош")
        lexus = _listing(s, ids, brand_id=ids["lexus"], car_model_id=ids["rx"])
        lalafo = _listing(s, ids, source_id=ids["lalafo"], mileage_km=None, seller_type="dealer")

        def found(**kw) -> set[int]:
            return {i["id"] for i in feed_page(s, FeedFilter(**kw))["items"]}

        assert found(brand_ids=[ids["lexus"]]) == {lexus.id}
        assert found(price_to=10_000) == {cheap.id}
        assert found(cities=["Ош"]) == {cheap.id}  # регистр: Postgres lower() кириллицу знает, SQLite — нет
        assert found(source="lalafo") == {lalafo.id}
        assert found(seller_type="dealer") == {lalafo.id}
        # Неизвестный пробег под ограничение пробега не попадает.
        assert lalafo.id not in found(mileage_to=200_000)


def test_price_dropped(db) -> None:
    Factory, ids = db
    with Factory() as s:
        dropped = _listing(s, ids, current_price_usd=Decimal("19000"))
        _listing(s, ids)
        s.add(
            ListingPriceHistory(
                listing_id=dropped.id, event_type="price_changed", price_usd=Decimal("20000"), observed_at=_now() - timedelta(days=1)
            )
        )
        s.commit()
        items = feed_page(s, FeedFilter(price_dropped=True))["items"]
    assert [i["id"] for i in items] == [dropped.id]
    assert items[0]["price_drop_usd"] == 1000


def test_cursor_pagination_has_no_gaps_or_duplicates(db) -> None:
    Factory, ids = db
    with Factory() as s:
        created = [_listing(s, ids).id for _ in range(7)]
        seen: list[int] = []
        cursor = None
        while True:
            page = feed_page(s, FeedFilter(), limit=3, cursor=cursor)
            seen += [i["id"] for i in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
    assert seen == created  # свежие первыми = порядок создания (first_seen_at убывает)
    assert len(set(seen)) == 7


def test_deal_filter_scans_past_non_matching(db) -> None:
    Factory, ids = db
    with Factory() as s:
        _stat(s, ids, avg="20000")
        for _ in range(5):
            _listing(s, ids, current_price_usd=Decimal("21000"))  # дорого
        good = _listing(s, ids, current_price_usd=Decimal("15000"), first_seen_at=_now() - timedelta(days=2))
        page = feed_page(s, FeedFilter(deal="good"), limit=3)
        count = feed_count(s, FeedFilter(deal="good"))
    assert [i["id"] for i in page["items"]] == [good.id]
    assert page["next_cursor"] is None
    assert count == {"count": 1, "approximate": False}


def test_bad_cursor_is_400(client) -> None:
    http, _, _ = client
    assert http.get("/api/v1/public/feed", params={"cursor": "garbage!!"}).status_code == 400


def test_cursor_from_other_sort_is_rejected(db) -> None:
    Factory, ids = db
    with Factory() as s:
        _listing(s, ids)
        _listing(s, ids)
        cursor = feed_page(s, FeedFilter(), limit=1)["next_cursor"]
    with pytest.raises(ValueError):
        decode_cursor("price_asc", cursor)


def test_validation(client) -> None:
    http, _, _ = client
    assert http.get("/api/v1/public/feed", params={"source": "avito"}).status_code == 422
    assert http.get("/api/v1/public/feed", params={"fuel": "nuclear"}).status_code == 422
    assert http.get("/api/v1/public/feed", params={"brand_ids": "1,x"}).status_code == 422


# --- избранное и карточка --------------------------------------------------------


def test_favorites_include_inactive(client) -> None:
    http, Factory, ids = client
    with Factory() as s:
        sold = _listing(s, ids, is_active=False, deactivated_at=_now())
        live = _listing(s, ids)
    items = http.get("/api/v1/public/listings", params={"ids": f"{sold.id},{live.id},999"}).json()
    assert [(i["id"], i["is_active"]) for i in items] == [(sold.id, False), (live.id, True)]


def test_hd_image_for_mashina_only() -> None:
    from app.services.public_feed import hd_image

    assert hd_image("https://s3.mashina.kg/images/x/abc_medium.webp") == "https://s3.mashina.kg/images/x/abc_large.webp"
    lalafo = "https://img5.lalafo.com/i/posters/original/a7/68/fe/x.jpeg"
    assert hd_image(lalafo) == lalafo
    assert hd_image(None) is None


def test_listing_detail(client) -> None:
    http, Factory, ids = client
    with Factory() as s:
        listing = _listing(
            s, ids, image_urls=["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"]
        )
        s.add(ListingPriceHistory(listing_id=listing.id, event_type="price_changed", price_usd=Decimal("21000"), observed_at=_now()))
        s.commit()
    body = http.get(f"/api/v1/public/listings/{listing.id}").json()
    assert body["images"] == ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"]
    assert body["photo_count"] == 2
    # Гостю оценка есть, а график истории цены — только с подпиской.
    assert body["price_history"] == [] and body["price_history_locked"] is True
    assert http.get("/api/v1/public/listings/424242").status_code == 404


def test_public_responses_are_cacheable(client) -> None:
    http, Factory, ids = client
    with Factory() as s:
        _listing(s, ids)
    response = http.get("/api/v1/public/feed")
    assert response.headers["cache-control"].startswith("public, max-age=")


def test_batch_estimates_match_bot(db) -> None:
    """Сайт и бот обязаны называть одну «выгоду»: пакетная оценка = market_estimate."""
    from app.bot.matching import market_estimate
    from app.services.public_feed import batch_market_estimates

    Factory, ids = db
    with Factory() as s:

        def stat(bucket: str, avg: str, samples: int, days_ago: int = 0, year: int = 2019) -> None:
            s.add(
                DailyMarketStat(
                    stat_date=date.today() - timedelta(days=days_ago),
                    brand_id=ids["toyota"], car_model_id=ids["camry"], generation_id=None,
                    year=year, mileage_bucket=bucket, customs_status="unknown",
                    avg_price_usd=Decimal(avg), median_price_usd=Decimal(avg),
                    min_price_usd=Decimal(avg), max_price_usd=Decimal(avg), sample_count=samples,
                )
            )

        stat("50-100k", "20000", 6)
        stat("50-100k", "99999", 50, days_ago=3)  # старая дата бакета — не берётся
        stat("100-150k", "17000", 3, days_ago=1)  # своя последняя дата, но мало образцов
        stat("0-50k", "26000", 9, days_ago=1)
        stat("50-100k", "30000", 2, year=2020)  # мало образцов
        stat("50-100k", "15000", 8, days_ago=40, year=2015)  # только старше окна в 7 дней
        s.commit()
        listings = [
            _listing(s, ids, mileage_km=90_000),
            _listing(s, ids, mileage_km=120_000),
            _listing(s, ids, mileage_km=10_000),
            _listing(s, ids, mileage_km=None),  # все бакеты на последнюю дату
            _listing(s, ids, mileage_km=None, year=2020),
            _listing(s, ids, mileage_km=90_000, city="Китай"),  # зарубежное
            _listing(s, ids, mileage_km=90_000, year=2005),  # нет статистики
            _listing(s, ids, mileage_km=90_000, year=2015),  # статистика 40 дней назад
        ]
        batch = batch_market_estimates(s, listings, min_samples=5)
        for listing in listings:
            bot = market_estimate(s, listing, min_samples=5)
            got = batch[listing.id]
            if bot is None:
                assert got is None, listing.mileage_km
            else:
                assert got is not None and got.samples == bot.samples
                assert abs(got.avg_usd - bot.avg_usd) < 1e-6
