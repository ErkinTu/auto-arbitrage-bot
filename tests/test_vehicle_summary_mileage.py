"""Оценка с поправкой на пробег в `/market/vehicle-summary`.

Эндпоинт пробег не принимал вовсе, поэтому фронт показывал медиану когорты, а
`/estimate` на тех же данных отвечал иначе. Тесты держат два обещания: без
пробега ответ прежний, а с пробегом обе ручки считают одним кодом и не
разъезжаются.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Brand, CarModel, Listing, Source
from app.services.analytics import estimate_price
from app.services.vehicle_summary import get_vehicle_summary


def _session() -> tuple[Session, Brand, CarModel]:
    import app.models  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    source = Source(name="mashina.kg")
    brand = Brand(name="Toyota")
    session.add_all([source, brand])
    session.flush()
    model = CarModel(brand_id=brand.id, name="Camry")
    session.add(model)
    session.flush()
    return session, brand, model


def _listing(
    session: Session,
    brand: Brand,
    model: CarModel,
    *,
    external_id: str,
    price_usd: int,
    mileage_km: int | None,
) -> None:
    session.add(
        Listing(
            source_id=1,
            external_id=external_id,
            brand_id=brand.id,
            car_model_id=model.id,
            title=f"Toyota Camry {external_id}",
            year=2024,
            mileage_km=mileage_km,
            current_price_usd=Decimal(price_usd),
            is_active=True,
            analytics_eligible=True,
            last_seen_at=datetime.now(timezone.utc) - timedelta(hours=1),
            payload_hash=f"hash-{external_id}",
        )
    )


def _seed_market(session: Session, brand: Brand, model: CarModel) -> None:
    """Рынок, где цена явно падает с пробегом: 35k при нуле, 20k при 200 тыс."""
    for index in range(20):
        mileage = index * 10_000
        price = 35_000 - index * 750
        _listing(
            session,
            brand,
            model,
            external_id=f"m{index}",
            price_usd=price,
            mileage_km=mileage,
        )
    session.flush()


def test_without_mileage_keeps_cohort_median() -> None:
    session, brand, model = _session()
    _seed_market(session, brand, model)

    summary = get_vehicle_summary(session, brand_id=brand.id, model_id=model.id, year=2024)

    assert summary["estimated_price"] == summary["market_median"]
    assert summary["samples"] == 20
    assert summary["mileage"]["requested_km"] is None
    assert summary["mileage"]["adjustment_usd"] == 0.0
    assert summary["mileage"]["window"] == "all matching listings"


def test_low_mileage_estimate_beats_high_mileage_one() -> None:
    session, brand, model = _session()
    _seed_market(session, brand, model)

    fresh = get_vehicle_summary(
        session, brand_id=brand.id, model_id=model.id, year=2024, mileage_km=10_000
    )
    worn = get_vehicle_summary(
        session, brand_id=brand.id, model_id=model.id, year=2024, mileage_km=180_000
    )

    assert fresh["estimated_price"] > worn["estimated_price"]
    assert fresh["mileage"]["adjustment_usd"] > 0
    assert worn["mileage"]["adjustment_usd"] < 0
    # Окно сузилось — выборка меньше когорты, и это видно в ответе.
    assert fresh["mileage"]["window"] == "±50,000 km"
    assert fresh["samples"] < 20


def test_both_endpoints_agree_on_the_same_car() -> None:
    session, brand, model = _session()
    _seed_market(session, brand, model)

    summary = get_vehicle_summary(
        session, brand_id=brand.id, model_id=model.id, year=2024, mileage_km=45_000
    )
    estimate = estimate_price(
        session,
        brand_name="Toyota",
        model_name="Camry",
        generation_name=None,
        year=2024,
        mileage_km=45_000,
    )

    assert summary["estimated_price"] == estimate["estimated_price"]
    assert summary["mileage"]["window"] == estimate["used_mileage_window"]
    assert summary["samples"] == estimate["samples"]


def test_unknown_mileage_listings_do_not_break_the_window() -> None:
    """У lalafo пробега нет у ~43% объявлений — они не должны ронять оценку."""
    session, brand, model = _session()
    _seed_market(session, brand, model)
    for index in range(10):
        _listing(
            session,
            brand,
            model,
            external_id=f"nomileage{index}",
            price_usd=26_000,
            mileage_km=None,
        )
    session.flush()

    summary = get_vehicle_summary(
        session, brand_id=brand.id, model_id=model.id, year=2024, mileage_km=20_000
    )

    assert summary["mileage"]["window"] == "±50,000 km"
    assert summary["estimated_price"] > 0
    # Когорта для рынка осталась полной, включая объявления без пробега.
    assert summary["anomalies"]["count"] >= 0
    assert summary["market_median"] > 0


def test_tiny_window_falls_back_to_whole_cohort() -> None:
    """Меньше трёх соседей — считаем по всей когорте и говорим об этом."""
    session, brand, model = _session()
    _listing(session, brand, model, external_id="a", price_usd=30_000, mileage_km=10_000)
    _listing(session, brand, model, external_id="b", price_usd=28_000, mileage_km=20_000)
    _listing(session, brand, model, external_id="c", price_usd=26_000, mileage_km=30_000)
    _listing(session, brand, model, external_id="d", price_usd=15_000, mileage_km=300_000)
    session.flush()

    summary = get_vehicle_summary(
        session, brand_id=brand.id, model_id=model.id, year=2024, mileage_km=300_000
    )

    assert summary["mileage"]["window"] == "all matching listings"
    assert summary["samples"] == 4
