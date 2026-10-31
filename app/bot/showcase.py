"""Витрина «лучшие сейчас»: самые дешёвые относительно рынка за последние сутки.

Считается живьём и кэшируется на SHOWCASE_TTL_SECONDS, а не хранится таблицей:
объявления протухают за часы, и сохранённая «десятка» через полдня вела бы на
мёртвые страницы. Берём только активные — снятое валидатором или full-scan-ом
пропадает из витрины со следующим пересчётом.

Чистая sync-логика без aiogram: зовут бот (`/best`, витрина в `/start` — через
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
    BASIS_AVG,
    MarketEstimate,
    PendingNotification,
    build_pending,
    is_complete_listing,
    is_plausible_discount,
    load_model_names,
    market_estimate,
)
from app.models import Brand, Listing, Source
from app.services.normalizer import MIN_PLAUSIBLE_PRICE_USD
from app.services.regions import is_foreign
from app.services.stats import mileage_bucket
from app.services.sync import utcnow

# Чуть дольше цикла рассылки (10 мин): бот прогревает витрину после каждого
# цикла, и кэш не должен истечь раньше, чем придёт следующий прогрев.
SHOWCASE_TTL_SECONDS = 900
SHOWCASE_SIZE = 12  # 4 страницы по 3 — дальше листать витрину уже никто не станет
SHOWCASE_PAGE = 3
SHOWCASE_WINDOW_HOURS = 24

_lock = threading.Lock()
_cache: tuple[float, list[PendingNotification]] = (0.0, [])

# Что недавно показано чату (витрина в /start, «Лучшее сейчас», превью фильтра).
# Без этого человек, нажавший /start → триал → «Лучшее сейчас», получал одни и те
# же три машины трижды: превью и витрина сортируют одно и то же по выгоде.
# Память процесса, 30 минут: пережил рестарт — просто покажем сначала.
SEEN_TTL_SECONDS = 1800
_seen_lock = threading.Lock()
_seen: dict[int, tuple[float, set[int]]] = {}


def mark_seen(chat_id: int, listing_ids) -> None:
    now = time.monotonic()
    with _seen_lock:
        for key in [key for key, (until, _) in _seen.items() if until < now]:
            del _seen[key]
        _, current = _seen.get(chat_id, (0.0, set()))
        _seen[chat_id] = (now + SEEN_TTL_SECONDS, current | set(listing_ids))


def seen_ids(chat_id: int) -> set[int]:
    with _seen_lock:
        until, ids = _seen.get(chat_id, (0.0, set()))
        return set(ids) if until >= time.monotonic() else set()


def first_unseen_offset(items: list[PendingNotification], chat_id: int) -> int:
    """С какой позиции витрины начинать для чата: первая ещё не показанная.

    Всё уже показано — сначала (витрина короткая, 12 позиций).
    """
    seen = seen_ids(chat_id)
    for index, item in enumerate(items):
        if item.listing_id not in seen:
            return index
    return 0


def market_averages(
    session: Session, listings, *, min_samples: int
) -> dict[Listing, float | None]:
    """`market_average` для пачки объявлений (см. `market_estimates`)."""
    return {
        listing: estimate.avg_usd if estimate is not None else None
        for listing, estimate in market_estimates(
            session, listings, min_samples=min_samples
        ).items()
    }


def market_estimates(
    session: Session, listings, *, min_samples: int, basis: str = BASIS_AVG
) -> dict[Listing, MarketEstimate | None]:
    """`market_estimate` для пачки объявлений с кэшем по когорте.

    Когорта = марка+модель+год+бакет пробега (NULL-пробег — своя ветка, как в
    самой `market_estimate`). Свежих за сутки ~2500, когорт среди них заметно
    меньше, а каждая — два запроса. Зарубежные отбрасываются: средней для них
    нет по построению.
    """
    avg_by_cohort: dict[tuple, MarketEstimate | None] = {}
    result: dict[Listing, MarketEstimate | None] = {}
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
            avg_by_cohort[cohort] = market_estimate(
                session, listing, min_samples=min_samples, basis=basis
            )
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
    model_names = load_model_names(session)

    scored: list[tuple[float, Listing, MarketEstimate]] = []
    estimates = market_estimates(session, candidates, min_samples=min_samples)
    for listing, estimate in estimates.items():
        if estimate is None or estimate.avg_usd <= 0:
            continue
        percent = round((1 - float(listing.current_price_usd) / estimate.avg_usd) * 100, 1)
        if percent > 0 and is_plausible_discount(percent, max_below_percent):
            scored.append((percent, listing, estimate))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        build_pending(
            listing,
            chat_id=0,
            filter_id=0,  # витрина не привязана к фильтру
            below_percent=percent,
            estimate=estimate,
            source_names=source_names,
            brand_names=brand_names,
            model_names=model_names,
        )
        for percent, listing, estimate in scored[:size]
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
