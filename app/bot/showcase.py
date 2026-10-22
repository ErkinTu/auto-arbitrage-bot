"""Витрина «лучшие сейчас»: самые дешёвые относительно рынка за последние сутки.

Считается живьём и кэшируется на SHOWCASE_TTL_SECONDS, а не хранится таблицей:
объявления протухают за часы, и сохранённая «десятка» через полдня вела бы на
мёртвые страницы. Берём только активные — снятое валидатором или full-scan-ом
пропадает из витрины со следующим пересчётом.

Чистая sync-логика без aiogram: зовут бот (`/best`, демо в онбординге — через
asyncio.to_thread) и worker (итоги триала с пустым результатом).
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bot.matching import (
    PendingNotification,
    is_complete_listing,
    is_plausible_discount,
    market_average,
)
from app.models import Brand, Listing, Source
from app.services.normalizer import MIN_PLAUSIBLE_PRICE_USD
from app.services.regions import is_foreign
from app.services.stats import mileage_bucket
from app.services.sync import utcnow
from app.services.vehicle_summary import _build_source_url as build_source_url

# Чуть дольше цикла рассылки (10 мин): бот прогревает витрину после каждого
# цикла, и кэш не должен истечь раньше, чем придёт следующий прогрев.
SHOWCASE_TTL_SECONDS = 900
SHOWCASE_SIZE = 12  # 4 страницы по 3 — дальше листать витрину уже никто не станет
SHOWCASE_PAGE = 3
SHOWCASE_WINDOW_HOURS = 24

_lock = threading.Lock()
_cache: tuple[float, list[PendingNotification]] = (0.0, [])


def market_averages(
    session: Session, listings, *, min_samples: int
) -> dict[Listing, float | None]:
    """`market_average` для пачки объявлений с кэшем по когорте.

    Когорта = марка+модель+год+бакет пробега (NULL-пробег — своя ветка, как в
    самой `market_average`). Свежих за сутки ~2500, когорт среди них заметно
    меньше, а каждая — два запроса. Зарубежные отбрасываются: средней для них
    нет по построению.
    """
    avg_by_cohort: dict[tuple, float | None] = {}
    result: dict[Listing, float | None] = {}
    for listing in listings:
        if is_foreign(listing.city):
            continue
        cohort = (
            listing.brand_id,
            listing.car_model_id,
            listing.year,
            mileage_bucket(listing.mileage_km) if listing.mileage_km is not None else None,
        )
        if cohort not in avg_by_cohort:
            avg_by_cohort[cohort] = market_average(session, listing, min_samples=min_samples)
        result[listing] = avg_by_cohort[cohort]
    return result


def compute_showcase(
    session: Session,
    *,
    min_samples: int,
    max_below_percent: float = 60.0,
    window_hours: int = SHOWCASE_WINDOW_HOURS,
    size: int = SHOWCASE_SIZE,
) -> list[PendingNotification]:
    """Лучшие по % ниже рынка среди опубликованных за `window_hours` (chat_id=0).

    `market_average` — та же, что у рассылки: витрина показывает ровно то, что
    человек получал бы по фильтру «ниже рынка». Только полные данные
    (`is_complete_listing`) и правдоподобная скидка: витрина — лицо бота.
    """
    published_after = utcnow() - timedelta(hours=window_hours)
    candidates = session.scalars(
        select(Listing).where(
            Listing.is_active.is_(True),
            Listing.analytics_eligible.is_(True),
            Listing.current_price_usd.is_not(None),
            Listing.current_price_usd >= MIN_PLAUSIBLE_PRICE_USD,
            Listing.source_created_at >= published_after,
            Listing.brand_id.is_not(None),
            Listing.car_model_id.is_not(None),
            Listing.year.is_not(None),
            Listing.mileage_km.is_not(None),
        )
    ).all()
    now = utcnow()
    candidates = [listing for listing in candidates if is_complete_listing(listing, now)]
    source_names = {row.id: row.name for row in session.execute(select(Source.id, Source.name))}
    brand_names = {row.id: row.name for row in session.execute(select(Brand.id, Brand.name))}

    scored: list[tuple[float, Listing, float]] = []
    for listing, avg in market_averages(session, candidates, min_samples=min_samples).items():
        if avg is None or avg <= 0:
            continue
        percent = round((1 - float(listing.current_price_usd) / avg) * 100, 1)
        if percent > 0 and is_plausible_discount(percent, max_below_percent):
            scored.append((percent, listing, avg))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        PendingNotification(
            chat_id=0,
            listing_id=listing.id,
            filter_id=0,  # витрина не привязана к фильтру
            title=listing.title,
            year=listing.year,
            mileage_km=listing.mileage_km,
            price_usd=float(listing.current_price_usd),
            city=listing.city,
            url=build_source_url(listing),
            below_market_percent=percent,
            market_avg_usd=avg,
            source_name=source_names.get(listing.source_id, "mashina.kg"),
            brand_name=brand_names.get(listing.brand_id),
        )
        for percent, listing, avg in scored[:size]
    ]


def get_showcase(
    session_factory,
    *,
    min_samples: int,
    max_below_percent: float = 60.0,
    refresh: bool = False,
) -> list[PendingNotification]:
    """Витрина из кэша процесса; пересчёт не чаще раза в SHOWCASE_TTL_SECONDS.

    `refresh=True` — пересчитать сейчас (прогрев из цикла бота после парса).
    Под замком: два одновременных `/best` после истечения кэша не должны
    считать одно и то же дважды — второй дождётся первого и возьмёт готовое.
    """
    global _cache
    with _lock:
        cached_until, cached = _cache
        if not refresh and time.monotonic() < cached_until:
            return cached
        with session_factory() as session:
            fresh = compute_showcase(
                session, min_samples=min_samples, max_below_percent=max_below_percent
            )
        _cache = (time.monotonic() + SHOWCASE_TTL_SECONDS, fresh)
        return fresh


def page_for_chat(
    items: list[PendingNotification], chat_id: int, offset: int, page: int = SHOWCASE_PAGE
) -> tuple[list[PendingNotification], int | None]:
    """Страница витрины для чата и offset следующей (None — дальше пусто)."""
    chunk = [replace(item, chat_id=chat_id) for item in items[offset : offset + page]]
    next_offset = offset + page
    return chunk, (next_offset if next_offset < len(items) else None)
