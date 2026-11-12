"""Публичная лента сайта: фильтры, курсор, аналитика поверх, отсутствие ПДн."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
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
        stat("100-150k", "14000", 7, days_ago=45, year=2015)
        stat("150-200k", "12000", 9, days_ago=30)  # когорта в окне есть, этот бакет — только старый
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
            _listing(s, ids, mileage_km=None, year=2015),  # без пробега, всё старше окна
            _listing(s, ids, mileage_km=170_000),  # старый бакет у свежей когорты
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
    # Цена +50 % дальше, чем год −2 (6 % за год): единая мера близости.
    assert [i["id"] for i in r.json()] == [close.id, older.id, far_price.id]
    assert client.get("/api/v1/public/listings/999999/similar").status_code == 404


def test_similar_prefers_close_mileage(client) -> None:
    """Пробег учитывается (11.11.2026): та же цена и год, но 250 тыс. км — ниже."""
    client, Factory, ids = client
    with Factory() as s:
        me = _listing(s, ids, year=2019, mileage_km=60_000)
        worn = _listing(s, ids, year=2019, mileage_km=250_000)
        alike = _listing(s, ids, year=2019, mileage_km=70_000)
    r = client.get(f"/api/v1/public/listings/{me.id}/similar")
    assert [i["id"] for i in r.json()] == [alike.id, worn.id]


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


def test_similar_sort_orders_by_closeness_and_excludes_reference(db) -> None:
    """«Искать дальше» на странице авто: ближе по цене/году/пробегу — выше."""
    Factory, ids = db
    with Factory() as s:
        ref = _listing(s, ids, current_price_usd=Decimal("20000"), year=2018, mileage_km=100_000)
        far = _listing(s, ids, current_price_usd=Decimal("35000"), year=2023, mileage_km=10_000)
        near = _listing(s, ids, current_price_usd=Decimal("20500"), year=2018, mileage_km=110_000)
        mid = _listing(s, ids, current_price_usd=Decimal("23000"), year=2017, mileage_km=150_000)
        order: list[int] = []
        cursor = None
        while True:
            page = feed_page(s, FeedFilter(), sort="similar", near=ref.id, limit=2, cursor=cursor)
            order += [i["id"] for i in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert order == [near.id, mid.id, far.id]
        # Образца нет — обычная лента (свежие), без ошибки.
        fallback = feed_page(s, FeedFilter(), sort="similar", near=999_999, limit=10)
        assert len(fallback["items"]) == 4


def test_new_listings_appear_gradually_in_order(db, monkeypatch) -> None:
    """Плавное появление (11.11.2026): новые встают в ленту по одной за 5 минут
    после обхода, в порядке first_seen — каждое наверх «Свежих»."""
    from app.services import sync

    monkeypatch.setattr(sync.get_settings(), "site_feed_spread_seconds", 300)
    Factory, ids = db
    now = _now()
    with Factory() as s:
        old = _listing(s, ids, first_seen_at=now - timedelta(hours=1))
        batch = [_listing(s, ids, first_seen_at=now - timedelta(seconds=30 - i)) for i in range(3)]
        sync._schedule_feed_reveal(s, batch, now=now)
        s.commit()
        reveal = [b.feed_at for b in batch]
        assert reveal == sorted(reveal)
        assert reveal[-1] <= now + timedelta(seconds=301)

        def visible(at):
            monkeypatch.setattr("app.services.public_feed._now", lambda: at)
            return [i["id"] for i in feed_page(s, FeedFilter(), limit=10)["items"]]

        assert visible(now) == [old.id]  # сразу после обхода — ещё ни одного
        assert visible(now + timedelta(seconds=110)) == [batch[0].id, old.id]  # первое — наверх
        assert visible(now + timedelta(seconds=301)) == [batch[2].id, batch[1].id, batch[0].id, old.id]
        # Следующий обход встаёт в очередь после запланированных, в пределах окна.
        more = [_listing(s, ids, first_seen_at=now)]
        sync._schedule_feed_reveal(s, more, now=now)
        assert more[0].feed_at > reveal[-1] - timedelta(seconds=1)


def test_garbage_prices_are_hidden_everywhere(client) -> None:
    """«$2» и «$206» — мусор ввода: ни в ленте, ни в счётчике, ни на странице авто,
    ни в избранном. Порог — тот же, что у аналитики (MIN_PLAUSIBLE_PRICE_USD)."""
    http, Factory, ids = client
    with Factory() as s:
        ok = _listing(s, ids, current_price_usd=Decimal("500"))
        junk2 = _listing(s, ids, current_price_usd=Decimal("2"))
        junk206 = _listing(s, ids, current_price_usd=Decimal("206"))
        absurd = _listing(s, ids, current_price_usd=Decimal("999999999"))
    feed = http.get("/api/v1/public/feed").json()["items"]
    assert [i["id"] for i in feed] == [ok.id]
    assert http.get("/api/v1/public/feed/count").json()["count"] == 1
    for bad in (junk2, junk206, absurd):
        assert http.get(f"/api/v1/public/listings/{bad.id}").status_code == 404
    favs = http.get("/api/v1/public/listings", params={"ids": f"{junk2.id},{ok.id},{junk206.id}"}).json()
    assert [i["id"] for i in favs] == [ok.id]


def test_warmup_fills_exactly_the_keys_the_home_page_requests(client, monkeypatch) -> None:
    """Прогрев (services/warmup.py) обязан класть в кэш те же ключи, что строят
    ручки для главной: иначе он греет «не то», а посетитель всё равно ждёт."""
    from app.core.cache import response_cache
    from app.services import warmup

    http, Factory, ids = client
    with Factory() as s:
        _listing(s, ids)
    monkeypatch.setattr(warmup, "SessionLocal", Factory)
    monkeypatch.setattr(warmup, "_pass", 0)  # первый проход — полный
    assert warmup.warm_once() == 6
    misses = response_cache.misses
    for url in (
        "/api/v1/public/feed?with_photo=true&sort=fresh&limit=24",
        "/api/v1/public/feed/count?with_photo=true",
        "/api/v1/public/feed/histograms?with_photo=true",
        "/api/v1/public/best?limit=12",
        "/api/v1/public/feed/facets",
        "/api/v1/catalog/brands",
    ):
        assert http.get(url).status_code == 200, url
    assert response_cache.misses == misses, "главная пошла мимо прогретого кэша"


def test_lalafo_cards_get_the_light_photo_but_gallery_keeps_original(client) -> None:
    """Карточка — `posters/api` (370 px), галерея страницы авто — оригинал (1024 px)."""
    from app.services.public_feed import card_image

    original = "https://img5.lalafo.com/i/posters/original/e3/52/b2/abc.jpeg"
    light = "https://img5.lalafo.com/i/posters/api/e3/52/b2/abc.jpeg"
    assert card_image(original) == light
    assert card_image(light) == light
    assert card_image("https://s3.mashina.kg/images/x/abc_medium.webp") == "https://s3.mashina.kg/images/x/abc_medium.webp"
    assert card_image(None) is None

    http, Factory, ids = client
    with Factory() as s:
        lal = _listing(s, ids, source_id=ids["lalafo"], image_url=original, image_urls=None)
    item = http.get("/api/v1/public/feed").json()["items"][0]
    assert item["image_url"] == light and item["image_url_hd"] == light
    detail = http.get(f"/api/v1/public/listings/{lal.id}").json()
    assert detail["images"] == [original]


def test_site_best_filters_orders_and_does_not_pad(client) -> None:
    """«Лучшее сейчас» — не самое дешёвое: коридор скидки, ≥ 3 фото; мало кандидатов —
    отдаём сколько есть (без добивки мусором). Правила — services/best_picks.py."""
    http, Factory, ids = client
    photos = [f"https://cdn.example/{i}.jpg" for i in range(5)]
    with Factory() as s:
        _stat(s, ids, avg="25000", samples=60)
        good = _listing(s, ids, current_price_usd=Decimal("18250"), image_urls=photos)  # −27 %
        _listing(s, ids, current_price_usd=Decimal("10500"), image_urls=photos)  # −58 % — подозрительно
        _listing(s, ids, current_price_usd=Decimal("18500"), image_urls=photos[:2])  # 2 фото
        _listing(s, ids, current_price_usd=Decimal("24500"), image_urls=photos)  # скидка 2 % — не «лучшее»
    body = http.get("/api/v1/public/best").json()
    assert [i["id"] for i in body] == [good.id]


def test_feed_thumbs_cover_first_light_and_capped(client) -> None:
    """Карточка-строка: до 3 лёгких фото, обложка первой, без дублей."""
    from app.services.public_feed import card_thumbs

    orig = "https://img5.lalafo.com/i/posters/original/a/{}.jpeg"
    api = "https://img5.lalafo.com/i/posters/api/a/{}.jpeg"
    gallery = [orig.format(i) for i in range(5)]
    assert card_thumbs(orig.format(2), gallery) == [api.format(2), api.format(0), api.format(1)]
    assert card_thumbs(None, []) == []

    http, Factory, ids = client
    photos = [f"https://cdn.example/{i}.jpg" for i in range(5)]
    with Factory() as s:
        _listing(s, ids, image_url=photos[0], image_urls=photos)
    item = http.get("/api/v1/public/feed").json()["items"][0]
    assert item["thumbs"] == photos[:3] and item["photo_count"] == 5


# --- Живая проверка «не снято ли», фото и видео (services/liveness.py, 12.11.2026) ---


def _alive(images=None, video="HdYFcHOd6jo"):
    from app.services.liveness import Check, Verdict

    return Check(Verdict.ALIVE, images=images, video_id=video, has_video=True)


def test_youtube_id_from_any_link() -> None:
    from app.services.listing_media import youtube_id

    for url in (
        "https://youtu.be/HdYFcHOd6jo?si=x",
        "https://www.youtube.com/watch?v=HdYFcHOd6jo&t=3",
        "https://m.youtube.com/watch?feature=share&v=HdYFcHOd6jo",
        "https://youtube.com/shorts/HdYFcHOd6jo",
        "https://www.youtube.com/embed/HdYFcHOd6jo",
    ):
        assert youtube_id(url) == "HdYFcHOd6jo", url
    assert youtube_id("Вставьте ссылку на ваш ролик на YouTube") is None
    assert youtube_id("https://vimeo.com/123456789") is None
    assert youtube_id(None) is None


def test_mashina_detail_api_verdicts(monkeypatch) -> None:
    """API карточки mashina: 200+active — живо (с фото и видео), 404 — снято,
    другой статус — снято, 5xx/сеть — «не знаю»."""
    import httpx

    from app.services import liveness as lv

    detail = {
        "status": "active",
        "video_url": "https://youtu.be/HdYFcHOd6jo?si=1",
        "images": [{"id": 1, "medium": "https://s3.mashina.kg/images/2026-11-12/7/aaa_medium.webp"}],
    }
    answers = {
        "alive": httpx.Response(200, json=detail),
        "gone": httpx.Response(404, json={"error_code": "NOT_FOUND"}),
        "sold": httpx.Response(200, json={**detail, "status": "inactive"}),
        "down": httpx.Response(502),
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda req: answers[req.url.path.split("/")[-2]]))
    assert lv.mashina_detail_url("x").endswith("/v1/ads/x/detail")
    alive = lv.check_mashina("alive", client)
    assert alive.verdict is lv.Verdict.ALIVE and alive.video_id == "HdYFcHOd6jo"
    assert alive.images == ["https://s3.mashina.kg/images/2026-11-12/7/aaa_medium.webp"]
    assert lv.check_mashina("gone", client).verdict is lv.Verdict.GONE
    assert lv.check_mashina("sold", client).verdict is lv.Verdict.GONE
    assert lv.check_mashina("down", client).verdict is lv.Verdict.UNKNOWN
    assert lv.check_mashina(None).verdict is lv.Verdict.UNKNOWN


def test_media_on_open_takes_fresh_photos_and_video_once_per_window(client, monkeypatch) -> None:
    from app.services import liveness as lv

    calls: list[int] = []
    fresh = ["https://s3.mashina.kg/images/2026-11-12/7/aaa_medium.webp", "https://s3.mashina.kg/images/2026-11-12/7/bbb_medium.webp"]
    monkeypatch.setattr(lv, "check_listing", lambda source, listing: calls.append(listing.id) or _alive(fresh))
    http, Factory, ids = client
    with Factory() as s:
        car = _listing(s, ids, image_urls=["https://s3.mashina.kg/images/2026-11-09/7/old_medium.webp"],
                       last_seen_at=_now() - timedelta(hours=3))
    body = http.get(f"/api/v1/public/listings/{car.id}/media").json()
    assert body["video_id"] == "HdYFcHOd6jo" and body["photo_count"] == 2
    assert body["images"][0].endswith("aaa_large.webp")  # галерее — крупный вариант
    detail = http.get(f"/api/v1/public/listings/{car.id}").json()  # кэш карточки обновлён
    assert detail["video_id"] == "HdYFcHOd6jo" and len(detail["images"]) == 2
    from app.core.cache import response_cache

    response_cache.clear()
    http.get(f"/api/v1/public/listings/{car.id}/media")
    assert calls == [car.id]  # в окне — без запроса к источнику


def test_gone_listing_hidden_but_data_and_phone_kept(client, monkeypatch) -> None:
    """Снятое — неактивно для пользователей, но строка и номер продавца у нас остаются."""
    from app.services import liveness as lv

    monkeypatch.setattr(lv, "check_listing", lambda source, listing: lv.Check(lv.Verdict.GONE, "card_gone"))
    http, Factory, ids = client
    with Factory() as s:
        car = _listing(s, ids, last_seen_at=_now() - timedelta(hours=3))
    body = http.get(f"/api/v1/public/listings/{car.id}/media").json()
    assert body["is_active"] is False
    with Factory() as s:
        row = s.get(Listing, car.id)
        assert row.is_active is False and row.seller_phone == "996555123456" and row.verified_at is not None


def test_unknown_changes_nothing_and_recently_seen_is_not_checked(client, monkeypatch) -> None:
    from app.services import liveness as lv

    calls: list[int] = []

    def down(source, listing):
        calls.append(listing.id)
        return lv.Check(lv.Verdict.UNKNOWN, "network")

    monkeypatch.setattr(lv, "check_listing", down)
    http, Factory, ids = client
    with Factory() as s:
        old = _listing(s, ids, last_seen_at=_now() - timedelta(hours=3), image_urls=["https://cdn.example/1.jpg"])
        seen = _listing(s, ids, last_seen_at=_now() - timedelta(minutes=10))
        assert lv.verify_one(s, old.id, window=timedelta(hours=1)) is False
        row = s.get(Listing, old.id)
        assert row.is_active and row.image_urls == ["https://cdn.example/1.jpg"] and row.verified_at is None
        assert lv.verify_one(s, seen.id, window=timedelta(hours=1)) is False
    assert calls == [old.id]  # виденное обходом 10 мин назад — не спрашиваем


def test_breaker_stops_pass_after_three_unknowns(client, monkeypatch) -> None:
    from app.services import liveness as lv

    calls: list[int] = []

    def down(source, listing):
        calls.append(listing.id)
        return lv.Check(lv.Verdict.UNKNOWN, "network")

    monkeypatch.setattr(lv, "check_listing", down)
    http, Factory, ids = client
    with Factory() as s:
        cars = [_listing(s, ids, last_seen_at=_now() - timedelta(days=2)) for _ in range(6)]
        counts = lv.verify_many(s, [c.id for c in cars], window=timedelta(minutes=5))
    assert len(calls) == 3 and counts["unknown"] == 3 and counts["skipped"] == 3
    with Factory() as s:
        assert all(s.get(Listing, c.id).is_active for c in cars)


def test_open_checks_have_a_global_budget(client, monkeypatch) -> None:
    """Посетитель не может заставить сервер ходить к источнику без меры: не больше
    OPEN_PER_MINUTE проверок по открытию в минуту на процесс."""
    from app.services import liveness as lv

    monkeypatch.setattr(lv, "OPEN_PER_MINUTE", 2)
    calls: list[int] = []
    monkeypatch.setattr(lv, "check_listing", lambda source, listing: calls.append(listing.id) or _alive())
    http, Factory, ids = client
    with Factory() as s:
        cars = [_listing(s, ids, last_seen_at=_now() - timedelta(days=2)) for _ in range(4)]
        for c in cars:
            lv.verify_one(s, c.id, window=timedelta(hours=1))
    assert len(calls) == 2


def test_favorites_are_watched_and_checked_by_background_pass(client, monkeypatch) -> None:
    from app.models import WatchedListing
    from app.services import liveness as lv

    gone_ids: set[int] = set()
    monkeypatch.setattr(
        lv, "check_listing",
        lambda source, listing: lv.Check(lv.Verdict.GONE, "card_gone") if listing.id in gone_ids else _alive(),
    )
    http, Factory, ids = client
    with Factory() as s:
        fav = _listing(s, ids, last_seen_at=_now() - timedelta(days=2))
        other = _listing(s, ids, last_seen_at=_now() - timedelta(days=2))
    gone_ids.add(fav.id)
    http.get("/api/v1/public/listings", params={"ids": str(fav.id)})
    with Factory() as s:
        assert [w.listing_id for w in s.scalars(select(WatchedListing))] == [fav.id]
        out = lv.run_due(s, {}, clock=lambda: 1e6)
        assert out["watched"]["gone"] == 1
        assert s.get(Listing, fav.id).is_active is False and s.get(Listing, other.id).is_active is True
        # Следующий тик раньше срока — проходов нет.
        assert lv.run_due(s, {"best": 1e6, "watched": 1e6}, clock=lambda: 1e6 + 60) == {}
