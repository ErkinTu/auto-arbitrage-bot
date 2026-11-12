"""Фильтры уведомлений с сайта (`/api/v1/me/filters`) = фильтры бота.

Проверяем: роли, общий с ботом лимит и запрет дублей, «несколько марок — без
модели», защиту от одновременной правки (бот ↔ сайт), что бот видит фильтр с
сайта, и что предпросмотр считает по правилам пушей.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import me_filters
from app.bot import db as botdb
from app.core.cache import response_cache
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    BotUser,
    Brand,
    CarModel,
    DailyMarketStat,
    Listing,
    ListingStatusHistory,
    Source,
    UserBlock,
    UserFilter,
)
from app.services import push_filters as pf
from app.services.site_auth import issue_login_token

PHOTO = "https://cdn.example/photo_1.jpg"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def env(monkeypatch):
    from app import models as _models  # noqa: F401

    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _dep():
        with Factory() as session:
            yield session

    app.dependency_overrides[get_session] = _dep
    monkeypatch.setattr(botdb, "SessionLocal", Factory)
    monkeypatch.setattr(get_settings(), "site_url", "https://site.test")
    monkeypatch.setattr(get_settings(), "allowed_brands", "Toyota,Lexus,Kia")
    # Паузы — в своих тестах ниже; здесь проверяем правила фильтров, а не частоту.
    monkeypatch.setattr(get_settings(), "site_filter_write_pause_seconds", 0)
    monkeypatch.setattr(get_settings(), "site_filter_writes_per_hour", 0)
    monkeypatch.setattr(get_settings(), "site_filter_bot_notify_minutes", 0)
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(me_filters, "send_message", lambda chat_id, text, **kw: sent.append((chat_id, text)))
    response_cache.clear()
    with Factory() as s:
        s.add(Source(name="mashina.kg"))
        for name in ("Toyota", "Lexus", "Kia", "BMW"):
            s.add(Brand(name=name))
        s.flush()
        toyota = s.scalar(select(Brand.id).where(Brand.name == "Toyota"))
        lexus = s.scalar(select(Brand.id).where(Brand.name == "Lexus"))
        s.add_all([CarModel(brand_id=toyota, name="Camry"), CarModel(brand_id=lexus, name="RX")])
        s.commit()
    yield TestClient(app, base_url="https://site.test"), Factory, sent
    app.dependency_overrides.clear()
    response_cache.clear()


def _ids(Factory) -> dict[str, int]:
    with Factory() as s:
        out = {b.name: b.id for b in s.scalars(select(Brand))}
        out.update({f"model:{m.name}": m.id for m in s.scalars(select(CarModel))})
        return out


def _login(env, tg: int, *, subscriber: bool = True) -> TestClient:
    client, Factory, _ = env
    with Factory() as s:
        kw = (
            {"access_status": "subscriber", "subscription_expires_at": _now() + timedelta(days=5)}
            if subscriber
            else {"access_status": "revoked"}
        )
        s.add(BotUser(telegram_id=tg, username=f"u{tg}", **kw))
        s.commit()
        token = issue_login_token(s, tg)
    assert client.post("/api/v1/auth/exchange", json={"token": token}).status_code == 200
    return client


# --- доступ ------------------------------------------------------------------------


def test_guest_gets_401_and_user_without_access_reads_only(env) -> None:
    client, _, _ = env
    assert client.get("/api/v1/me/filters").status_code == 401
    _login(env, 10, subscriber=False)
    listing = client.get("/api/v1/me/filters").json()
    assert listing["can_edit"] is False and listing["items"] == [] and listing["limit"] == 5
    assert [b["name"] for b in listing["brands"]] == ["Kia", "Lexus", "Toyota"]  # список бота, без BMW
    assert client.post("/api/v1/me/filters", json={}).status_code == 403
    # Предпросмотр открыт вошедшему без подписки — повод её оформить.
    assert client.post("/api/v1/me/filters/preview", json={}).status_code == 200


def test_foreign_origin_refused(env) -> None:
    client = _login(env, 11)
    response = client.post("/api/v1/me/filters", json={}, headers={"Origin": "https://evil.test"})
    assert response.status_code == 403


# --- CRUD = те же строки, что у бота ---------------------------------------------------


def test_create_multi_brand_is_visible_in_bot_and_bot_is_notified(env) -> None:
    client = _login(env, 12)
    _, Factory, sent = env
    ids = _ids(Factory)
    response = client.post(
        "/api/v1/me/filters",
        json={"brand_ids": [ids["Lexus"], ids["Toyota"]], "price_max_usd": 20000, "below_market_percent": 10,
              "region": "bishkek", "fuel_groups": ["hybrid"]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert [b["name"] for b in body["brands"]] == ["Lexus", "Toyota"]
    assert body["summary"] == "Lexus, Toyota · гибрид · до $20 000 · ниже рынка на 10%+ · регион: Бишкек"
    # Бот видит тот же фильтр (он читает базу) — та же строка.
    (in_bot,) = botdb.list_filters(12)
    assert in_bot.id == body["id"] and set(in_bot.brand_ids) == {ids["Lexus"], ids["Toyota"]}
    with Factory() as s:
        row = s.get(UserFilter, body["id"])
        assert row.brand_id is None and row.car_model_id is None
        assert row.brand_ids == ",".join(map(str, sorted((ids["Lexus"], ids["Toyota"]))))
    assert sent and sent[0][0] == 12 and "добавлен на сайте" in sent[0][1]


def test_single_brand_with_model_and_model_rules(env) -> None:
    client = _login(env, 13)
    ids = _ids(env[1])
    ok = client.post("/api/v1/me/filters", json={"brand_ids": [ids["Toyota"]], "model_id": ids["model:Camry"]})
    assert ok.status_code == 201 and ok.json()["model_name"] == "Camry"
    two = client.post(
        "/api/v1/me/filters", json={"brand_ids": [ids["Toyota"], ids["Lexus"]], "model_id": ids["model:Camry"]}
    )
    assert two.status_code == 422 and two.json()["detail"]["field"] == "model_id"
    alien = client.post("/api/v1/me/filters", json={"brand_ids": [ids["Lexus"]], "model_id": ids["model:Camry"]})
    assert alien.status_code == 422


@pytest.mark.parametrize(
    "payload, field_name",
    [
        ({"year_from": 2015, "max_age_years": 5}, "max_age_years"),
        ({"year_from": 1900}, "year_from"),
        ({"price_max_usd": 0}, "price_max_usd"),
        ({"mileage_max_km": 9_000_000}, "mileage_max_km"),
        ({"below_market_percent": 7}, "below_market_percent"),
        ({"region": "mars"}, "region"),
    ],
)
def test_validation(env, payload, field_name) -> None:
    client = _login(env, 14)
    response = client.post("/api/v1/me/filters", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == field_name


def test_brand_outside_bot_list_and_more_than_ten_refused(env) -> None:
    client = _login(env, 15)
    ids = _ids(env[1])
    bmw = client.post("/api/v1/me/filters", json={"brand_ids": [ids["BMW"]]})
    assert bmw.status_code == 422 and bmw.json()["detail"]["field"] == "brand_ids"
    assert client.post("/api/v1/me/filters", json={"brand_ids": list(range(1, 12))}).status_code == 422


def test_limit_and_duplicate_shared_with_bot(env) -> None:
    client = _login(env, 16)
    for price in (5000, 6000, 7000, 8000):
        assert client.post("/api/v1/me/filters", json={"price_max_usd": price}).status_code == 201
    dup = client.post("/api/v1/me/filters", json={"price_max_usd": 5000})
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "duplicate"
    # Пятый — из бота: слот общий.
    botdb.create_filter(16, **pf.PushFilterSpec(price_max_usd=9000).db_fields())
    full = client.post("/api/v1/me/filters", json={"price_max_usd": 10000})
    assert full.status_code == 409 and full.json()["detail"]["code"] == "limit"
    assert client.get("/api/v1/me/filters").json()["used"] == 5


def test_update_keeps_row_and_refuses_stale_version(env) -> None:
    client = _login(env, 17)
    _, Factory, sent = env
    created = client.post("/api/v1/me/filters", json={"price_max_usd": 5000}).json()
    edited = client.put(
        f"/api/v1/me/filters/{created['id']}",
        json={"price_max_usd": 6000, "updated_at": created["updated_at"]},
    )
    assert edited.status_code == 200 and edited.json()["id"] == created["id"]
    assert edited.json()["price_max_usd"] == 6000
    assert "изменён на сайте" in sent[-1][1]
    # Правка в боте после того, как сайт открыл форму.
    with Factory() as s:
        row = s.get(UserFilter, created["id"])
        row.updated_at = _now() + timedelta(minutes=1)
        s.commit()
    stale = client.put(
        f"/api/v1/me/filters/{created['id']}",
        json={"price_max_usd": 7000, "updated_at": edited.json()["updated_at"]},
    )
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale"


def test_cannot_touch_someone_elses_filter(env) -> None:
    client = _login(env, 18)
    with env[1]() as s:
        s.add(BotUser(telegram_id=99, access_status="revoked"))
        s.commit()
    other = botdb.create_filter(99, **pf.PushFilterSpec(price_max_usd=5000).db_fields())
    assert client.put(f"/api/v1/me/filters/{other.id}", json={"price_max_usd": 1}).status_code == 404
    assert client.delete(f"/api/v1/me/filters/{other.id}").status_code == 404
    assert botdb.get_filter(99, other.id) is not None


def test_delete_allowed_without_access(env) -> None:
    client = _login(env, 19, subscriber=False)
    created = botdb.create_filter(19, **pf.PushFilterSpec(price_max_usd=5000).db_fields())
    assert client.delete(f"/api/v1/me/filters/{created.id}").status_code == 204
    assert botdb.list_filters(19) == []


def test_legacy_filter_is_flagged(env) -> None:
    client = _login(env, 20)
    botdb.create_filter(
        20, brand_id=None, car_model_id=None, year_from=None, year_to=None, mileage_max_km=None,
        price_max_usd=None, below_market_only=True, below_market_percent=None, skip_foreign=True,
    )
    (item,) = client.get("/api/v1/me/filters").json()["items"]
    assert item["legacy"] is True and item["below_market_percent"] is None


# --- частота изменений ---------------------------------------------------------------------


def test_short_pause_between_changes(env, monkeypatch) -> None:
    client = _login(env, 40)
    monkeypatch.setattr(get_settings(), "site_filter_write_pause_seconds", 5)
    assert client.post("/api/v1/me/filters", json={"price_max_usd": 5000}).status_code == 201
    second = client.post("/api/v1/me/filters", json={"price_max_usd": 6000})
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "too_often"
    assert "подождите" in second.json()["detail"]["message"]
    assert int(second.headers["Retry-After"]) >= 1
    # Удаление — тоже изменение.
    (item,) = client.get("/api/v1/me/filters").json()["items"]
    assert client.delete(f"/api/v1/me/filters/{item['id']}").status_code == 429
    # Пауза — на человека: другой вошедший не ждёт.
    other = TestClient(app, base_url="https://site.test")
    with env[1]() as s:
        s.add(BotUser(telegram_id=41, access_status="subscriber", subscription_expires_at=_now() + timedelta(days=1)))
        s.commit()
        token = issue_login_token(s, 41)
    other.post("/api/v1/auth/exchange", json={"token": token})
    assert other.post("/api/v1/me/filters", json={"price_max_usd": 5000}).status_code == 201


def test_hourly_cap(env, monkeypatch) -> None:
    client = _login(env, 42)
    monkeypatch.setattr(get_settings(), "site_filter_writes_per_hour", 3)
    created = client.post("/api/v1/me/filters", json={"price_max_usd": 5000}).json()
    for price in (6000, 7000):
        body = {"price_max_usd": price}
        assert client.put(f"/api/v1/me/filters/{created['id']}", json=body).status_code == 200
    capped = client.put(f"/api/v1/me/filters/{created['id']}", json={"price_max_usd": 8000})
    assert capped.status_code == 429 and "мин" in capped.json()["detail"]["message"]


def test_bot_message_not_more_often_than_interval(env, monkeypatch) -> None:
    client = _login(env, 43)
    _, _, sent = env
    monkeypatch.setattr(get_settings(), "site_filter_bot_notify_minutes", 10)
    created = client.post("/api/v1/me/filters", json={"price_max_usd": 5000}).json()
    client.put(f"/api/v1/me/filters/{created['id']}", json={"price_max_usd": 6000})
    client.post("/api/v1/me/filters", json={"price_max_usd": 7000})
    assert [chat for chat, _ in sent] == [43]  # три изменения — одно сообщение


def test_user_without_access_cannot_change_filters_like_in_bot(env) -> None:
    client = _login(env, 44, subscriber=False)
    created = botdb.create_filter(44, **pf.PushFilterSpec(price_max_usd=5000).db_fields())
    assert client.put(f"/api/v1/me/filters/{created.id}", json={"price_max_usd": 1}).status_code == 403
    assert client.post("/api/v1/me/filters", json={}).status_code == 403


# --- предпросмотр -------------------------------------------------------------------------


def _listing(Factory, ext: str, *, price: int, observed: datetime, event: str = "new", brand: str = "Toyota",
             photo: str | None = PHOTO, published: datetime | None = None, active: bool = True,
             year: int = 2019, city: str = "Бишкек") -> int:
    ids = _ids(Factory)
    with Factory() as s:
        listing = Listing(
            source_id=s.scalar(select(Source.id)), external_id=ext,
            brand_id=ids[brand], car_model_id=ids["model:Camry"] if brand == "Toyota" else ids["model:RX"],
            title="t", source_slug=f"s-{ext}", year=year, mileage_km=90_000, city=city,
            current_price_usd=Decimal(price), is_active=active, image_url=photo,
            source_created_at=published or observed, payload_hash="x" * 8,
        )
        s.add(listing)
        s.flush()
        s.add(ListingStatusHistory(listing_id=listing.id, event_type=event, old_status=None,
                                   new_status="active", observed_at=observed))
        s.commit()
        return listing.id


def _stat(Factory, *, avg: int, brand: str = "Toyota") -> None:
    ids = _ids(Factory)
    with Factory() as s:
        s.add(DailyMarketStat(
            stat_date=date.today(), brand_id=ids[brand],
            car_model_id=ids["model:Camry"] if brand == "Toyota" else ids["model:RX"],
            generation_id=None, year=2019, mileage_bucket="50-100k", customs_status="cleared",
            avg_price_usd=Decimal(avg), median_price_usd=Decimal(avg), min_price_usd=Decimal(avg),
            max_price_usd=Decimal(avg), sample_count=20,
        ))
        s.commit()


def test_preview_counts_like_pushes(env) -> None:
    _, Factory, _ = env
    now = _now()
    _stat(Factory, avg=10_000)
    _stat(Factory, avg=10_000, brand="Lexus")
    good = _listing(Factory, "good", price=8_000, observed=now - timedelta(hours=1))  # −20 %
    _listing(Factory, "meh", price=9_700, observed=now - timedelta(days=1))  # −3 %
    _listing(Factory, "nophoto", price=8_000, observed=now - timedelta(hours=2), photo=None)
    _listing(Factory, "vip", price=8_000, observed=now - timedelta(hours=3), published=now - timedelta(days=20))
    _listing(Factory, "old", price=8_000, observed=now - timedelta(days=9))
    _listing(Factory, "junk", price=2_000, observed=now - timedelta(hours=4))  # −80 % — мусор цены
    drop = _listing(Factory, "drop", price=8_500, observed=now - timedelta(hours=5), event="price_changed",
                    published=now - timedelta(days=30))
    sold = _listing(Factory, "sold", price=8_000, observed=now - timedelta(days=2), active=False)
    _listing(Factory, "lexus", price=8_000, observed=now - timedelta(hours=6), brand="Lexus")
    ids = _ids(Factory)

    with Factory() as s:
        spec = pf.validate_spec(
            s, brand_ids=[ids["Toyota"]], model_id=None, year_from=None, year_to=None, max_age_years=None,
            mileage_max_km=None, price_max_usd=None, below_market_percent=10, region=None, fuel_groups=[],
        )
        result = pf.preview(s, spec, now=now)
    # good, drop (снижение цены), sold (уведомление тогда ушло); без «meh» (−3 %),
    # без фото, VIP-поднятого, старше 7 дней, мусорной цены и чужой марки.
    assert result["total"] == 3
    assert sum(d["count"] for d in result["by_day"]) == 3 and len(result["by_day"]) == 7
    # Примеры — только продающиеся, самые выгодные первыми.
    assert [item["id"] for item in result["samples"]] == [good, drop]
    assert sold not in [item["id"] for item in result["samples"]]


def test_preview_without_below_market_and_blocklist(env) -> None:
    client = _login(env, 30)
    _, Factory, _ = env
    now = _now()
    _listing(Factory, "a", price=9_900, observed=now - timedelta(hours=1))
    _listing(Factory, "b", price=9_900, observed=now - timedelta(hours=2), brand="Lexus")
    assert client.post("/api/v1/me/filters/preview", json={}).json()["total"] == 2
    with Factory() as s:
        user = s.scalar(select(BotUser).where(BotUser.telegram_id == 30))
        s.add(UserBlock(user_id=user.id, kind="brand", value=str(_ids(Factory)["Lexus"]), label="Lexus"))
        s.commit()
    response_cache.clear()
    result = client.post("/api/v1/me/filters/preview", json={}).json()
    assert result["total"] == 1 and result["without_market"] == 1
