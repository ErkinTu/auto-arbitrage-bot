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
        _listing(s, ids, current_price_usd=Decimal("18000"))  # −10 % — выгодно
        _listing(s, ids, current_price_usd=Decimal("19000"))  # −5 % ровно — в рынке
        _listing(s, ids, current_price_usd=Decimal("20400"))  # +2 % — в рынке
        _listing(s, ids, current_price_usd=Decimal("21000"))  # +5 % ровно — в рынке
        _listing(s, ids, current_price_usd=Decimal("21200"))  # +6 % — дорого
        items = feed_page(s, FeedFilter(), sort="price_asc")["items"]
    assert [i["market"]["verdict"] for i in items] == ["good", "market", "market", "market", "high"]


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


def test_spec_filters_ignore_case_and_skip_unknown(db) -> None:
    Factory, ids = db
    with Factory() as s:
        sedan = _listing(s, ids, body_type="Sedan", gearbox="Automatic", drive_type="AWD",
                         steering_wheel="Left", customs_status="cleared")
        sedan_lower = _listing(s, ids, body_type="sedan ", gearbox="automatic")
        suv = _listing(s, ids, body_type="SUV", gearbox="Manual", customs_status="not_cleared")
        unknown = _listing(s, ids, body_type=None, gearbox=None)

        def found(**kw) -> set[int]:
            return {i["id"] for i in feed_page(s, FeedFilter(**kw))["items"]}

        assert found(body_types=["Sedan"]) == {sedan.id, sedan_lower.id}
        assert found(body_types=["sedan", "SUV"]) == {sedan.id, sedan_lower.id, suv.id}
        assert found(gearboxes=["Manual"]) == {suv.id}
        assert found(drive_types=["awd"]) == {sedan.id}
        assert found(steering=["Left"]) == {sedan.id}
        assert found(customs="cleared") == {sedan.id}
        assert found(customs="not_cleared") == {suv.id}
        assert unknown.id not in found(body_types=["Sedan", "SUV"])


def test_facets_group_spellings_and_drop_rare(db, monkeypatch) -> None:
    from app.services import public_feed

    monkeypatch.setattr(public_feed, "FACET_MIN_COUNT", 2)
    Factory, ids = db
    with Factory() as s:
        for body in ["Седан", "седан", "Кроссовер", "внедорожник 5 дв.", "Лимузин", "-1"]:
            _listing(s, ids, body_type=body)
        _listing(s, ids, body_type="Купе", is_active=False)
        _listing(s, ids, body_type="Купе", is_active=False)
        facets = public_feed.feed_facets(s)
    assert facets["body_types"] == [
        {"value": "sedan", "label": "Седан", "count": 2},
        {"value": "suv", "label": "Внедорожник / кроссовер", "count": 2},
    ]
    assert facets["cities"] == [{"value": "Бишкек", "count": 6}]
    assert [c["value"] for c in facets["customs"]] == ["cleared", "not_cleared"]


def test_facets_and_spec_params_over_http(client) -> None:
    client, _, _ = client
    assert client.get("/api/v1/public/feed/facets").status_code == 200
    ok = client.get("/api/v1/public/feed", params={"body_type": "sedan,suv", "customs": "cleared"})
    assert ok.status_code == 200
    assert client.get("/api/v1/public/feed", params={"body_type": "Sedan"}).status_code == 422
    assert client.get("/api/v1/public/feed", params={"customs": "maybe"}).status_code == 422


def test_histograms_bucket_overflow_and_ignore_own_range(db) -> None:
    from app.services.public_feed import feed_histograms

    Factory, ids = db
    with Factory() as s:
        _listing(s, ids, current_price_usd=Decimal("1500"), year=1980, mileage_km=5_000)
        _listing(s, ids, current_price_usd=Decimal("3999"), year=2019, mileage_km=None)
        _listing(s, ids, current_price_usd=Decimal("250000"), year=2019, mileage_km=900_000)
        _listing(s, ids, current_price_usd=Decimal("20000"), year=2019, brand_id=ids["lexus"],
                 car_model_id=ids["rx"])
        h = feed_histograms(s, FeedFilter(price_from=3000, brand_ids=[ids["toyota"]]))

    price = h["price"]
    assert (price["min"], price["step"], price["max"]) == (0, 2_000, 50_000)
    # Свой диапазон (price_from) не применён, марка — применена (Lexus не считается).
    assert price["counts"][0] == 1 and price["counts"][1] == 1 and price["counts"][-1] == 1
    assert sum(price["counts"]) == 3
    # Год до 1995 — в первую корзину; пробег NULL не считается, 900k — в последнюю.
    assert h["year"]["counts"][0] == 0  # 1980 отрезан price_from=3000 (цена 1500)
    assert sum(h["mileage"]["counts"]) == 1 and h["mileage"]["counts"][-1] == 1


def test_histograms_over_http(client) -> None:
    client, _, _ = client
    r = client.get("/api/v1/public/feed/histograms", params={"price_from": 1000})
    assert r.status_code == 200
    assert set(r.json()) == {"price", "year", "mileage"}


def test_region_filter_over_http_and_junk_facets(client) -> None:
    client, Factory, ids = client
    with Factory() as s:
        bish = _listing(s, ids, city="Бишкек", gearbox="-1")
        karakol = _listing(s, ids, city="Каракол")
        china = _listing(s, ids, city="Китай")
        village = _listing(s, ids, city="с. Неизвестное")

    def ids_for(**params) -> set[int]:
        r = client.get("/api/v1/public/feed", params=params)
        assert r.status_code == 200, r.text
        return {i["id"] for i in r.json()["items"]}

    assert ids_for(region="bishkek") == {bish.id}
    assert ids_for(region="issykkul,foreign") == {karakol.id, china.id}
    assert village.id not in ids_for(region="chuy")
    assert client.get("/api/v1/public/feed", params={"region": "mars"}).status_code == 422

    facets = client.get("/api/v1/public/feed/facets").json()
    assert {r["value"]: r["count"] for r in facets["regions"]} == {"bishkek": 1, "issykkul": 1, "foreign": 1}
    assert facets["regions"][0]["label"] == "Бишкек"
    assert all(g["value"] != "-1" for g in facets["gearboxes"])


def test_similar_same_model_close_year_with_photo(client) -> None:
    client, Factory, ids = client
    with Factory() as s:
        me = _listing(s, ids, year=2019, current_price_usd=Decimal("20000"))
        close = _listing(s, ids, year=2019, current_price_usd=Decimal("20500"))
        far_price = _listing(s, ids, year=2019, current_price_usd=Decimal("30000"))
        older = _listing(s, ids, year=2017)
        _listing(s, ids, year=2015)  # вне ±2
        _listing(s, ids, year=2019, image_url=None)  # без фото
        _listing(s, ids, brand_id=ids["lexus"], car_model_id=ids["rx"])  # другая модель
        _listing(s, ids, year=2019, is_active=False)

    r = client.get(f"/api/v1/public/listings/{me.id}/similar")
    assert r.status_code == 200
    assert [i["id"] for i in r.json()] == [close.id, far_price.id, older.id]
    assert client.get("/api/v1/public/listings/999999/similar").status_code == 404


def test_spec_groups_over_http_cover_all_spellings(client) -> None:
    client, Factory, ids = client
    with Factory() as s:
        a = _listing(s, ids, body_type="Кроссовер", gearbox="Типтроник")
        b = _listing(s, ids, body_type="внедорожник 5 дв.", gearbox="автомат")
        c = _listing(s, ids, body_type="седан", gearbox="механика")

    def ids_for(**params) -> set[int]:
        r = client.get("/api/v1/public/feed", params=params)
        assert r.status_code == 200, r.text
        return {i["id"] for i in r.json()["items"]}

    assert ids_for(body_type="suv") == {a.id, b.id}
    assert ids_for(gearbox="automatic") == {a.id, b.id}
    assert ids_for(gearbox="manual", body_type="sedan") == {c.id}
    assert ids_for(drive="awd") == set()  # группа без объявлений — пусто, а не «всё»
