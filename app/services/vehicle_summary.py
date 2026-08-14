from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Brand, CarModel, DailyMarketStat, Generation, Listing, RawPayload
from app.services.analytics import select_by_mileage
from app.services.sources import listing_public_url


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    position = (len(sorted_values) - 1) * p
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def _build_source_url(listing: Listing) -> str:
    return listing_public_url(listing.source_slug, listing.external_id)


def _extract_image_url(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    # mashina.kg payloads keep galleries under a few possible keys; we probe all of them.
    for key in ("photo", "preview_image"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    for key in ("photos", "images"):
        gallery = payload.get(key)
        if isinstance(gallery, list) and gallery:
            first = gallery[0]
            if isinstance(first, str) and first.startswith(("http://", "https://")):
                return first
            if isinstance(first, dict):
                for url_key in ("url", "src", "image", "large", "medium", "small"):
                    candidate = first.get(url_key)
                    if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                        return candidate
    return None


def _representative_listing(
    session: Session,
    listings: list[Listing],
    market_median: float,
) -> dict | None:
    if not listings:
        return None
    # Pick the listing whose price is closest to the market median.
    chosen = min(
        listings,
        key=lambda item: abs(float(item.current_price_usd or 0) - market_median),
    )
    payload_row = session.scalar(
        select(RawPayload)
        .where(RawPayload.listing_id == chosen.id)
        .order_by(RawPayload.observed_at.desc())
    )
    image_url = _extract_image_url(payload_row.payload if payload_row else None)
    title = chosen.title or "Без названия"
    return {
        "id": chosen.id,
        "title": title,
        "image_url": image_url,
        "source_url": _build_source_url(chosen),
    }


def _detect_anomalies(listings: list[Listing], market_average: float) -> dict:
    priced = [item for item in listings if item.current_price_usd is not None]
    prices = [float(item.current_price_usd) for item in priced]
    if len(prices) < 4:
        return {"count": 0, "low_count": 0, "high_count": 0, "items": []}

    q1 = _percentile(prices, 0.25)
    q3 = _percentile(prices, 0.75)
    iqr = q3 - q1
    low_bound = q1 - 1.5 * iqr
    high_bound = q3 + 1.5 * iqr

    items: list[dict] = []
    low_count = 0
    high_count = 0
    for listing in priced:
        price = float(listing.current_price_usd)
        if price < low_bound:
            low_count += 1
        elif price > high_bound:
            high_count += 1
        else:
            continue
        deviation = (
            (price - market_average) / market_average * 100.0 if market_average else 0.0
        )
        items.append(
            {
                "id": listing.id,
                "title": listing.title or "Без названия",
                "year": listing.year or 0,
                "mileage_km": listing.mileage_km,
                "city": listing.city,
                "price_usd": round(price, 2),
                "deviation_percent": round(deviation, 2),
                "source_url": _build_source_url(listing),
            }
        )

    items.sort(key=lambda x: abs(x["deviation_percent"]), reverse=True)
    return {
        "count": len(items),
        "low_count": low_count,
        "high_count": high_count,
        "items": items[:10],
    }


def _trend_points(
    session: Session,
    *,
    brand_id: int,
    car_model_id: int,
    generation_id: int | None,
    year: int,
    days: int = 1095,
) -> list[dict]:
    conditions = [
        DailyMarketStat.brand_id == brand_id,
        DailyMarketStat.car_model_id == car_model_id,
        DailyMarketStat.year == year,
        DailyMarketStat.stat_date >= date.today() - timedelta(days=days),
    ]
    if generation_id is not None:
        conditions.append(DailyMarketStat.generation_id == generation_id)

    rows = session.scalars(
        select(DailyMarketStat).where(*conditions).order_by(DailyMarketStat.stat_date)
    ).all()
    if not rows:
        return []

    # Aggregate by year-month to make the chart readable across 2024-2026.
    grouped: dict[str, list[DailyMarketStat]] = defaultdict(list)
    for row in rows:
        key = row.stat_date.strftime("%Y-%m")
        grouped[key].append(row)

    points: list[dict] = []
    for period in sorted(grouped.keys()):
        bucket = grouped[period]
        total_samples = sum(item.sample_count for item in bucket)
        if total_samples == 0:
            continue
        weighted_avg = (
            sum(float(item.avg_price_usd) * item.sample_count for item in bucket) / total_samples
        )
        weighted_med = (
            sum(float(item.median_price_usd) * item.sample_count for item in bucket)
            / total_samples
        )
        points.append(
            {
                "period": period,
                "average_price_usd": round(weighted_avg, 2),
                "median_price_usd": round(weighted_med, 2),
                "sample_count": total_samples,
            }
        )
    return points


def get_vehicle_summary(
    session: Session,
    *,
    brand_id: int,
    model_id: int,
    year: int,
    generation_id: int | None = None,
    mileage_km: int | None = None,
) -> dict:
    brand = session.get(Brand, brand_id)
    car_model = session.get(CarModel, model_id)
    if not brand or not car_model:
        raise LookupError("Марка или модель не найдены")
    generation = session.get(Generation, generation_id) if generation_id else None

    conditions = [
        Listing.is_active.is_(True),
        Listing.analytics_eligible.is_(True),
        Listing.brand_id == brand_id,
        Listing.car_model_id == model_id,
        Listing.year == year,
        Listing.current_price_usd.is_not(None),
    ]
    if generation_id is not None:
        conditions.append(Listing.generation_id == generation_id)

    listings = session.scalars(select(Listing).where(*conditions).limit(1000)).all()
    if not listings:
        raise LookupError("Недостаточно похожих активных объявлений")

    cohort_prices = [
        float(item.current_price_usd) for item in listings if item.current_price_usd is not None
    ]
    # Рынок описывает когорта «модель+год» целиком — пробег её не сужает:
    # средняя, аномалии и представитель отвечают на вопрос «что происходит с
    # этой машиной на рынке», а не «сколько стоит именно моя».
    market_average = mean(cohort_prices)
    market_median = float(median(cohort_prices))

    # Оценка — наоборот, про конкретный экземпляр: тем же кодом, что и /estimate.
    window = select_by_mileage(list(listings), mileage_km)
    prices = window.prices or cohort_prices
    q1 = _percentile(prices, 0.25)
    q3 = _percentile(prices, 0.75)

    samples = len(prices)
    confidence = "high" if samples >= 20 else "medium" if samples >= 8 else "low"

    representative = _representative_listing(session, list(listings), market_median)
    anomalies = _detect_anomalies(list(listings), market_average)
    trend = _trend_points(
        session,
        brand_id=brand_id,
        car_model_id=model_id,
        generation_id=generation_id,
        year=year,
    )

    return {
        "vehicle": {
            "brand": brand.name,
            "model": car_model.name,
            "generation": generation.name if generation else None,
            "year": year,
        },
        "currency": "USD",
        "estimated_price": round(window.estimated_price, 2),
        "estimated_range": {"min": round(q1, 2), "max": round(q3, 2)},
        "market_average": round(market_average, 2),
        "market_median": round(market_median, 2),
        "samples": samples,
        "confidence": confidence,
        "mileage": {
            "requested_km": mileage_km,
            "window": window.window,
            "samples_in_window": samples,
            "adjustment_usd": round(window.adjustment_usd, 2),
        },
        "representative_listing": representative,
        "anomalies": anomalies,
        "trend": trend,
    }
