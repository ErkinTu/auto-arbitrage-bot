from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Brand, CarModel, DailyMarketStat, Generation, Listing, RawPayload
from app.services.analytics import select_by_mileage
from app.services.images import extract_image_url as _extract_image_url
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
    image_url = chosen.image_url
    if image_url is None:
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
    # Отдаём ВСЕ аномалии, а не top-10: панель фильтрует и листает их у себя, а
    # с обрезанным списком «Всего: 34» рядом с пятью строками читалось как
    # ошибка. Размер ответа ограничен не здесь, а когортой: `get_vehicle_summary`
    # берёт не больше 1000 объявлений, и выбросов IQR среди них — доли процента.
    # Порядок по умолчанию — сильнее отклонение: сортировку панель меняет сама.
    return {
        "count": len(items),
        "low_count": low_count,
        "high_count": high_count,
        "items": items,
    }


Granularity = Literal["week", "month"]

# Окна разной длины намеренно: недельная точка частая, за 3 года их было бы ~156
# и ось превращалась бы в кашу — полгода читается. Месячная точка редкая, туда
# влезает вся история.
_WINDOW_DAYS: dict[str, int] = {"week": 26 * 7, "month": 1095}

# Периоды режем по UTC-датам, а не по Asia/Bishkek: `daily_market_stats.stat_date`
# пишется воркером как `date.today()` в UTC-контейнере, и если приток считать по
# местному календарю, два графика на одном экране разъедутся на границах недель.


def _period_start(day: date, granularity: str) -> date:
    if granularity == "week":
        return day - timedelta(days=day.weekday())  # понедельник ISO-недели
    return day.replace(day=1)


def _period_label(start: date, granularity: str) -> str:
    if granularity == "week":
        iso = start.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return start.strftime("%Y-%m")


def _next_period(start: date, granularity: str) -> date:
    if granularity == "week":
        return start + timedelta(days=7)
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1)


def _trend_points(
    session: Session,
    *,
    brand_id: int,
    car_model_id: int,
    generation_id: int | None,
    year: int,
    granularity: str,
) -> list[dict]:
    conditions = [
        DailyMarketStat.brand_id == brand_id,
        DailyMarketStat.car_model_id == car_model_id,
        DailyMarketStat.year == year,
        DailyMarketStat.stat_date >= date.today() - timedelta(days=_WINDOW_DAYS[granularity]),
    ]
    if generation_id is not None:
        conditions.append(DailyMarketStat.generation_id == generation_id)

    rows = session.scalars(
        select(DailyMarketStat).where(*conditions).order_by(DailyMarketStat.stat_date)
    ).all()
    if not rows:
        return []

    grouped: dict[date, list[DailyMarketStat]] = defaultdict(list)
    for row in rows:
        grouped[_period_start(row.stat_date, granularity)].append(row)

    points: list[dict] = []
    for start in sorted(grouped):
        bucket = grouped[start]
        total_samples = sum(item.sample_count for item in bucket)
        if total_samples == 0:
            continue
        # Цена периода — средневзвешенная по дневным срезам: день с 20 машинами
        # должен весить больше дня с двумя.
        weighted_avg = (
            sum(float(item.avg_price_usd) * item.sample_count for item in bucket) / total_samples
        )
        # Медиана — медиана дневных медиан, а НЕ их средневзвешенное: усреднять
        # медианы значит получить величину, которая медианой уже не является.
        period_median = float(median(sorted(float(item.median_price_usd) for item in bucket)))
        # Строки daily_market_stats — суточные СРЕЗЫ одного и того же запаса, их
        # сумма за неделю считала бы одну машину семь раз. Показываем среднее
        # число активных объявлений в сутки — это и есть «сколько их стояло».
        days = len({item.stat_date for item in bucket})
        points.append(
            {
                "period": _period_label(start, granularity),
                "period_start": start,
                "average_price_usd": round(weighted_avg, 2),
                "median_price_usd": round(period_median, 2),
                "sample_count": round(total_samples / days) if days else 0,
            }
        )
    return points


def _intake_points(
    session: Session,
    *,
    brand_id: int,
    car_model_id: int,
    generation_id: int | None,
    year: int,
    granularity: str,
) -> list[dict]:
    """Сколько объявлений появилось у нас за период.

    Дата — `first_seen_at` (когда объявление увидели мы), а не
    `source_created_at`: последний nullable, и объявления без даты публикации
    молча выпали бы из притока.

    Фильтров `is_active`/`analytics_eligible` тут НЕТ намеренно, в отличие от
    ценовой когорты: приток — исторический факт, а активность меняется задним
    числом. С фильтром старые недели пустели бы по мере снятия объявлений с
    продажи, и график рисовал бы несуществующий рост к сегодняшнему дню.
    """
    since = datetime.now(timezone.utc) - timedelta(days=_WINDOW_DAYS[granularity])
    conditions = [
        Listing.brand_id == brand_id,
        Listing.car_model_id == car_model_id,
        Listing.year == year,
        Listing.first_seen_at >= since,
    ]
    if generation_id is not None:
        conditions.append(Listing.generation_id == generation_id)

    seen_at = session.scalars(select(Listing.first_seen_at).where(*conditions)).all()
    if not seen_at:
        return []

    counts: dict[date, int] = defaultdict(int)
    for moment in seen_at:
        if moment is None:
            continue
        # SQLite в тестах отдаёт naive datetime; на проде поле timezone-aware.
        day = (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(
            timezone.utc
        ).date()
        counts[_period_start(day, granularity)] += 1
    if not counts:
        return []

    # Пустые периоды внутри истории — это честный ноль (объявлений не появилось),
    # и пропустить их значило бы нарисовать непрерывный поток. А вот до первого
    # объявления нулей не рисуем: там не «мёртвый рынок», там просто нет наших
    # данных — источник мог быть подключён позже.
    points: list[dict] = []
    cursor = min(counts)
    last = _period_start(date.today(), granularity)
    while cursor <= last:
        points.append(
            {
                "period": _period_label(cursor, granularity),
                "period_start": cursor,
                "count": counts.get(cursor, 0),
            }
        )
        cursor = _next_period(cursor, granularity)
    return points


def get_vehicle_summary(
    session: Session,
    *,
    brand_id: int,
    model_id: int,
    year: int,
    generation_id: int | None = None,
    mileage_km: int | None = None,
    granularity: Granularity = "week",
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
    if granularity not in _WINDOW_DAYS:
        granularity = "week"
    trend = _trend_points(
        session,
        brand_id=brand_id,
        car_model_id=model_id,
        generation_id=generation_id,
        year=year,
        granularity=granularity,
    )
    intake = _intake_points(
        session,
        brand_id=brand_id,
        car_model_id=model_id,
        generation_id=generation_id,
        year=year,
        granularity=granularity,
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
        "granularity": granularity,
        "trend": trend,
        "intake": intake,
    }
