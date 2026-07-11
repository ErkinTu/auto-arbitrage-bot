"""Чистая sync-логика подбора новых объявлений под фильтры пользователей.

Не зависит от aiogram: принимает Session, возвращает DTO. Вызывается poller-ом
через asyncio.to_thread и напрямую из тестов.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    BotUser,
    DailyMarketStat,
    Listing,
    ListingStatusHistory,
    SentNotification,
    UserFilter,
)
from app.services.stats import mileage_bucket
from app.services.vehicle_summary import _build_source_url as build_source_url


@dataclass(slots=True)
class PendingNotification:
    chat_id: int
    listing_id: int
    filter_id: int
    title: str | None
    year: int | None
    mileage_km: int | None
    price_usd: float | None
    city: str | None
    url: str
    below_market_percent: float | None


def find_new_listings(session: Session, since: datetime) -> list[Listing]:
    """Активные объявления с событием new не старше since и известной ценой."""
    # DISTINCT по id в подзапросе: DISTINCT по всем колонкам Listing невозможен
    # в Postgres из-за JSON-колонки quality_flags (нет оператора равенства).
    new_listing_ids = (
        select(ListingStatusHistory.listing_id)
        .where(
            ListingStatusHistory.event_type == "new",
            ListingStatusHistory.observed_at >= since,
        )
        .distinct()
    )
    return list(
        session.scalars(
            select(Listing).where(
                Listing.id.in_(new_listing_ids),
                Listing.is_active.is_(True),
                Listing.current_price_usd.is_not(None),
            )
        ).all()
    )


def filter_matches(listing: Listing, user_filter: UserFilter) -> bool:
    """Проверка объявления против одного фильтра. NULL-поле фильтра = любое значение.

    Если фильтр задает границу, а у объявления поле неизвестно, считаем не-матчем:
    push-уведомление по неполным данным хуже, чем тишина.
    """
    if listing.brand_id != user_filter.brand_id:
        return False
    if user_filter.car_model_id is not None and listing.car_model_id != user_filter.car_model_id:
        return False
    if user_filter.year_from is not None and (listing.year is None or listing.year < user_filter.year_from):
        return False
    if user_filter.year_to is not None and (listing.year is None or listing.year > user_filter.year_to):
        return False
    if user_filter.mileage_max_km is not None and (
        listing.mileage_km is None or listing.mileage_km > user_filter.mileage_max_km
    ):
        return False
    if user_filter.price_max_usd is not None and (
        listing.current_price_usd is None or listing.current_price_usd > user_filter.price_max_usd
    ):
        return False
    return True


def is_below_market(
    session: Session,
    listing: Listing,
    *,
    factor: float,
    min_samples: int,
) -> tuple[bool, float] | None:
    """Сравнение цены объявления со средней по рынку из daily_market_stats.

    Возвращает (matched, средняя_цена) или None, когда статистики недостаточно
    для уверенного вывода (мало образцов, нет рядов, неполные данные объявления).
    """
    if (
        listing.brand_id is None
        or listing.car_model_id is None
        or listing.year is None
        or listing.current_price_usd is None
    ):
        return None

    conditions = [
        DailyMarketStat.brand_id == listing.brand_id,
        DailyMarketStat.car_model_id == listing.car_model_id,
        DailyMarketStat.year == listing.year,
        DailyMarketStat.mileage_bucket == mileage_bucket(listing.mileage_km),
    ]
    # Растаможка существенно сдвигает цены: сравниваем только внутри своей группы.
    if listing.customs_status != "unknown":
        conditions.append(DailyMarketStat.customs_status == listing.customs_status)

    latest_date = session.scalar(select(func.max(DailyMarketStat.stat_date)).where(*conditions))
    if latest_date is None:
        return None

    rows = session.scalars(
        select(DailyMarketStat).where(*conditions, DailyMarketStat.stat_date == latest_date)
    ).all()
    total_samples = sum(row.sample_count for row in rows)
    if total_samples < min_samples:
        return None

    # Взвешенное по числу образцов среднее по поколениям (и группам растаможки при unknown).
    weighted_avg = sum(float(row.avg_price_usd) * row.sample_count for row in rows) / total_samples
    price = float(listing.current_price_usd)
    return price <= weighted_avg * factor, weighted_avg


def match_new_listings(
    session: Session,
    since: datetime,
    *,
    factor: float,
    min_samples: int,
) -> list[PendingNotification]:
    """Подбирает пары (пользователь, новое объявление), фиксирует отправку, возвращает DTO.

    Строки sent_notifications коммитятся ДО фактической отправки в Telegram:
    при падении между коммитом и отправкой теряется максимум одно уведомление,
    но дубль не возникает никогда.
    """
    listings = find_new_listings(session, since)
    if not listings:
        return []

    filter_user_pairs = session.execute(
        select(UserFilter, BotUser)
        .join(BotUser, UserFilter.user_id == BotUser.id)
        .where(
            UserFilter.enabled.is_(True),
            BotUser.is_active.is_(True),
            BotUser.access_status != "revoked",
        )
    ).all()
    if not filter_user_pairs:
        return []

    listing_ids = [listing.id for listing in listings]
    already_sent: set[tuple[int, int]] = {
        (row.user_id, row.listing_id)
        for row in session.execute(
            select(SentNotification.user_id, SentNotification.listing_id).where(
                SentNotification.listing_id.in_(listing_ids)
            )
        )
    }

    below_market_cache: dict[int, tuple[bool, float] | None] = {}
    pending: list[PendingNotification] = []

    for listing in listings:
        for user_filter, user in filter_user_pairs:
            key = (user.id, listing.id)
            if key in already_sent:
                continue
            if not filter_matches(listing, user_filter):
                continue

            if listing.id not in below_market_cache:
                below_market_cache[listing.id] = is_below_market(
                    session, listing, factor=factor, min_samples=min_samples
                )
            below = below_market_cache[listing.id]
            # Тумблер "только ниже рынка": отсутствие статистики = не матч.
            if user_filter.below_market_only and (below is None or not below[0]):
                continue

            below_percent: float | None = None
            if below is not None and below[0] and below[1] > 0:
                below_percent = round((1 - float(listing.current_price_usd) / below[1]) * 100, 1)

            already_sent.add(key)
            session.add(
                SentNotification(user_id=user.id, listing_id=listing.id, filter_id=user_filter.id)
            )
            pending.append(
                PendingNotification(
                    chat_id=user.telegram_id,
                    listing_id=listing.id,
                    filter_id=user_filter.id,
                    title=listing.title,
                    year=listing.year,
                    mileage_km=listing.mileage_km,
                    price_usd=float(listing.current_price_usd)
                    if listing.current_price_usd is not None
                    else None,
                    city=listing.city,
                    url=build_source_url(listing),
                    below_market_percent=below_percent,
                )
            )

    session.commit()
    return pending
