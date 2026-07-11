from __future__ import annotations

from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Brand, Listing


def get_dashboard_summary(session: Session) -> dict:
    total_listings = session.scalar(select(func.count(Listing.id))) or 0
    active_listings = (
        session.scalar(select(func.count(Listing.id)).where(Listing.is_active.is_(True))) or 0
    )
    analytics_eligible = (
        session.scalar(
            select(func.count(Listing.id)).where(
                Listing.is_active.is_(True),
                Listing.analytics_eligible.is_(True),
                Listing.current_price_usd.is_not(None),
            )
        )
        or 0
    )
    brand_count = (
        session.scalar(
            select(func.count(func.distinct(Listing.brand_id))).where(
                Listing.is_active.is_(True),
                Listing.analytics_eligible.is_(True),
                Listing.brand_id.is_not(None),
            )
        )
        or 0
    )
    model_count = (
        session.scalar(
            select(func.count(func.distinct(Listing.car_model_id))).where(
                Listing.is_active.is_(True),
                Listing.analytics_eligible.is_(True),
                Listing.car_model_id.is_not(None),
            )
        )
        or 0
    )

    prices = session.scalars(
        select(Listing.current_price_usd).where(
            Listing.is_active.is_(True),
            Listing.analytics_eligible.is_(True),
            Listing.current_price_usd.is_not(None),
        )
    ).all()
    price_values = [float(p) for p in prices if p is not None]
    avg_price = sum(price_values) / len(price_values) if price_values else 0.0
    med_price = float(median(price_values)) if price_values else 0.0

    last_seen_at = session.scalar(select(func.max(Listing.last_seen_at)))

    return {
        "total_listings": total_listings,
        "active_listings": active_listings,
        "analytics_eligible_listings": analytics_eligible,
        "brand_count": brand_count,
        "model_count": model_count,
        "average_price_usd": round(avg_price, 2),
        "median_price_usd": round(med_price, 2),
        "last_seen_at": last_seen_at,
    }


def get_brand_stats(session: Session, limit: int = 20) -> list[dict]:
    rows = session.execute(
        select(
            Brand.name,
            func.count(Listing.id).label("active_count"),
            func.avg(Listing.current_price_usd).label("avg_price"),
        )
        .join(Listing, Listing.brand_id == Brand.id)
        .where(
            Listing.is_active.is_(True),
            Listing.analytics_eligible.is_(True),
            Listing.current_price_usd.is_not(None),
        )
        .group_by(Brand.id, Brand.name)
        .order_by(func.count(Listing.id).desc())
        .limit(limit)
    ).all()

    result: list[dict] = []
    for brand_name, active_count, avg_price in rows:
        prices = session.scalars(
            select(Listing.current_price_usd)
            .join(Brand, Brand.id == Listing.brand_id)
            .where(
                Brand.name == brand_name,
                Listing.is_active.is_(True),
                Listing.analytics_eligible.is_(True),
                Listing.current_price_usd.is_not(None),
            )
        ).all()
        values = [float(p) for p in prices if p is not None]
        med_price = float(median(values)) if values else 0.0
        result.append(
            {
                "brand": brand_name,
                "active_count": int(active_count or 0),
                "average_price_usd": round(float(avg_price or 0), 2),
                "median_price_usd": round(med_price, 2),
            }
        )
    return result
