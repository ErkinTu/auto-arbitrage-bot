"""Витрина «лучшие сейчас»: самые дешёвые относительно рынка за последние сутки.

Считается живьём и кэшируется на SHOWCASE_TTL_SECONDS, а не хранится таблицей:
объявления протухают за часы, и сохранённая «десятка» через полдня вела бы на
мёртвые страницы. Берём только активные — снятое валидатором или full-scan-ом
пропадает из витрины со следующим пересчётом.

Чистая sync-логика без aiogram: зовут бот (`/best`, витрина в `/start` — через
asyncio.to_thread) и worker (итоги триала с пустым результатом).

Правки по проверке владельца 01.11.2026 («снятые, устаревшие, без фото, повторы»):
- **свежесть — по номеру объявления**, а не только по дате источника: lalafo
  (и иногда mashina) при переподаче оставляет старый номер, а дату создания
  ставит заново — почти половина «свежих за сутки» у lalafo была старыми
  объявлениями. Свежее = номер больше самого нового, что мы знали сутки назад
  (`fresh_id_floors`, калибруется по нашей же базе);
- машины не старше `BOT_SHOWCASE_MAX_AGE_YEARS` (у старых средняя шумная);
- только с настоящим фото (заглушка mashina — не фото, `images.real_photo`);
- повторы (та же машина на mashina и lalafo) склеиваются;
- при прогреве из цикла бота позиции проверяются на источнике (`verify`),
  снятое сразу деактивируется и выпадает; вердикт живёт `VERIFY_TTL_SECONDS`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

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
from app.services.images import real_photo
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
# Пул кандидатов на проверку у источника: часть топа может оказаться снятой.
SHOWCASE_POOL = SHOWCASE_SIZE * 2
# Сколько последних «впервые увиденных до отсечки» объявлений смотреть, чтобы
# найти самый новый номер на тот момент (среди них есть и переподанные старые —
# берём максимум).
ID_FLOOR_SAMPLE = 500
# Вердикт проверки у источника: снятое не перепроверяем, живое — раз в 30 минут.
VERIFY_TTL_SECONDS = 1800
_verdicts: dict[int, tuple[float, bool]] = {}

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


def fresh_id_floors(session: Session, cutoff: datetime) -> dict[int, int]:
    """Источник → самый новый номер объявления, известный нам к `cutoff`.

    Номера у mashina и lalafo растут со временем, поэтому всё, что новее этого
    номера, действительно создано после отсечки, а переподанное старое — нет,
    какую бы дату создания ни показывал источник. Нет истории — нет порога.
    """
    floors: dict[int, int] = {}
    for source_id in session.scalars(select(Source.id)).all():
        ids = session.scalars(
            select(Listing.external_id)
            .where(Listing.source_id == source_id, Listing.first_seen_at < cutoff)
            .order_by(Listing.first_seen_at.desc())
            .limit(ID_FLOOR_SAMPLE)
        ).all()
        numbers = [int(value) for value in ids if value and value.isdigit()]
        if numbers:
            floors[source_id] = max(numbers)
    return floors


def is_fresh_id(listing: Listing, floors: dict[int, int]) -> bool:
    floor = floors.get(listing.source_id)
    external_id = listing.external_id or ""
    if floor is None or not external_id.isdigit():
        return True  # порога нет — судить не по чему
    return int(external_id) > floor


def _duplicate_key(listing: Listing) -> tuple:
    """Та же машина на двух источниках (или переподана продавцом)."""
    return (
        listing.brand_id,
        listing.car_model_id,
        listing.year,
        listing.mileage_km,
        round(float(listing.current_price_usd)),
    )


def compute_showcase(
    session: Session,
    *,
    min_samples: int,
    max_below_percent: float = 60.0,
    window_hours: int = SHOWCASE_WINDOW_HOURS,
    size: int = SHOWCASE_SIZE,
    max_age_years: int = 20,
) -> list[PendingNotification]:
    """Лучшие по % ниже рынка среди созданных за `window_hours` (chat_id=0).

    `market_average` — та же, что у рассылки: витрина показывает ровно то, что
    человек получал бы по фильтру «ниже рынка». Только полные данные
    (`is_complete_listing`), правдоподобная скидка, настоящее фото, свежий
    номер и не слишком старая машина: витрина — лицо бота.
    """
    now = utcnow()
    published_after = now - timedelta(hours=window_hours)
    query = select(Listing).where(
        Listing.is_active.is_(True),
        Listing.analytics_eligible.is_(True),
        Listing.current_price_usd.is_not(None),
        Listing.current_price_usd >= MIN_PLAUSIBLE_PRICE_USD,
        Listing.source_created_at >= published_after,
        Listing.brand_id.is_not(None),
        Listing.car_model_id.is_not(None),
        Listing.year.is_not(None),
        Listing.mileage_km.is_not(None),
        Listing.image_url.is_not(None),
    )
    if max_age_years > 0:
        query = query.where(Listing.year >= now.year - max_age_years)
    floors = fresh_id_floors(session, published_after)
    candidates = [
        listing
        for listing in session.scalars(query).all()
        if is_complete_listing(listing, now)
        and real_photo(listing.image_url)
        and is_fresh_id(listing, floors)
    ]
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
    unique: list[tuple[float, Listing, MarketEstimate]] = []
    keys: set[tuple] = set()
    for item in scored:
        key = _duplicate_key(item[1])
        if key not in keys:
            keys.add(key)
            unique.append(item)
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
        for percent, listing, estimate in unique[:size]
    ]


def verified(
    items: list[PendingNotification],
    verify: Callable[[PendingNotification], bool | None] | None,
    size: int,
) -> list[PendingNotification]:
    """Первые `size` позиций, не снятых на источнике.

    `verify` — живая проверка (True — живо, False — снято, None — не удалось
    проверить: оставляем). Без `verify` (холодный `/best`) — только уже
    известные вердикты: проверять 12 объявлений, пока человек ждёт, долго.
    """
    now = time.monotonic()
    result: list[PendingNotification] = []
    for item in items:
        if len(result) >= size:
            break
        cached = _verdicts.get(item.listing_id)
        if cached is not None and (not cached[1] or now - cached[0] < VERIFY_TTL_SECONDS):
            alive = cached[1]  # снятое не оживает — без срока
        elif verify is None:
            alive = True
        else:
            verdict = verify(item)
            if verdict is not None:
                _verdicts[item.listing_id] = (now, verdict)
            alive = verdict is not False
        if alive:
            result.append(item)
    if len(_verdicts) > 5000:  # память процесса не растёт вечно
        for key in [k for k, (at, _) in _verdicts.items() if now - at > VERIFY_TTL_SECONDS]:
            _verdicts.pop(key, None)
    return result


def get_showcase(
    session_factory,
    *,
    min_samples: int,
    max_below_percent: float = 60.0,
    max_age_years: int = 20,
    refresh: bool = False,
    verify: Callable[[PendingNotification], bool | None] | None = None,
) -> list[PendingNotification]:
    """Витрина из кэша процесса; пересчёт не чаще раза в SHOWCASE_TTL_SECONDS.

    `refresh=True` — пересчитать сейчас (прогрев из цикла бота после парса);
    там же `verify` — проверка позиций у источника. Под замком: два
    одновременных `/best` после истечения кэша не должны считать одно и то же
    дважды — второй дождётся первого и возьмёт готовое.
    """
    global _cache
    with _lock:
        cached_until, cached = _cache
        if not refresh and time.monotonic() < cached_until:
            return verified(cached, None, SHOWCASE_SIZE)  # снятое за это время — вон
        with session_factory() as session:
            pool = compute_showcase(
                session,
                min_samples=min_samples,
                max_below_percent=max_below_percent,
                max_age_years=max_age_years,
                size=SHOWCASE_POOL,
            )
        fresh = verified(pool, verify, SHOWCASE_SIZE)
        _cache = (time.monotonic() + SHOWCASE_TTL_SECONDS, fresh)
        return fresh


def page_for_chat(
    items: list[PendingNotification], chat_id: int, offset: int, page: int = SHOWCASE_PAGE
) -> tuple[list[PendingNotification], int | None]:
    """Страница витрины для чата и offset следующей (None — дальше пусто)."""
    chunk = [replace(item, chat_id=chat_id) for item in items[offset : offset + page]]
    next_offset = offset + page
    return chunk, (next_offset if next_offset < len(items) else None)
