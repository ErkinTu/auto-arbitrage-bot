"""Сводка по базе для публичного дашборда.

Считаем всё в БД. Раньше медианы брались в Python: сводка выгружала 52k цен в
память, а `get_brand_stats` делал это ещё раз на каждую марку — 17 запросов и
десятки тысяч строк ради шестнадцати чисел. На объёмах MVP это работало, но
растёт линейно с базой, а сервер — 2 ГБ на четыре сервиса.

`percentile_cont` даёт ту же медиану (линейная интерполяция между соседями),
что и `statistics.median`, поэтому ответы эндпоинтов не изменились.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Brand, Listing

# Одна и та же выборка «годно для аналитики» во всех цифрах дашборда: активное,
# с распознанной моделью и известной ценой.
_ANALYTICS_FILTER = (
    Listing.is_active.is_(True),
    Listing.analytics_eligible.is_(True),
    Listing.current_price_usd.is_not(None),
)

# То же самое, но без условия на цену: по нему считаются марки и модели.
_ELIGIBLE_ROWS = (
    Listing.is_active.is_(True),
    Listing.analytics_eligible.is_(True),
)


def get_dashboard_summary(session: Session) -> dict:
    # Все счётчики — одним проходом по таблице: FILTER считает разные условия
    # в рамках одного сканирования вместо четырёх отдельных запросов.
    counts = session.execute(
        select(
            func.count(Listing.id),
            func.count(Listing.id).filter(Listing.is_active.is_(True)),
            func.count(Listing.id).filter(*_ANALYTICS_FILTER),
            # Марки и модели считаются БЕЗ условия на цену — так было до этой
            # правки, и производительность менять числа не должна.
            # Расхождение при этом реальное: 668 объявлений помечены
            # analytics_eligible, но цены не имеют, и приносят 3 модели, которых
            # в самой аналитике нет. Сузить — отдельное решение, не рефакторинг.
            func.count(func.distinct(Listing.brand_id)).filter(*_ELIGIBLE_ROWS),
            func.count(func.distinct(Listing.car_model_id)).filter(*_ELIGIBLE_ROWS),
            func.max(Listing.last_seen_at),
        )
    ).one()

    prices = session.execute(
        select(
            func.avg(Listing.current_price_usd),
            func.percentile_cont(0.5).within_group(Listing.current_price_usd),
        ).where(*_ANALYTICS_FILTER)
    ).one()

    return {
        "total_listings": int(counts[0] or 0),
        "active_listings": int(counts[1] or 0),
        "analytics_eligible_listings": int(counts[2] or 0),
        "brand_count": int(counts[3] or 0),
        "model_count": int(counts[4] or 0),
        "average_price_usd": round(float(prices[0]), 2) if prices[0] is not None else 0.0,
        "median_price_usd": round(float(prices[1]), 2) if prices[1] is not None else 0.0,
        "last_seen_at": counts[5],
    }


def get_brand_stats(session: Session, limit: int = 20) -> list[dict]:
    rows = session.execute(
        select(
            Brand.name,
            func.count(Listing.id),
            func.avg(Listing.current_price_usd),
            func.percentile_cont(0.5).within_group(Listing.current_price_usd),
        )
        .join(Listing, Listing.brand_id == Brand.id)
        .where(*_ANALYTICS_FILTER)
        .group_by(Brand.id, Brand.name)
        .order_by(func.count(Listing.id).desc())
        .limit(limit)
    ).all()

    return [
        {
            "brand": name,
            "active_count": int(count or 0),
            "average_price_usd": round(float(avg_price or 0), 2),
            "median_price_usd": round(float(median_price), 2) if median_price is not None else 0.0,
        }
        for name, count, avg_price, median_price in rows
    ]
