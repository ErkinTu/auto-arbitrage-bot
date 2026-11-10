"""Графики раздела «Аналитика» сайта: поступления, распределение, срок до снятия."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Brand, CarModel, Listing, Source
from app.services import market_overview as mo


def _session() -> tuple[Session, dict[str, int]]:
    import app.models  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all([Source(name="mashina.kg"), Source(name="lalafo.kg")])
    ids = {}
    for name in ("Toyota", "BMW"):
        brand = Brand(name=name)
        session.add(brand)
        session.flush()
        session.add(CarModel(brand_id=brand.id, name=f"{name}-m"))
        ids[name] = brand.id
    session.flush()
    return session, ids


_n = 0


def _listing(session, brand_id, *, source_id=1, seen, active=True, gone=None, last=None, price=20_000):
    global _n
    _n += 1
    session.add(
        Listing(
            source_id=source_id, external_id=f"x{_n}", brand_id=brand_id, car_model_id=brand_id,
            title="t", year=2018, current_price_usd=Decimal(price), is_active=active,
            analytics_eligible=True, first_seen_at=seen, last_seen_at=last or seen, deactivated_at=gone,
            payload_hash=f"h{_n}",
        )
    )


def test_intake_by_day_splits_sources_and_fills_gaps() -> None:
    session, ids = _session()
    now = datetime.now(timezone.utc)
    _listing(session, ids["Toyota"], source_id=1, seen=now - timedelta(days=3))
    _listing(session, ids["Toyota"], source_id=2, seen=now - timedelta(days=3))
    _listing(session, ids["BMW"], source_id=2, seen=now, active=False)  # снятое — тоже приток
    session.flush()
    points = mo.intake_by_day(session)
    assert len(points) == 4  # 3 дня назад … сегодня, пустые дни — нули
    assert (points[0]["mashina"], points[0]["lalafo"], points[0]["total"]) == (1, 1, 2)
    assert [p["total"] for p in points[1:3]] == [0, 0]
    assert points[-1]["lalafo"] == 1


def test_liquidity_needs_enough_samples_and_uses_median(monkeypatch) -> None:
    session, ids = _session()
    monkeypatch.setattr(mo, "LIQUIDITY_MIN_SAMPLES", 3)
    now = datetime.now(timezone.utc)
    for days in (2, 4, 40):  # выброс 40 дней не тянет медиану
        seen = now - timedelta(days=days + 10)
        # Срок — до последнего раза, когда видели живым, а не до снятия (+ задержка обнаружения).
        _listing(session, ids["Toyota"], seen=seen, last=seen + timedelta(days=days), active=False, gone=now - timedelta(days=1))
    _listing(session, ids["BMW"], seen=now - timedelta(days=5), active=False, gone=now)  # мало данных
    # lalafo не считаем: видим их почти только в момент публикации.
    for _ in range(3):
        _listing(session, ids["BMW"], source_id=2, seen=now - timedelta(days=9), active=False, gone=now)
    session.flush()
    [toyota] = mo.liquidity_by_brand(session)
    assert toyota["brand"] == "Toyota" and toyota["removed"] == 3 and toyota["median_days"] == 4.0


def test_price_distribution_for_market_and_brand() -> None:
    session, ids = _session()
    now = datetime.now(timezone.utc)
    for price in range(10_000, 30_000, 1_000):
        _listing(session, ids["Toyota"], seen=now, price=price)
    for price in (60_000, 61_000, 62_000, 63_000, 64_000):
        _listing(session, ids["BMW"], seen=now, price=price)
    session.flush()
    market = mo.price_distribution(session)
    assert market["total"] == 25 and market["bins"]
    bmw = mo.price_distribution(session, brand_id=ids["BMW"])
    assert bmw["total"] == 5 and bmw["median_usd"] == 62_000.0
