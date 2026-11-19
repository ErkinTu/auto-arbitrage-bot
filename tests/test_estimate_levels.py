"""Уровни точности оценки (решение владельца 19.11.2026): 1 — как сейчас всем,
2 — год ±1 подписчикам, 3 — + снятые, только админам. Все уровни считаются
всегда, независимо от того, хватает ли данных уровню 1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.cache import response_cache
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import BotUser, Brand, CarModel, Listing, ListingSameCar, Source
from app.services.estimate_levels import estimate_levels, year_step
from app.services.site_auth import issue_login_token
from app.services.vehicle_summary import get_vehicle_summary


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def env(monkeypatch):
    from app import models as _models  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Factory() as s:
        s.add(Source(name="mashina.kg"))
        brand = Brand(name="Toyota")
        s.add(brand)
        s.flush()
        model = CarModel(brand_id=brand.id, name="Camry")
        s.add(model)
        s.commit()
        ids = {"brand": brand.id, "model": model.id}

    def _dep():
        with Factory() as session:
            yield session

    app.dependency_overrides[get_session] = _dep
    monkeypatch.setattr(get_settings(), "site_url", "https://site.test")
    response_cache.clear()
    yield TestClient(app, base_url="https://site.test"), Factory, ids
    app.dependency_overrides.clear()
    response_cache.clear()


_n = iter(range(1, 100_000))


def _car(s, ids, *, year: int, price: int, active: bool = True, mileage: int | None = 100_000, **kw) -> Listing:
    n = next(_n)
    item = Listing(
        source_id=1,
        external_id=f"e{n}",
        brand_id=ids["brand"],
        car_model_id=ids["model"],
        title="Toyota Camry",
        year=year,
        mileage_km=mileage,
        current_price_usd=Decimal(price),
        is_active=active,
        analytics_eligible=True,
        payload_hash=f"h{n}",
        **{"deactivated_at": None if active else _now() - timedelta(days=10), **kw},
    )
    s.add(item)
    s.commit()
    return item


def _fill(s, ids) -> None:
    # 2019: 3 по 20 000; 2018: 3 по 18 000; 2020: 3 по 22 000 — шаг года ~10 %.
    for year, price in ((2018, 18_000), (2019, 20_000), (2020, 22_000)):
        for _ in range(3):
            _car(s, ids, year=year, price=price)


def test_level_one_equals_current_estimate(env) -> None:
    _, Factory, ids = env
    with Factory() as s:
        _fill(s, ids)
        summary = get_vehicle_summary(s, brand_id=ids["brand"], model_id=ids["model"], year=2019, mileage_km=90_000)
        levels = estimate_levels(s, brand_id=ids["brand"], model_id=ids["model"], year=2019, mileage_km=90_000)
    one = levels[0]
    assert one["level"] == 1 and one["samples"] == summary["samples"] == 3
    assert one["estimated_price"] == summary["estimated_price"]
    assert one["estimated_range"] == summary["estimated_range"]
    assert one["years"] == [{"year": 2019, "count": 3}]


def test_level_two_adjusts_neighbour_years_and_level_three_adds_archive(env) -> None:
    _, Factory, ids = env
    with Factory() as s:
        _fill(s, ids)
        gone = _car(s, ids, year=2019, price=19_000, active=False)
        _car(s, ids, year=2019, price=5_000, active=False, deactivated_at=_now() - timedelta(days=200))  # старое
        _car(s, ids, year=2019, price=50_000, active=False, price_suspect=True)  # выброс
        # Снятый повтор машины, которая ещё в продаже, — не берём второй раз.
        twin = _car(s, ids, year=2019, price=19_500, active=False)
        live = s.query(Listing).filter_by(year=2019, is_active=True).first()
        s.add(ListingSameCar(listing_id=twin.id, other_id=live.id, distance=0, computed_at=_now()))
        s.commit()
        levels = estimate_levels(s, brand_id=ids["brand"], model_id=ids["model"], year=2019)
    two, three = levels[1], levels[2]
    assert two["samples"] == 9 and two["year_step_source"] == "data"
    assert 9 <= two["year_step_percent"] <= 11
    # Соседние годы приведены к 2019: все ~20 000.
    assert 19_500 <= two["estimated_range"]["min"] and two["estimated_range"]["max"] <= 20_500
    assert three["samples"] == 10 and three["archive_count"] == 1
    assert gone.id  # единственный годный архив


def test_levels_always_computed_even_without_exact_year(env) -> None:
    """Уровня 1 нет (точного года нет в продаже) — уровни 2 и 3 всё равно считаются."""
    _, Factory, ids = env
    with Factory() as s:
        _car(s, ids, year=2018, price=18_000)
        _car(s, ids, year=2020, price=22_000)
        levels = estimate_levels(s, brand_id=ids["brand"], model_id=ids["model"], year=2019)
    assert levels[0]["available"] is False and levels[0]["samples"] == 0
    assert levels[1]["available"] is True and levels[1]["samples"] == 2
    assert levels[2]["available"] is True


def test_year_step_default_when_one_year() -> None:
    item = Listing(year=2019, current_price_usd=Decimal(20_000))
    assert year_step([item], 2019) == (0.07, "default")


def _login(client: TestClient, Factory, tg: int, **kw) -> None:
    with Factory() as s:
        s.add(BotUser(telegram_id=tg, username=f"u{tg}", access_status="revoked", **kw))
        s.commit()
        token = issue_login_token(s, tg)
    assert client.post("/api/v1/auth/exchange", json={"token": token}).status_code == 200


def test_roles_get_their_levels_only(env) -> None:
    client, Factory, ids = env
    with Factory() as s:
        _fill(s, ids)
    url = f"/api/v1/me/estimate-levels?brand_id={ids['brand']}&model_id={ids['model']}&year=2019"
    assert client.get(url).status_code == 401  # гость
    _login(client, Factory, 11)
    assert client.get(url).status_code == 403  # без подписки
    client.cookies.clear()
    _login(client, Factory, 12, subscription_expires_at=_now() + timedelta(days=3))
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert [lv["level"] for lv in response.json()["levels"]] == [1, 2]
    client.cookies.clear()
    _login(client, Factory, 13, is_superuser=True)
    assert [lv["level"] for lv in client.get(url).json()["levels"]] == [1, 2, 3]
