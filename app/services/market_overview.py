"""Аналитика рынка для сайта (подписчикам): поступления, распределение цен, срок продажи.

Первая версия графиков раздела «Аналитика» по демо (10.11.2026):

- **Поступления по дням** — сколько объявлений мы впервые увидели за сутки, по
  источникам. Дата — `first_seen_at` (UTC; окно активности бота целиком внутри
  одних UTC-суток Бишкека, так что сутки не режутся). Активность не фильтруем:
  приток — исторический факт (та же логика, что `vehicle_summary._intake_points`).
- **Распределение цен** — гистограмма активных «годных для аналитики» цен, всего
  рынка или одной марки. Корзины — те же, что у оценки
  (`vehicle_summary._price_distribution`: усы IQR, хвосты отдельно).
- **Сколько висит объявление** — медиана дней от `first_seen_at` до
  `last_seen_at` (последний раз видели живым) у снятых за `LIQUIDITY_DAYS`
  дней, по маркам, **только mashina**. Почему так (замер на локальной базе
  10.11.2026): по `deactivated_at` у всех марок выходило ~66 дней — там
  задержка обнаружения (у «зависших» +30 дней фолбэка); а lalafo мы обходим
  только по свежим, и снятое объявление «видели последний раз» почти в момент
  публикации (медиана 0,5 дня). У mashina полный обход раз в неделю — сроки
  15–26 дней и различаются по маркам. Это «сколько было в продаже», а не «за
  сколько продали»: снятие бывает и без продажи; точность — до недели обхода.

Цены и сроки собираем списком и считаем в Python: это два столбца, а
`percentile_cont` по произвольным корзинам переносимо не выразить. Ответ
кэшируется ручкой (`cached_call`), так что проход один на TTL.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Brand, Listing, Source
from app.services.sources import MASHINA
from app.services.vehicle_summary import _price_distribution

INTAKE_DAYS = 30
LIQUIDITY_DAYS = 60
LIQUIDITY_MIN_SAMPLES = 30  # меньше — медиана по марке шумит
TOP_BRANDS = 12


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def intake_by_day(session: Session, *, days: int = INTAKE_DAYS) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day = func.date(Listing.first_seen_at)
    rows = session.execute(
        select(day, Source.name, func.count(Listing.id))
        .join(Source, Source.id == Listing.source_id)
        .where(Listing.first_seen_at >= since)
        .group_by(day, Source.name)
    ).all()
    by_day: dict[date, dict[str, int]] = defaultdict(dict)
    for raw_day, source, count in rows:
        key = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day))
        by_day[key][source] = int(count)
    if not by_day:
        return []
    # Пустые сутки внутри окна — честный ноль, до первых данных — не рисуем.
    points, cursor, last = [], min(by_day), datetime.now(timezone.utc).date()
    while cursor <= last:
        counts = by_day.get(cursor, {})
        points.append(
            {
                "day": cursor,
                "mashina": counts.get("mashina.kg", 0),
                "lalafo": counts.get("lalafo.kg", 0),
                "total": sum(counts.values()),
            }
        )
        cursor += timedelta(days=1)
    return points


def price_distribution(session: Session, *, brand_id: int | None = None) -> dict:
    conditions = [
        Listing.is_active.is_(True),
        Listing.analytics_eligible.is_(True),
        Listing.current_price_usd.is_not(None),
    ]
    if brand_id is not None:
        conditions.append(Listing.brand_id == brand_id)
    prices = [float(p) for p in session.scalars(select(Listing.current_price_usd).where(*conditions))]
    # Рынок большой (~100 тыс. цен) — корзин больше, чем у оценки одной модели.
    result = _price_distribution(prices, max_bins=24)
    result["median_usd"] = round(median(prices), 2) if prices else None
    return result


def liquidity_by_brand(session: Session, *, days: int = LIQUIDITY_DAYS) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(
        select(Brand.name, Listing.first_seen_at, Listing.last_seen_at)
        .join(Brand, Brand.id == Listing.brand_id)
        .join(Source, Source.id == Listing.source_id)
        .where(
            Source.name == MASHINA,
            Listing.is_active.is_(False),
            Listing.deactivated_at.is_not(None),
            Listing.deactivated_at >= since,
            Listing.first_seen_at.is_not(None),
            Listing.last_seen_at.is_not(None),
        )
    ).all()
    durations: dict[str, list[float]] = defaultdict(list)
    for name, seen, last in rows:
        span = (_utc(last) - _utc(seen)).total_seconds() / 86400
        if span >= 0:
            durations[name].append(span)
    items = [
        {"brand": name, "removed": len(values), "median_days": round(median(values), 1)}
        for name, values in durations.items()
        if len(values) >= LIQUIDITY_MIN_SAMPLES
    ]
    items.sort(key=lambda item: -item["removed"])
    return items[:TOP_BRANDS]


def market_overview(session: Session, *, brand_id: int | None = None) -> dict:
    return {
        "intake": intake_by_day(session),
        "price_distribution": price_distribution(session, brand_id=brand_id),
        "liquidity": liquidity_by_brand(session),
        "liquidity_days": LIQUIDITY_DAYS,
    }
