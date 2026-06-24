from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Brand, CarModel, DailyMarketStat, Generation, Listing
from app.services.stats import mileage_bucket


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    position = (len(values) - 1) * p
    low = int(position)
    high = min(low + 1, len(values) - 1)
    weight = position - low
    return values[low] * (1 - weight) + values[high] * weight


def _linear_estimate(samples: list[Listing], requested_mileage: int, fallback: float) -> float:
    points = [(item.mileage_km, float(item.current_price_usd)) for item in samples if item.mileage_km is not None]
    if len(points) < 5:
        return fallback
    x_avg = mean(x for x, _ in points)
    y_avg = mean(y for _, y in points)
    denominator = sum((x - x_avg) ** 2 for x, _ in points)
    if denominator == 0:
        return fallback
    slope = sum((x - x_avg) * (y - y_avg) for x, y in points) / denominator
    # Avoid an implausible positive slope caused by very small/noisy samples.
    slope = min(slope, 0.0)
    intercept = y_avg - slope * x_avg
    estimate = intercept + slope * requested_mileage
    # Keep an early MVP estimate inside a robust market range.
    prices = [y for _, y in points]
    return max(_percentile(prices, 0.1), min(estimate, _percentile(prices, 0.9)))


def resolve_vehicle_ids(
    session: Session, brand_name: str, model_name: str, generation_name: str | None
) -> tuple[Brand, CarModel, Generation | None]:
    brand = session.scalar(select(Brand).where(Brand.name.ilike(brand_name)))
    if not brand:
        raise LookupError("Марка не найдена в собранной базе")
    car_model = session.scalar(
        select(CarModel).where(CarModel.brand_id == brand.id, CarModel.name.ilike(model_name))
    )
    if not car_model:
        raise LookupError("Модель не найдена в собранной базе")
    generation = None
    if generation_name:
        generation = session.scalar(
            select(Generation).where(
                Generation.car_model_id == car_model.id,
                Generation.name.ilike(generation_name),
            )
        )
        if not generation:
            raise LookupError("Поколение не найдено в собранной базе")
    return brand, car_model, generation


def estimate_price(
    session: Session,
    *,
    brand_name: str,
    model_name: str,
    generation_name: str | None,
    year: int,
    mileage_km: int,
    customs_status: str | None = None,
) -> dict:
    brand, car_model, generation = resolve_vehicle_ids(session, brand_name, model_name, generation_name)
    conditions = [
        Listing.is_active.is_(True),
        Listing.analytics_eligible.is_(True),
        Listing.brand_id == brand.id,
        Listing.car_model_id == car_model.id,
        Listing.year == year,
        Listing.current_price_usd.is_not(None),
    ]
    if generation:
        conditions.append(Listing.generation_id == generation.id)
    if customs_status:
        conditions.append(Listing.customs_status == customs_status)

    all_samples = session.scalars(select(Listing).where(*conditions).limit(1_000)).all()
    if not all_samples:
        raise LookupError("Недостаточно похожих активных объявлений")

    nearby = [item for item in all_samples if item.mileage_km is not None and abs(item.mileage_km - mileage_km) <= 50_000]
    selected = nearby if len(nearby) >= 3 else all_samples
    prices = [float(item.current_price_usd) for item in selected if item.current_price_usd is not None]
    base_median = float(median(prices))
    estimated = _linear_estimate(all_samples, mileage_km, base_median)
    q1, q3 = _percentile(prices, 0.25), _percentile(prices, 0.75)
    sample_count = len(selected)
    confidence = "high" if sample_count >= 20 else "medium" if sample_count >= 8 else "low"

    return {
        "currency": "USD",
        "estimated_price": round(estimated, 2),
        "estimated_range": {"min": round(q1, 2), "max": round(q3, 2)},
        "market_average": round(mean(prices), 2),
        "market_median": round(base_median, 2),
        "samples": sample_count,
        "confidence": confidence,
        "used_mileage_window": "±50,000 km" if selected is nearby else "all matching listings",
        "criteria": {
            "brand": brand.name,
            "model": car_model.name,
            "generation": generation.name if generation else None,
            "year": year,
            "mileage_km": mileage_km,
            "customs_status": customs_status,
        },
    }


def price_trend(
    session: Session,
    *,
    brand_name: str,
    model_name: str,
    generation_name: str | None,
    year: int,
    mileage_km: int,
    days: int = 365,
) -> dict:
    brand, car_model, generation = resolve_vehicle_ids(session, brand_name, model_name, generation_name)
    conditions = [
        DailyMarketStat.brand_id == brand.id,
        DailyMarketStat.car_model_id == car_model.id,
        DailyMarketStat.year == year,
        DailyMarketStat.stat_date >= date.today() - timedelta(days=days),
    ]
    if generation:
        conditions.append(DailyMarketStat.generation_id == generation.id)

    bucket = mileage_bucket(mileage_km)
    rows = session.scalars(
        select(DailyMarketStat)
        .where(*conditions, DailyMarketStat.mileage_bucket == bucket)
        .order_by(DailyMarketStat.stat_date)
    ).all()
    if not rows:
        rows = session.scalars(
            select(DailyMarketStat).where(*conditions).order_by(DailyMarketStat.stat_date)
        ).all()

    daily: dict[date, list[DailyMarketStat]] = defaultdict(list)
    for row in rows:
        daily[row.stat_date].append(row)
    points = []
    for stat_date, items in sorted(daily.items()):
        total_samples = sum(item.sample_count for item in items)
        weighted_avg = sum(float(item.avg_price_usd) * item.sample_count for item in items) / total_samples
        points.append(
            {
                "date": stat_date.isoformat(),
                "average_price_usd": round(weighted_avg, 2),
                "sample_count": total_samples,
            }
        )
    return {"mileage_bucket": bucket, "points": points}
