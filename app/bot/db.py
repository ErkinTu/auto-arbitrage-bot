"""Sync-хелперы БД для бота.

Каждая функция открывает собственную SessionLocal и возвращает простые DTO,
а не ORM-объекты: вызывающий код (aiogram) работает через asyncio.to_thread,
и сессия не должна пересекать границу потока.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import BotUser, Brand, CarModel, UserFilter


class FilterLimitError(Exception):
    """У пользователя уже максимум фильтров (bot_max_filters_per_user)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"filter limit reached: {limit}")


@dataclass(slots=True)
class BrandDTO:
    id: int
    name: str


@dataclass(slots=True)
class ModelDTO:
    id: int
    name: str


@dataclass(slots=True)
class FilterDTO:
    id: int
    brand_name: str | None  # None = любая марка
    model_name: str | None
    year_from: int | None
    year_to: int | None
    mileage_max_km: int | None
    price_max_usd: float | None
    below_market_only: bool
    below_market_percent: int | None
    skip_foreign: bool
    region: str | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_or_create_bot_user(telegram_id: int, username: str | None, first_name: str | None) -> int:
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            user = BotUser(telegram_id=telegram_id, username=username, first_name=first_name)
            session.add(user)
        else:
            user.username = username
            user.first_name = first_name
            # Пользователь снова пишет боту: значит не заблокировал и доступ проверен.
            user.is_active = True
            if user.access_status == "revoked":
                user.access_status = "member"
        user.last_access_check_at = _utcnow()
        session.commit()
        return user.id


def list_brands() -> list[BrandDTO]:
    allowed = get_settings().allowed_brand_list
    with SessionLocal() as session:
        brands = session.scalars(
            select(Brand).where(Brand.name.in_(allowed)).order_by(Brand.name)
        ).all()
        return [BrandDTO(id=brand.id, name=brand.name) for brand in brands]


def list_models(brand_id: int) -> list[ModelDTO]:
    with SessionLocal() as session:
        models = session.scalars(
            select(CarModel).where(CarModel.brand_id == brand_id).order_by(CarModel.name)
        ).all()
        return [ModelDTO(id=model.id, name=model.name) for model in models]


def _filter_dto(session, user_filter: UserFilter) -> FilterDTO:
    brand = session.get(Brand, user_filter.brand_id) if user_filter.brand_id else None
    model = session.get(CarModel, user_filter.car_model_id) if user_filter.car_model_id else None
    return FilterDTO(
        id=user_filter.id,
        brand_name=brand.name if brand else None,
        model_name=model.name if model else None,
        year_from=user_filter.year_from,
        year_to=user_filter.year_to,
        mileage_max_km=user_filter.mileage_max_km,
        price_max_usd=float(user_filter.price_max_usd)
        if user_filter.price_max_usd is not None
        else None,
        below_market_only=user_filter.below_market_only,
        below_market_percent=user_filter.below_market_percent,
        skip_foreign=user_filter.skip_foreign,
        region=user_filter.region,
    )


def count_filters(telegram_id: int) -> int:
    """Число активных фильтров пользователя (0, если юзера ещё нет)."""
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            return 0
        return session.scalar(
            select(func.count())
            .select_from(UserFilter)
            .where(UserFilter.user_id == user.id, UserFilter.enabled.is_(True))
        ) or 0


def list_filters(telegram_id: int) -> list[FilterDTO]:
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            return []
        filters = session.scalars(
            select(UserFilter)
            .where(UserFilter.user_id == user.id, UserFilter.enabled.is_(True))
            .order_by(UserFilter.id)
        ).all()
        return [_filter_dto(session, item) for item in filters]


def create_filter(
    telegram_id: int,
    *,
    brand_id: int | None,
    car_model_id: int | None,
    year_from: int | None,
    year_to: int | None,
    mileage_max_km: int | None,
    price_max_usd: float | None,
    below_market_only: bool,
    below_market_percent: int | None = None,
    skip_foreign: bool = False,
    region: str | None = None,
) -> FilterDTO:
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            raise LookupError(f"bot user {telegram_id} is not registered")
        limit = get_settings().bot_max_filters_per_user
        current = session.scalar(
            select(func.count())
            .select_from(UserFilter)
            .where(UserFilter.user_id == user.id, UserFilter.enabled.is_(True))
        ) or 0
        if current >= limit:
            raise FilterLimitError(limit)
        user_filter = UserFilter(
            user_id=user.id,
            brand_id=brand_id,
            car_model_id=car_model_id,
            year_from=year_from,
            year_to=year_to,
            mileage_max_km=mileage_max_km,
            price_max_usd=Decimal(str(price_max_usd)) if price_max_usd is not None else None,
            below_market_only=below_market_only,
            below_market_percent=below_market_percent,
            skip_foreign=skip_foreign,
            region=region,
        )
        session.add(user_filter)
        session.commit()
        return _filter_dto(session, user_filter)


def delete_filter(telegram_id: int, filter_id: int) -> bool:
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            return False
        user_filter = session.scalar(
            select(UserFilter).where(UserFilter.id == filter_id, UserFilter.user_id == user.id)
        )
        if user_filter is None:
            return False
        session.delete(user_filter)
        session.commit()
        return True


def mark_user_blocked(telegram_id: int) -> None:
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is not None:
            user.is_active = False
            session.commit()
