from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from statistics import median

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import DailyMarketStat, Listing


def mileage_bucket(mileage_km: int | None) -> str:
    if mileage_km is None:
        return "unknown"
    if mileage_km < 50_000:
        return "0-50k"
    if mileage_km < 100_000:
        return "50-100k"
    if mileage_km < 150_000:
        return "100-150k"
    if mileage_km < 200_000:
        return "150-200k"
    return "200k+"


def rebuild_daily_market_stats(session: Session, stat_date: date | None = None) -> int:
    stat_date = stat_date or date.today()
    # Derived rows are rebuilt from scratch so old model aliases cannot leave stale statistics.
    session.execute(delete(DailyMarketStat).where(DailyMarketStat.stat_date == stat_date))
    session.flush()
    listings = session.scalars(
        select(Listing).where(
            Listing.is_active.is_(True),
            Listing.analytics_eligible.is_(True),
            Listing.current_price_usd.is_not(None),
            Listing.brand_id.is_not(None),
            Listing.car_model_id.is_not(None),
        )
    ).all()

    groups: dict[tuple, list[Decimal]] = defaultdict(list)
    for listing in listings:
        key = (
            listing.brand_id,
            listing.car_model_id,
            listing.generation_id,
            listing.year,
            mileage_bucket(listing.mileage_km),
            listing.customs_status,
        )
        groups[key].append(Decimal(listing.current_price_usd))

    written = 0
    for key, prices in groups.items():
        brand_id, car_model_id, generation_id, year, bucket, customs_status = key
        existing = session.scalar(
            select(DailyMarketStat).where(
                DailyMarketStat.stat_date == stat_date,
                DailyMarketStat.brand_id == brand_id,
                DailyMarketStat.car_model_id == car_model_id,
                DailyMarketStat.generation_id == generation_id,
                DailyMarketStat.year == year,
                DailyMarketStat.mileage_bucket == bucket,
                DailyMarketStat.customs_status == customs_status,
            )
        )
        avg_price = sum(prices) / len(prices)
        values = sorted(prices)
        row = existing or DailyMarketStat(
            stat_date=stat_date,
            brand_id=brand_id,
            car_model_id=car_model_id,
            generation_id=generation_id,
            year=year,
            mileage_bucket=bucket,
            customs_status=customs_status,
            avg_price_usd=avg_price,
            median_price_usd=Decimal(str(median(values))),
            min_price_usd=values[0],
            max_price_usd=values[-1],
            sample_count=len(values),
        )
        row.avg_price_usd = avg_price
        row.median_price_usd = Decimal(str(median(values)))
        row.min_price_usd = values[0]
        row.max_price_usd = values[-1]
        row.sample_count = len(values)
        if existing is None:
            session.add(row)
        written += 1

    session.commit()
    return written
