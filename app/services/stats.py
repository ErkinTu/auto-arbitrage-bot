from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import DailyMarketStat, Listing
from app.services.regions import is_foreign


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


def rebuild_daily_market_stats(
    session: Session,
    stat_date: date | None = None,
    *,
    max_last_seen_days: int | None = None,
    outlier_min_cohort: int | None = None,
    outlier_price_median_multiple: float | None = None,
) -> int:
    stat_date = stat_date or date.today()
    settings = get_settings()
    if max_last_seen_days is None:
        max_last_seen_days = settings.stats_max_last_seen_days
    if outlier_min_cohort is None:
        outlier_min_cohort = settings.outlier_min_cohort
    if outlier_price_median_multiple is None:
        outlier_price_median_multiple = settings.outlier_price_median_multiple
    # Derived rows are rebuilt from scratch so old model aliases cannot leave stale statistics.
    session.execute(delete(DailyMarketStat).where(DailyMarketStat.stat_date == stat_date))
    session.flush()
    # У fresh-only источников (lalafo) проданное авто может висеть is_active=true:
    # в агрегаты идут только объявления, виденные в ленте недавно.
    seen_after = datetime.now(timezone.utc) - timedelta(days=max_last_seen_days)
    listings = session.scalars(
        select(Listing).where(
            Listing.is_active.is_(True),
            Listing.analytics_eligible.is_(True),
            Listing.last_seen_at >= seen_after,
            Listing.current_price_usd.is_not(None),
            Listing.brand_id.is_not(None),
            Listing.car_model_id.is_not(None),
        )
    ).all()
    # Зарубежные объявления (в поле города — страна: Китай, Корея, США...) — вне
    # статистики целиком, ДО отсева выбросов и раскладки по бакетам. Внутри одной
    # когорты «модель+год» они стабильно дешевле местных на 20-35% (цена без
    # растаможки и доставки — не тот же товар), а по горячим когортам их бывает
    # больше, чем местных (замер на дампе 2026-09-06: Geely Galaxy 2026 — 323
    # зарубежных против 85 местных, средняя $22.4k против $28.7k). Оставить их —
    # значит систематически занижать «рынок» и раздавать ложные «ниже рынка».
    # Цена: ~17% когорт набирали min_samples только за счёт зарубежного навеса
    # и теперь замолчат — по правилу «нет надёжной статистики → тишина».
    # Фильтр в Python, а не в SQL: `is_foreign` делает strip(), а NOT IN на
    # NULL-городе отбросил бы и объявления без города.
    listings = [listing for listing in listings if not is_foreign(listing.city)]

    # Отсев выбросов ПЕРЕД раскладкой по бакетам: когорта — «модель+год» целиком
    # (не bucket), т.к. один модельный год — связная ценовая группа (пробег двигает
    # цену в ~2–3×, а не в 100×). Заодно чистит и bucket unknown, где сидят NULL-пробеги.
    cohort_prices: dict[tuple, list[Decimal]] = defaultdict(list)
    for listing in listings:
        cohort_prices[(listing.brand_id, listing.car_model_id, listing.year)].append(
            Decimal(listing.current_price_usd)
        )
    k = Decimal(str(outlier_price_median_multiple))
    cohort_bounds: dict[tuple, tuple[Decimal, Decimal]] = {}
    for cohort_key, prices in cohort_prices.items():
        if len(prices) >= outlier_min_cohort:
            med = Decimal(str(median(sorted(prices))))
            cohort_bounds[cohort_key] = (med / k, med * k)

    groups: dict[tuple, list[Decimal]] = defaultdict(list)
    for listing in listings:
        price = Decimal(listing.current_price_usd)
        bounds = cohort_bounds.get((listing.brand_id, listing.car_model_id, listing.year))
        if bounds is not None and not (bounds[0] <= price <= bounds[1]):
            continue  # выброс относительно своей когорты — вне avg/median/sample_count
        key = (
            listing.brand_id,
            listing.car_model_id,
            listing.generation_id,
            listing.year,
            mileage_bucket(listing.mileage_km),
            listing.customs_status,
        )
        groups[key].append(price)

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
