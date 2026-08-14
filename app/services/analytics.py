from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, median
from typing import Sequence

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


MILEAGE_WINDOW_KM = 50_000
MIN_WINDOW_SAMPLES = 3
WINDOW_NEARBY = "±50,000 km"
WINDOW_ALL = "all matching listings"


@dataclass(frozen=True)
class MileageWindow:
    """Какие объявления взяты под оценку и что дала поправка на пробег."""

    prices: list[float]
    window: str
    estimated_price: float
    adjustment_usd: float


def select_by_mileage(samples: Sequence[Listing], mileage_km: int | None) -> MileageWindow:
    """Сужает выборку до похожего пробега и правит оценку регрессией.

    Общий код для `/estimate` и `/market/vehicle-summary`: пока он был только в
    первом, второй отдавал во фронт медиану когорты без поправки — на реальных
    данных расхождение по одной машине доходило до полутора тысяч долларов.
    Дублировать логику нельзя: две оценки одного и того же авто разъезжаются
    молча.

    `mileage_km=None` — не ошибка, а обычный случай: у lalafo пробег не указан
    примерно у 43% объявлений. Тогда берём когорту целиком и поправку не делаем.
    """
    all_prices = [float(item.current_price_usd) for item in samples if item.current_price_usd is not None]
    cohort_median = float(median(all_prices)) if all_prices else 0.0

    if mileage_km is None:
        return MileageWindow(
            prices=all_prices,
            window=WINDOW_ALL,
            estimated_price=cohort_median,
            adjustment_usd=0.0,
        )

    nearby = [
        item
        for item in samples
        if item.mileage_km is not None and abs(item.mileage_km - mileage_km) <= MILEAGE_WINDOW_KM
    ]
    # Окно уже когорты, но по трём объявлениям медиану считать бессмысленно —
    # тогда возвращаемся ко всей выборке и честно говорим об этом в `window`.
    use_nearby = len(nearby) >= MIN_WINDOW_SAMPLES
    selected = nearby if use_nearby else list(samples)
    prices = [float(item.current_price_usd) for item in selected if item.current_price_usd is not None]
    base_median = float(median(prices)) if prices else cohort_median
    estimated = _linear_estimate(list(samples), mileage_km, base_median)

    return MileageWindow(
        prices=prices,
        window=WINDOW_NEARBY if use_nearby else WINDOW_ALL,
        estimated_price=estimated,
        adjustment_usd=estimated - cohort_median,
    )


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

    window = select_by_mileage(all_samples, mileage_km)
    prices = window.prices
    q1, q3 = _percentile(prices, 0.25), _percentile(prices, 0.75)
    sample_count = len(prices)
    confidence = "high" if sample_count >= 20 else "medium" if sample_count >= 8 else "low"

    return {
        "currency": "USD",
        "estimated_price": round(window.estimated_price, 2),
        "estimated_range": {"min": round(q1, 2), "max": round(q3, 2)},
        "market_average": round(mean(prices), 2),
        "market_median": round(float(median(prices)), 2),
        "samples": sample_count,
        "confidence": confidence,
        "used_mileage_window": window.window,
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
