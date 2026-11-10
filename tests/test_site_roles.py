"""Роли сайта: гость / пользователь / подписчик / админ.

Проверяем границы, а не фичу: что гостю закрыто, что подписка открывает сразу
и закрывает сразу по истечении, что ссылка входа одноразовая, и что ответ,
зависящий от роли, не уезжает в общий кэш.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import BotUser, Brand, CarModel, Listing, ListingPriceHistory, Source
from app.services.site_auth import access_of, issue_login_token, role_of


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def env(monkeypatch):
    from app import models as _models  # noqa: F401 — регистрирует таблицы

    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _dep():
        with Factory() as session:
            yield session

    app.dependency_overrides[get_session] = _dep
    monkeypatch.setattr(get_settings(), "site_url", "https://site.test")
    # https: кука Secure, по http клиент её обратно не пошлёт.
    yield TestClient(app, base_url="https://site.test"), Factory
    app.dependency_overrides.clear()


def _user(Factory, tg: int, **kw) -> BotUser:
    with Factory() as s:
        user = BotUser(telegram_id=tg, username=f"u{tg}", access_status="revoked", **kw)
        s.add(user)
        s.commit()
        return user


def _login(client: TestClient, Factory, tg: int) -> dict:
    with Factory() as s:
        token = issue_login_token(s, tg)
    response = client.post("/api/v1/auth/exchange", json={"token": token})
    assert response.status_code == 200, response.text
    return response.json()


# --- правила роли ------------------------------------------------------------


def test_role_rules() -> None:
    now = _now()
    assert role_of(None) == "guest"
    assert role_of(BotUser(access_status="revoked")) == "user"
    paid = BotUser(access_status="subscriber", subscription_expires_at=now + timedelta(days=3))
    assert role_of(paid) == "subscriber"
    expired = BotUser(access_status="subscriber", subscription_expires_at=now - timedelta(minutes=1))
    assert role_of(expired) == "user"  # истекла — сразу, без перелогина
    trial = BotUser(access_status="trial", trial_expires_at=now + timedelta(hours=5))
    assert role_of(trial) == "subscriber"
    member = BotUser(access_status="member", last_access_check_at=now)
    assert role_of(member) == "subscriber"
    assert role_of(BotUser(access_status="revoked", is_superuser=True)) == "admin"


def test_unverified_member_default_is_not_subscriber() -> None:
    """'member' — дефолт колонки; без проверки ботом доступа не даёт."""
    assert role_of(BotUser(access_status="member", last_access_check_at=None)) == "user"
    assert access_of(BotUser(access_status="member")).active is False


# --- вход ----------------------------------------------------------------------


def test_guest_me(env) -> None:
    client, _ = env
    assert client.get("/api/v1/auth/me").json() == {
        "role": "guest", "user": None, "access": None, "session_expires_at": None,
    }


def test_login_link_is_one_time_and_sets_lax_httponly_cookie(env) -> None:
    client, Factory = env
    _user(Factory, 100)
    with Factory() as s:
        token = issue_login_token(s, 100)
    first = client.post("/api/v1/auth/exchange", json={"token": token})
    assert first.status_code == 200 and first.json()["role"] == "user"
    cookie = first.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert client.post("/api/v1/auth/exchange", json={"token": token}).status_code == 401
    assert client.get("/api/v1/auth/me").json()["user"]["telegram_id"] == 100


def test_new_link_cancels_previous(env) -> None:
    client, Factory = env
    _user(Factory, 101)
    with Factory() as s:
        old = issue_login_token(s, 101)
        issue_login_token(s, 101)
    assert client.post("/api/v1/auth/exchange", json={"token": old}).status_code == 401


def test_no_link_when_site_disabled_or_unknown_user(env, monkeypatch) -> None:
    _, Factory = env
    with Factory() as s:
        assert issue_login_token(s, 999) is None  # нет такого юзера
    _user(Factory, 102)
    monkeypatch.setattr(get_settings(), "site_url", "")
    with Factory() as s:
        assert issue_login_token(s, 102) is None


def test_logout(env) -> None:
    client, Factory = env
    _user(Factory, 103)
    _login(client, Factory, 103)
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/auth/me").json()["role"] == "guest"


# --- гейты ---------------------------------------------------------------------


def test_analytics_gates(env) -> None:
    client, Factory = env
    url = "/api/v1/dashboard/brand-stats"
    assert client.get(url).status_code == 401  # гость
    _user(Factory, 200)
    _login(client, Factory, 200)
    assert client.get(url).status_code == 403  # вошёл, подписки нет
    for path in ("/api/v1/market/vehicle-summary?brand_id=1&model_id=1&year=2019",
                 "/api/v1/estimate?brand=a&model=b&year=2019&mileage_km=1",
                 "/api/v1/price-trend?brand=a&model=b&year=2019&mileage_km=1"):
        assert client.get(path).status_code == 403, path


def test_subscriber_gets_analytics(env) -> None:
    client, Factory = env
    _user(Factory, 201, subscription_expires_at=_now() + timedelta(days=5))
    me = _login(client, Factory, 201)
    assert me["role"] == "subscriber" and me["access"]["status"] == "subscriber"
    # Гейт пройден: данных нет — честный 404, а не 401/403.
    assert client.get("/api/v1/market/vehicle-summary?brand_id=1&model_id=1&year=2019").status_code == 404


def test_estimate_open_to_guests_without_market_details(env) -> None:
    """«Оценить авто» открыта гостям (10.11.2026); аналитика рынка — нет."""
    client, _ = env
    # Гейта нет: данных нет — 404, а не 401.
    assert client.get("/api/v1/public/estimate?brand_id=1&model_id=1&year=2019").status_code == 404
    assert client.get("/api/v1/market/vehicle-summary?brand_id=1&model_id=1&year=2019").status_code == 401


def test_feed_stays_open_to_guests(env) -> None:
    client, _ = env
    assert client.get("/api/v1/public/feed").status_code == 200
    # Счётчик «Показать N» панели фильтров: упал незаметно (вклеенные строки в
    # вызове cached_call, ветка feed-regions) — держим ручки ленты под тестом.
    for path in ("/api/v1/public/feed/count", "/api/v1/public/feed/count?brand_ids=1,2&price_from=1000&price_to=20000",
                 "/api/v1/public/feed/facets"):
        assert client.get(path).status_code == 200, path
    assert client.get("/api/v1/catalog/brands").status_code == 200


def _listing_with_history(Factory) -> int:
    with Factory() as s:
        src, brand = Source(name="mashina.kg"), Brand(name="Toyota")
        s.add_all([src, brand])
        s.flush()
        model = CarModel(brand_id=brand.id, name="Camry")
        s.add(model)
        s.flush()
        listing = Listing(
            source_id=src.id, external_id="1", brand_id=brand.id, car_model_id=model.id, year=2019,
            current_price_usd=Decimal("20000"), is_active=True, payload_hash="x", source_slug="s",
            first_seen_at=_now(),
        )
        s.add(listing)
        s.flush()
        s.add(ListingPriceHistory(listing_id=listing.id, event_type="new", price_usd=Decimal("21000"), observed_at=_now()))
        s.commit()
        return listing.id


def test_price_history_only_for_subscribers_and_not_shared_cache(env) -> None:
    client, Factory = env
    listing_id = _listing_with_history(Factory)
    url = f"/api/v1/public/listings/{listing_id}"
    guest = client.get(url)
    assert guest.json()["price_history_locked"] is True and guest.json()["price_history"] == []
    # Ответ зависит от роли — только приватный кэш и Vary по куке.
    assert guest.headers["cache-control"].startswith("private")
    assert guest.headers["vary"] == "Cookie"

    _user(Factory, 202, trial_expires_at=_now() + timedelta(hours=3))
    _login(client, Factory, 202)
    sub = client.get(url).json()
    assert sub["price_history_locked"] is False and sub["price_history"][0]["price_usd"] == 21000


def test_admin_via_site_session_sees_admin_api(env, monkeypatch) -> None:
    client, Factory = env
    _user(Factory, 300)
    _login(client, Factory, 300)
    assert client.get("/api/v1/admin/users").status_code == 401  # обычный — нет
    client.cookies.clear()
    _user(Factory, 301, is_superuser=True)
    me = _login(client, Factory, 301)
    assert me["role"] == "admin"
    assert client.get("/api/v1/admin/users").status_code == 200
    assert client.get("/api/v1/market/vehicle-summary?brand_id=1&model_id=1&year=2019").status_code == 404  # админ ≥ подписчик: гейт пройден


def test_dev_login_refuses_non_local_site(monkeypatch) -> None:
    """Тестовые юзеры (в т.ч. админ) не должны появиться на проде случайно."""
    import argparse

    from app.cli import command_dev_login

    monkeypatch.setattr(get_settings(), "site_url", "https://skupka.63-182-28-250.sslip.io")
    with pytest.raises(SystemExit, match="не localhost"):
        command_dev_login(argparse.Namespace(role=None, i_know_this_is_not_prod=False))


def test_public_estimate_strips_market_analytics(env, monkeypatch) -> None:
    """Гостю — оценка и графики, но не список аномалий со ссылками."""
    import app.api.public.estimate as public_estimate

    client, _ = env
    fake = {
        "vehicle": {"brand": "Toyota", "model": "Camry", "generation": None, "year": 2018},
        "currency": "USD", "estimated_price": 18_000.0, "estimated_range": {"min": 17_000.0, "max": 19_000.0},
        "market_average": 18_100.0, "market_median": 18_000.0, "samples": 30, "confidence": "high",
        "mileage": {"requested_km": None, "window": "all", "samples_in_window": 30, "adjustment_usd": 0.0},
        "representative_listing": {"id": 1, "title": "x", "image_url": None, "source_url": "https://x"},
        "anomalies": {"count": 1, "low_count": 1, "high_count": 0, "items": [{
            "id": 2, "title": "y", "year": 2018, "mileage_km": None, "city": None,
            "price_usd": 5000.0, "deviation_percent": -70.0, "source_url": "https://y"}]},
        "price_distribution": {"step": 1000.0, "bins": [], "below": 0, "above": 0, "total": 30},
        "granularity": "week", "trend": [], "intake": [],
    }
    monkeypatch.setattr(public_estimate, "get_vehicle_summary", lambda *a, **k: fake)
    body = client.get("/api/v1/public/estimate?brand_id=7&model_id=7&year=2018").json()
    assert body["estimated_price"] == 18_000.0
    assert body["anomalies"]["items"] == [] and body["representative_listing"] is None
    # Общий объект кэша не испорчен.
    assert fake["anomalies"]["count"] == 1
