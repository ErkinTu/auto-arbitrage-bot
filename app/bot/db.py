"""Sync-хелперы БД для бота.

Каждая функция открывает собственную SessionLocal и возвращает простые DTO,
а не ORM-объекты: вызывающий код (aiogram) работает через asyncio.to_thread,
и сессия не должна пересекать границу потока.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.bot.matching import PendingNotification, market_average
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import BotUser, Brand, CarModel, Listing, Payment, Source, UserEvent, UserFilter
from app.services.vehicle_summary import _build_source_url as build_source_url
from app.services.events import EVENT_PAYMENT_CREATED, EVENT_START, log_event
from app.services.referral import (
    ReferralStats,
    attach_referrer,
    grant_due_bonuses,
    parse_deep_link,
    referral_stats,
)
from app.services.subscription import is_subscription_active
from app.services.trial import is_trial_active, start_trial, trial_used


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


def decide_access_status(
    is_member: bool | None,
    active_sub: bool,
    current: str = "revoked",
    active_trial: bool = False,
) -> tuple[str, bool]:
    """Целевой access_status и признак доступа.

    Три источника доступа, по убыванию приоритета: член группы (суперюзер) →
    активная платная подписка → активный триал. Приоритет важен не для доступа
    (он всё равно есть), а для честности статуса: по нему считается, кого
    догонять пейволом и кому слать напоминание о продлении.

    is_member=None — «проверить не удалось» (Telegram недоступен, бот выкинут из
    группы, неверный chat_id). Понижать статус в этом случае нельзя: matching.py
    шлёт только 'member'/активным 'subscriber'/'trial', поэтому один сбой
    get_chat_member молча отписал бы живого юзера от рассылки до его следующего
    сообщения боту. Держим прежний статус и пускаем по нему.
    """
    if is_member is None:
        return current, current == "member" or active_sub or active_trial
    if is_member:
        return "member", True
    if active_sub:
        return "subscriber", True
    if active_trial:
        return "trial", True
    return "revoked", False


def resolve_access(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    is_member: bool | None,
) -> bool:
    """Апсертит пользователя, синхронизирует access_status и возвращает доступ.

    access_status держим правдивым для matching.py (тот не может звать Telegram):
    член группы → 'member' (суперюзер), не член но с активной подпиской →
    'subscriber', иначе → 'revoked'. Пишем в БД только при изменении статуса/
    идентити — у болтливых юзеров это чаще всего no-op на чтение. Подписку читаем
    свежей (дёшево), чтобы доступ открывался сразу после оплаты (webhook уже
    выставил 'subscriber').
    """
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        created = user is None
        if created:
            user = BotUser(telegram_id=telegram_id, username=username, first_name=first_name)
            session.add(user)

        # У свежедобавленного user access_status ещё None (column default ставится
        # на INSERT) — для новичка «прежний статус» = отсутствие доступа.
        desired, has_access = decide_access_status(
            is_member,
            is_subscription_active(user),
            user.access_status or "revoked",
            is_trial_active(user),
        )

        changed = (
            created
            or user.access_status != desired
            or user.username != username
            or user.first_name != first_name
            or not user.is_active
        )
        if changed:
            user.username = username
            user.first_name = first_name
            user.is_active = True
            user.access_status = desired
            user.last_access_check_at = _utcnow()
            session.commit()
        return has_access


@dataclass(slots=True)
class SubscriptionInfo:
    access_status: str
    expires_at: datetime | None
    active: bool
    trial_active: bool = False
    trial_expires_at: datetime | None = None
    trial_used: bool = False


def get_subscription_info(telegram_id: int) -> SubscriptionInfo | None:
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            return None
        return SubscriptionInfo(
            access_status=user.access_status,
            expires_at=user.subscription_expires_at,
            active=is_subscription_active(user),
            trial_active=is_trial_active(user),
            trial_expires_at=user.trial_expires_at,
            trial_used=trial_used(user),
        )


def create_pending_payment(
    telegram_id: int, *, amount: Decimal, currency: str, period_days: int
) -> str:
    """Создаёт pending-платёж, возвращает order_id (уходит в Finik как PaymentId)."""
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            raise LookupError(f"bot user {telegram_id} is not registered")
        order_id = str(uuid.uuid4())  # Finik ждёт UUID (с дефисами), не hex
        session.add(
            Payment(
                user_id=user.id,
                order_id=order_id,
                amount=amount,
                currency=currency,
                period_days=period_days,
                status="pending",
            )
        )
        log_event(session, user.id, EVENT_PAYMENT_CREATED, {"order_id": order_id})
        session.commit()
        return order_id


# --------------------------------------------------------------------------
# Онбординг, триал, рефералка
# --------------------------------------------------------------------------


@dataclass(slots=True)
class StartInfo:
    """Что нужно /start-у, чтобы выбрать ветку онбординга."""

    access_status: str
    trial_used: bool
    trial_active: bool
    trial_expires_at: datetime | None
    subscription_active: bool
    referrer_attached: bool  # пришёл по чужой ссылке, и она засчиталась
    first_start: bool


@dataclass(slots=True)
class TrialActivation:
    ok: bool  # триал только что выдан
    expires_at: datetime | None = None
    already_used: bool = False
    # Кого поздравить с бонусом (реферер получает дни в этот же момент).
    referrer_chat_id: int | None = None
    referrer_bonus_days: int = 0


@dataclass(slots=True)
class InviteInfo:
    invite_code: str
    invited_total: int
    qualified: int
    bonuses_granted: int
    friends_to_next_bonus: int


def handle_start(
    telegram_id: int,
    *,
    username: str | None,
    first_name: str | None,
    deep_link_payload: str | None = None,
) -> StartInfo:
    """Регистрирует /start: событие воронки + привязка реферера.

    Пользователь к этому моменту уже создан AccessMiddleware (`resolve_access`),
    поэтому здесь только дополняем. Событие `start` пишем ОДИН раз на юзера —
    метрика «сколько стартовало бота» считает людей, а не нажатия.
    """
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:  # гейт выключен/юзер удалён между апдейтами — создаём сами
            user = BotUser(telegram_id=telegram_id, username=username, first_name=first_name)
            session.add(user)
            session.flush()

        first_start = (
            session.scalar(
                select(UserEvent.id)
                .where(UserEvent.user_id == user.id, UserEvent.event == EVENT_START)
                .limit(1)
            )
            is None
        )
        if first_start:
            log_event(session, user.id, EVENT_START)

        referrer_attached = False
        code = parse_deep_link(deep_link_payload)
        if code and get_settings().referral_enabled:
            referrer_attached = attach_referrer(session, user, code) is not None

        info = StartInfo(
            access_status=user.access_status,
            trial_used=trial_used(user),
            trial_active=is_trial_active(user),
            trial_expires_at=user.trial_expires_at,
            subscription_active=is_subscription_active(user),
            referrer_attached=referrer_attached,
            first_start=first_start,
        )
        session.commit()
        return info


def activate_trial(telegram_id: int) -> TrialActivation:
    """Выдаёт триал и сразу догоняет бонус пригласившему.

    Бонус начисляется именно здесь, а не по клику по ссылке: клик бесплатен, а
    активация триала одноразова (см. `services/referral.py`).
    """
    settings = get_settings()
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            raise LookupError(f"bot user {telegram_id} is not registered")
        expires = start_trial(session, user, days=settings.trial_days)
        if expires is None:
            return TrialActivation(ok=False, already_used=True)

        result = TrialActivation(ok=True, expires_at=expires)
        if user.referrer_id is not None and settings.referral_enabled:
            referrer = session.get(BotUser, user.referrer_id)
            if referrer is not None:
                bonuses = grant_due_bonuses(
                    session,
                    referrer,
                    friends_per_bonus=settings.referral_friends_per_bonus,
                    bonus_days=settings.referral_bonus_days,
                )
                if bonuses:
                    result.referrer_chat_id = referrer.telegram_id
                    result.referrer_bonus_days = bonuses * settings.referral_bonus_days
        session.commit()
        return result


def get_invite_info(telegram_id: int) -> InviteInfo:
    settings = get_settings()
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            raise LookupError(f"bot user {telegram_id} is not registered")
        stats: ReferralStats = referral_stats(
            session, user, friends_per_bonus=settings.referral_friends_per_bonus
        )
        session.commit()  # ensure_invite_code мог выдать код
        return InviteInfo(
            invite_code=stats.invite_code,
            invited_total=stats.invited_total,
            qualified=stats.qualified,
            bonuses_granted=stats.bonuses_granted,
            friends_to_next_bonus=stats.friends_to_next_bonus,
        )


# Демо-лот для онбординга: настоящий, но пересчитывать его на каждый /start
# дорого (market_average — два запроса на объявление), а меняется он медленно.
DEMO_CANDIDATES = 40
DEMO_CACHE_TTL_SECONDS = 900
DEMO_GOOD_ENOUGH_PERCENT = 20.0  # нашли такое — дальше не ищем
_demo_cache: tuple[float, PendingNotification | None] = (0.0, None)


def get_demo_listing(chat_id: int) -> PendingNotification | None:
    """Живой пример «ниже рынка» для онбординга (не хардкод).

    Берём свежие объявления и считаем по ним ту же `market_average`, что и
    рассылка, — демо показывает ровно то, что человек будет получать. Результат
    кэшируется на DEMO_CACHE_TTL_SECONDS: /start редкий, но выдавать по 80
    запросов на нажатие незачем.
    """
    global _demo_cache
    cached_until, cached = _demo_cache
    if time.monotonic() < cached_until and cached is not None:
        return replace(cached, chat_id=chat_id)

    settings = get_settings()
    best: PendingNotification | None = None
    best_percent = 0.0
    with SessionLocal() as session:
        candidates = session.scalars(
            select(Listing)
            .where(
                Listing.is_active.is_(True),
                Listing.current_price_usd.is_not(None),
                Listing.analytics_eligible.is_(True),
                Listing.source_created_at.is_not(None),
            )
            .order_by(Listing.source_created_at.desc())
            .limit(DEMO_CANDIDATES)
        ).all()
        source_names = {row.id: row.name for row in session.execute(select(Source.id, Source.name))}
        for listing in candidates:
            avg = market_average(session, listing, min_samples=settings.bot_below_avg_min_samples)
            if avg is None or avg <= 0:
                continue
            percent = round((1 - float(listing.current_price_usd) / avg) * 100, 1)
            if percent <= best_percent:
                continue
            best_percent = percent
            best = PendingNotification(
                chat_id=chat_id,
                listing_id=listing.id,
                filter_id=0,  # демо не привязано к фильтру
                title=listing.title,
                year=listing.year,
                mileage_km=listing.mileage_km,
                price_usd=float(listing.current_price_usd),
                city=listing.city,
                url=build_source_url(listing),
                below_market_percent=percent,
                market_avg_usd=avg,
                source_name=source_names.get(listing.source_id, "mashina.kg"),
            )
            if percent >= DEMO_GOOD_ENOUGH_PERCENT:
                break

    _demo_cache = (time.monotonic() + DEMO_CACHE_TTL_SECONDS, best)
    return best


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
