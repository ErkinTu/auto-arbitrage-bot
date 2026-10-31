"""Sync-хелперы БД для бота.

Каждая функция открывает собственную SessionLocal и возвращает простые DTO,
а не ORM-объекты: вызывающий код (aiogram) работает через asyncio.to_thread,
и сессия не должна пересекать границу потока.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, TypeVar

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.bot.digest import DIGEST_PAGE, DIGEST_TTL_MINUTES, batch_age_minutes, order_digest
from app.bot.matching import PendingNotification, build_pending, load_model_names, market_estimate
from app.bot.matching import preview_for_filter as _preview_for_filter
from app.bot.showcase import get_showcase as _get_showcase
from app.bot.showcase import first_unseen_offset
from app.bot.showcase import page_for_chat as showcase_page
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import (
    AdminLoginToken,
    BotUser,
    Brand,
    CarModel,
    Listing,
    Payment,
    SentNotification,
    Source,
    UserBlock,
    UserEvent,
    UserFilter,
)
from app.services.blocklist import (
    KIND_BRAND,
    KIND_CITY,
    KIND_MODEL,
    MAX_BLOCKS_PER_USER,
    block_value,
)
from app.services.access_schedule import (
    ScheduleEntry,
    exceeds_horizon,
    full_speed_starts_at,
    schedule_for_user,
    tail_end,
)
from app.services.events import EVENT_PAYMENT_CREATED, EVENT_START, log_event
from app.services.filter_presets import base_preset, find_preset
from app.services.plans import TIER_FULL, effective_tier, find_plan
from app.services.referral import (
    ReferralStats,
    attach_referrer,
    grant_due_bonuses,
    parse_deep_link,
    referral_stats,
)
from app.services.subscription import is_subscription_active
from app.services.trial import is_trial_active, start_trial, trial_used

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _retry_on_user_insert_race(action: Callable[[], _T]) -> _T:
    """Повторяет операцию, если строку пользователя параллельно вставил сосед.

    Апдейты одного и того же НОВОГО юзера приходят пачкой (Telegram отдаёт
    накопленное сразу после перезапуска бота), обрабатываются параллельно в
    разных потоках, оба видят «юзера нет» и оба делают INSERT. Второй падает на
    уникальном `ix_bot_users_telegram_id`, aiogram роняет апдейт целиком — и
    человек, впервые написавший боту, не получает ответа. Ловили на проде
    08.08.2026 на первом же новом пользователе.

    Одной повторной попытки достаточно: к этому моменту строка соседа уже
    закоммичена, и повтор идёт по ветке «пользователь найден».
    """
    try:
        return action()
    except IntegrityError:
        logger.info("bot user insert race detected, retrying once")
        return action()


class FilterLimitError(Exception):
    """У пользователя уже максимум фильтров (bot_max_filters_per_user).

    Несёт и `used`: лимит снижали с 10 до 5, поэтому «7 из 5» — законное
    состояние, и сообщение о нём должно отличаться от «5 из 5».
    """

    def __init__(self, limit: int, used: int = 0) -> None:
        self.limit = limit
        self.used = used
        super().__init__(f"filter limit reached: {used}/{limit}")


class DuplicateFilterError(Exception):
    """Такой же фильтр у пользователя уже есть.

    При лимите 5 слот стоит дорого: молча создать копию — значит списать слот
    ни за что (дублей в рассылке не будет, sent_notifications дедуплицирует по
    паре юзер+объявление).
    """

    def __init__(self, filter_id: int) -> None:
        self.filter_id = filter_id
        super().__init__(f"duplicate of filter {filter_id}")


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
    brand_group: str | None = None
    max_age_years: int | None = None


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
    """Апсерт пользователя с защитой от гонки вставки (см. `_retry_on_user_insert_race`)."""
    return _retry_on_user_insert_race(
        lambda: _resolve_access_once(telegram_id, username, first_name, is_member)
    )


def _resolve_access_once(
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
    # Скорость уведомлений прямо сейчас и расписание её изменений: у человека
    # может быть оплачено `[с отставанием до 30.08][полная до 29.09]`, и одной
    # датой это не рассказать — а рассказать обязательно, иначе купивший дорогой
    # тариф прочтёт первый тихий час как обман.
    tier: str = TIER_FULL
    schedule: tuple[ScheduleEntry, ...] = ()
    full_speed_from: datetime | None = None


def get_subscription_info(telegram_id: int) -> SubscriptionInfo | None:
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            return None
        now = _utcnow()
        entries = schedule_for_user(session, user.id, now)
        active_block_tier = next(
            (entry.tier for entry in entries if entry.starts_at <= now < entry.ends_at), None
        )
        return SubscriptionInfo(
            access_status=user.access_status,
            expires_at=user.subscription_expires_at,
            active=is_subscription_active(user, now),
            trial_active=is_trial_active(user, now),
            trial_expires_at=user.trial_expires_at,
            trial_used=trial_used(user),
            tier=effective_tier(user, active_block_tier, now),
            schedule=tuple(entries),
            full_speed_from=full_speed_starts_at(entries),
        )


@dataclass(slots=True)
class PaymentDraft:
    """Итог выбора плана: либо заведённый платёж с датами, либо отказ.

    Даты считаются ДО оплаты и одним расчётом с расписанием: человек должен
    увидеть «полная скорость с 30.08» на экране подтверждения, а не выяснять
    после списания, почему уведомления идут медленно.
    """

    ok: bool
    plan_code: str
    order_id: str | None = None
    # Когда начнутся купленные дни: now, если доступа нет, иначе — конец уже
    # оплаченного (блоки строго в хвост, понижения «прямо сейчас» не бывает).
    starts_at: datetime | None = None
    # Купленные дни начинаются прямо сейчас (доступа не было) — от этого зависит
    # весь текст подтверждения: «доступ до …» против «встанет следом с …».
    starts_now: bool = False
    access_until: datetime | None = None
    # Что оплачено сейчас — чтобы сказать «ничего не сгорает» предметно.
    schedule_before: tuple[ScheduleEntry, ...] = ()
    trial_expires_at: datetime | None = None
    # Заполнено ТОЛЬКО при отказе: доступ уже оплачен дальше горизонта.
    horizon_until: datetime | None = None


def create_pending_payment(telegram_id: int, *, plan_code: str) -> PaymentDraft:
    """Заводит pending-платёж по коду плана и возвращает даты для подтверждения.

    Сумма, срок и код плана берутся ИЗ РЕЕСТРА (`services/plans.py`), никогда из
    данных клиента: callback приходит из чата, где кнопку можно нажать и через
    неделю, и подменить в ней цену не должно быть возможности.

    Отказ по горизонту (`ok=False`) выдаётся ДО создания платежа — иначе вышло бы
    «деньги взяли, дни не встали»: горизонт мы всё равно не сдвинем дальше
    разрешённого, а возвратов у нас нет.
    """
    plan = find_plan(plan_code)
    if plan is None:  # защита: хендлер уже проверил код кнопки
        raise LookupError(f"unknown plan code {plan_code!r}")
    settings = get_settings()
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            raise LookupError(f"bot user {telegram_id} is not registered")
        now = _utcnow()
        if exceeds_horizon(
            user,
            plan.days,
            max_horizon_days=settings.subscription_max_horizon_days,
            now=now,
        ):
            return PaymentDraft(
                ok=False,
                plan_code=plan.code,
                horizon_until=user.subscription_expires_at,
                schedule_before=tuple(schedule_for_user(session, user.id, now)),
            )

        starts_at = tail_end(user, now)
        order_id = str(uuid.uuid4())  # Finik ждёт UUID (с дефисами), не hex
        session.add(
            Payment(
                user_id=user.id,
                order_id=order_id,
                amount=plan.price_kgs,
                currency="KGS",
                period_days=plan.days,
                plan_code=plan.code,
                status="pending",
            )
        )
        log_event(
            session,
            user.id,
            EVENT_PAYMENT_CREATED,
            {"order_id": order_id, "plan_code": plan.code, "tier": plan.tier},
        )
        draft = PaymentDraft(
            ok=True,
            plan_code=plan.code,
            order_id=order_id,
            starts_at=starts_at,
            starts_now=starts_at <= now,
            access_until=starts_at + timedelta(days=plan.days),
            schedule_before=tuple(schedule_for_user(session, user.id, now)),
            trial_expires_at=user.trial_expires_at if is_trial_active(user, now) else None,
        )
        session.commit()
        return draft


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
    limit_reached: bool = False


def handle_start(
    telegram_id: int,
    *,
    username: str | None,
    first_name: str | None,
    deep_link_payload: str | None = None,
) -> StartInfo:
    """Регистрация /start с защитой от гонки вставки (см. `_retry_on_user_insert_race`)."""
    return _retry_on_user_insert_race(
        lambda: _handle_start_once(
            telegram_id,
            username=username,
            first_name=first_name,
            deep_link_payload=deep_link_payload,
        )
    )


def _handle_start_once(
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
                    max_bonuses=settings.referral_max_bonuses,
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
            session,
            user,
            friends_per_bonus=settings.referral_friends_per_bonus,
            max_bonuses=settings.referral_max_bonuses,
        )
        session.commit()  # ensure_invite_code мог выдать код
        return InviteInfo(
            invite_code=stats.invite_code,
            invited_total=stats.invited_total,
            qualified=stats.qualified,
            bonuses_granted=stats.bonuses_granted,
            friends_to_next_bonus=stats.friends_to_next_bonus,
            limit_reached=stats.limit_reached,
        )


def get_showcase(
    chat_id: int, offset: int = 0, *, skip_seen: bool = False
) -> tuple[list[PendingNotification], int | None]:
    """Страница витрины «лучшие сейчас» для чата (см. `bot/showcase.py`).

    `skip_seen` — начать с первой машины, которую этому чату ещё не показывали
    (повторное «Лучшее сейчас» даёт новые, а не те же три).
    """
    settings = get_settings()
    items = _get_showcase(
        SessionLocal,
        min_samples=settings.bot_below_avg_min_samples,
        max_below_percent=settings.bot_max_below_market_percent,
    )
    if skip_seen:
        offset = first_unseen_offset(items, chat_id)
    return showcase_page(items, chat_id, offset)


def refresh_showcase() -> None:
    """Пересчёт витрины сейчас — прогрев из цикла бота (poller)."""
    settings = get_settings()
    _get_showcase(
        SessionLocal,
        min_samples=settings.bot_below_avg_min_samples,
        max_below_percent=settings.bot_max_below_market_percent,
        refresh=True,
    )


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


def _filter_signature(user_filter: UserFilter) -> tuple:
    """Значимые поля фильтра — для сравнения «это тот же самый фильтр».

    `enabled` и `id` не входят: сравниваем НАСТРОЙКУ, а не строку.
    """
    return (
        user_filter.brand_id,
        user_filter.car_model_id,
        user_filter.year_from,
        user_filter.year_to,
        user_filter.mileage_max_km,
        float(user_filter.price_max_usd) if user_filter.price_max_usd is not None else None,
        user_filter.below_market_only,
        user_filter.below_market_percent,
        user_filter.skip_foreign,
        user_filter.region,
        user_filter.brand_group,
        user_filter.max_age_years,
    )


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
        brand_group=user_filter.brand_group,
        max_age_years=user_filter.max_age_years,
    )


@dataclass(slots=True)
class FiltersQuota:
    used: int
    limit: int

    @property
    def can_add(self) -> bool:
        return self.used < self.limit

    @property
    def over_limit(self) -> bool:
        """Фильтров больше лимита — наследие прежнего лимита 10, не ошибка."""
        return self.used > self.limit

    @property
    def to_delete(self) -> int:
        """Сколько удалить, чтобы снова можно было добавлять."""
        return max(self.used - self.limit + 1, 0)


def _count_filters(session, user_id: int) -> int:
    return session.scalar(
        select(func.count())
        .select_from(UserFilter)
        .where(UserFilter.user_id == user_id, UserFilter.enabled.is_(True))
    ) or 0


def filters_quota(telegram_id: int) -> FiltersQuota:
    """Занято/можно фильтров (для незнакомого юзера — 0 из лимита)."""
    limit = get_settings().bot_max_filters_per_user
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        used = _count_filters(session, user.id) if user is not None else 0
        return FiltersQuota(used=used, limit=limit)


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
    brand_group: str | None = None,
    max_age_years: int | None = None,
) -> FilterDTO:
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            raise LookupError(f"bot user {telegram_id} is not registered")
        limit = get_settings().bot_max_filters_per_user
        current = _count_filters(session, user.id)
        if current >= limit:
            raise FilterLimitError(limit, current)
        existing = session.scalars(
            select(UserFilter).where(
                UserFilter.user_id == user.id, UserFilter.enabled.is_(True)
            )
        ).all()
        candidate = (
            brand_id,
            car_model_id,
            year_from,
            year_to,
            mileage_max_km,
            float(price_max_usd) if price_max_usd is not None else None,
            below_market_only,
            below_market_percent,
            skip_foreign,
            region,
            brand_group,
            max_age_years,
        )
        for item in existing:
            if _filter_signature(item) == candidate:
                raise DuplicateFilterError(item.id)
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
            brand_group=brand_group,
            max_age_years=max_age_years,
        )
        session.add(user_filter)
        session.commit()
        return _filter_dto(session, user_filter)


def create_preset_filter(telegram_id: int, preset_code: str) -> FilterDTO:
    """Фильтр из пресета одной кнопкой. Ошибки — те же, что у `create_filter`.

    LookupError — и для незнакомого юзера, и для неизвестного кода (кнопка из
    старой истории чата после смены реестра).
    """
    preset = find_preset(preset_code)
    if preset is None:
        raise LookupError(f"unknown preset {preset_code!r}")
    return create_filter(
        telegram_id,
        brand_id=None,
        car_model_id=None,
        year_from=None,
        year_to=None,
        mileage_max_km=None,
        price_max_usd=preset.price_max_usd,
        below_market_only=True,
        below_market_percent=preset.below_market_percent,
        region=preset.region,
        brand_group=preset.brand_group,
        max_age_years=preset.max_age_years,
    )


def ensure_base_filter(telegram_id: int) -> FilterDTO | None:
    """Ставит базовый фильтр, если у человека нет НИ ОДНОГО; иначе None.

    Нужен там, где фильтр ставится не по прямой просьбе (триал в одно нажатие,
    кнопка в напоминании): свои фильтры человека не трогаем и слот не тратим.
    """
    if filters_quota(telegram_id).used:
        return None
    try:
        return create_preset_filter(telegram_id, base_preset().code)
    except (DuplicateFilterError, FilterLimitError):
        return None  # гонка двух нажатий: фильтр уже поставил соседний апдейт


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


# --------------------------------------------------------------------------
# Чёрный список
# --------------------------------------------------------------------------


@dataclass(slots=True)
class BlockDTO:
    id: int
    kind: str
    label: str


@dataclass(slots=True)
class BlockOptions:
    """Что можно запретить по конкретному объявлению (None = признака нет)."""

    brand_name: str | None
    model_name: str | None  # уже с маркой: «Toyota Camry»
    city: str | None


@dataclass(slots=True)
class BlockResult:
    ok: bool
    label: str = ""
    already: bool = False
    limit_reached: bool = False
    unavailable: bool = False  # у объявления нет такого признака (NULL в БД)
    block_id: int | None = None  # запрет (новый или уже бывший) — для «Вернуть»


def _block_labels(session, listing: Listing) -> dict[str, tuple[str, str]]:
    """kind -> (value, label) для того, что у объявления реально заполнено."""
    options: dict[str, tuple[str, str]] = {}
    brand = session.get(Brand, listing.brand_id) if listing.brand_id else None
    if brand is not None:
        options[KIND_BRAND] = (block_value(KIND_BRAND, brand_id=brand.id), brand.name)
    model = session.get(CarModel, listing.car_model_id) if listing.car_model_id else None
    if model is not None:
        label = f"{brand.name} {model.name}" if brand is not None else model.name
        options[KIND_MODEL] = (block_value(KIND_MODEL, car_model_id=model.id), label)
    city = (listing.city or "").strip()
    if city:
        options[KIND_CITY] = (block_value(KIND_CITY, city=city), city)
    return options


def block_options(listing_id: int) -> BlockOptions | None:
    """Варианты для меню «не показывать такие»; None — объявление исчезло."""
    with SessionLocal() as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return None
        options = _block_labels(session, listing)
        return BlockOptions(
            brand_name=options.get(KIND_BRAND, (None, None))[1],
            model_name=options.get(KIND_MODEL, (None, None))[1],
            city=options.get(KIND_CITY, (None, None))[1],
        )


def add_block_from_listing(telegram_id: int, listing_id: int, kind: str) -> BlockResult:
    """Заносит в чёрный список признак объявления (марку/модель/город).

    Идёт от объявления, а не от справочника: кнопка живёт под карточкой, и
    пользователю не нужно вспоминать, как называется марка в нашем каталоге.
    """
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            raise LookupError(f"bot user {telegram_id} is not registered")
        listing = session.get(Listing, listing_id)
        if listing is None:
            return BlockResult(ok=False, unavailable=True)
        option = _block_labels(session, listing).get(kind)
        if option is None:
            return BlockResult(ok=False, unavailable=True)
        value, label = option

        existing = session.scalar(
            select(UserBlock).where(
                UserBlock.user_id == user.id, UserBlock.kind == kind, UserBlock.value == value
            )
        )
        if existing is not None:
            return BlockResult(ok=False, already=True, label=existing.label, block_id=existing.id)

        used = session.scalar(
            select(func.count()).select_from(UserBlock).where(UserBlock.user_id == user.id)
        ) or 0
        if used >= MAX_BLOCKS_PER_USER:
            return BlockResult(ok=False, limit_reached=True, label=label)

        block = UserBlock(user_id=user.id, kind=kind, value=value, label=label)
        session.add(block)
        session.commit()
        return BlockResult(ok=True, label=label, block_id=block.id)


def hide_from_listing(telegram_id: int, listing_id: int) -> BlockResult:
    """«Не то» под карточкой: запрет на модель объявления, без модели — на марку.

    Город одним тапом не скрываем: «не то» почти всегда про машину, а запрет
    города молча выключил бы все марки оттуда.
    """
    options = block_options(listing_id)
    if options is None:
        return BlockResult(ok=False, unavailable=True)
    if options.model_name is not None:
        kind = KIND_MODEL
    elif options.brand_name is not None:
        kind = KIND_BRAND
    else:
        return BlockResult(ok=False, unavailable=True)
    return add_block_from_listing(telegram_id, listing_id, kind)


def list_blocks(telegram_id: int) -> list[BlockDTO]:
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            return []
        blocks = session.scalars(
            select(UserBlock).where(UserBlock.user_id == user.id).order_by(UserBlock.id)
        ).all()
        return [BlockDTO(id=item.id, kind=item.kind, label=item.label) for item in blocks]


def delete_block(telegram_id: int, block_id: int) -> bool:
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            return False
        block = session.scalar(
            select(UserBlock).where(UserBlock.id == block_id, UserBlock.user_id == user.id)
        )
        if block is None:
            return False
        session.delete(block)
        session.commit()
        return True


def issue_admin_login_token(telegram_id: int) -> str | None:
    """Выдаёт одноразовый токен входа в панель; None — если человек не админ.

    None означает «отказано» для любой причины (нет строки, нет флага) — вызов
    не должен различать «не админ» и «не пользователь»: и то и другое отвечает
    молчанием, иначе перебором можно узнать, кто админ.

    Ранее выданные неиспользованные токены гасим: запрос новой ссылки — это
    почти всегда «предыдущая куда-то делась», и оставлять её живой незачем.
    Сам токен возвращаем ЕДИНСТВЕННЫЙ раз, в базе только его sha256.
    """
    settings = get_settings()
    if not settings.admin_panel_enabled:
        return None

    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None or not user.is_superuser:
            return None

        now = datetime.now(timezone.utc)
        session.execute(
            update(AdminLoginToken)
            .where(AdminLoginToken.user_id == user.id, AdminLoginToken.used_at.is_(None))
            .values(used_at=now)
        )
        token = secrets.token_urlsafe(32)
        session.add(
            AdminLoginToken(
                user_id=user.id,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=now + timedelta(minutes=settings.admin_login_token_ttl_minutes),
            )
        )
        session.commit()
        logger.info("Админ-вход: выдана ссылка пользователю %s", telegram_id)
        return token


def mark_user_blocked(telegram_id: int) -> None:
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is not None:
            user.is_active = False
            session.commit()


def preview_for_filter(filter_id: int, exclude_ids=frozenset()) -> list[PendingNotification]:
    """Подборка «что уже есть» для только что созданного фильтра.

    `exclude_ids` — машины, которые чату уже показали (витрина в /start)."""
    settings = get_settings()
    with SessionLocal() as session:
        return _preview_for_filter(
            session,
            filter_id,
            limit=settings.bot_filter_preview_limit,
            max_age_hours=settings.bot_filter_preview_max_age_hours,
            factor=settings.bot_below_avg_factor,
            min_samples=settings.bot_below_avg_min_samples,
            delayed_lag_minutes=settings.delayed_tier_lag_minutes,
            max_below_percent=settings.bot_max_below_market_percent,
            exclude_listing_ids=frozenset(exclude_ids),
        )


# --- дайджест: кнопка «Показать с фото» --------------------------------------


def tag_digest_rows(batch_id: int, groups: list[tuple[int, list[int]]]) -> bool:
    """Ставит номер пачки строкам `sent_notifications`, ушедшим списком.

    Зовёт рассылка ДО отправки (`notifier.send_notifications(on_digest=…)`):
    кнопка под списком ищет его позиции по (пользователь, пачка, марка).
    """
    with SessionLocal() as session:
        user_ids = dict(
            session.execute(
                select(BotUser.telegram_id, BotUser.id).where(
                    BotUser.telegram_id.in_({chat_id for chat_id, _ in groups})
                )
            ).all()
        )
        for chat_id, listing_ids in groups:
            user_id = user_ids.get(chat_id)
            if user_id is None or not listing_ids:
                continue
            session.execute(
                update(SentNotification)
                .where(
                    SentNotification.user_id == user_id,
                    SentNotification.listing_id.in_(listing_ids),
                )
                .values(batch_id=batch_id)
            )
        session.commit()
    return True


DIGEST_OK = "ok"
DIGEST_EXPIRED = "expired"  # кнопке больше суток
DIGEST_GONE = "gone"  # всё оставшееся в списке снято с продажи
DIGEST_EMPTY = "empty"  # позиций дальше нет (повторное нажатие на старую кнопку)


@dataclass(slots=True)
class DigestPage:
    status: str
    notes: list[PendingNotification]
    next_offset: int | None = None
    remaining: int = 0  # сколько ещё активных до конца этого списка


def digest_page(telegram_id: int, batch_id: int, offset: int) -> DigestPage:
    """Следующие DIGEST_PAGE активных объявлений дайджеста — карточками с фото.

    Позиции списка — строки `sent_notifications` пользователя с этой пачкой в
    порядке `order_digest` (разделы по маркам, как в самом списке). Снятые
    пропускаем. % ниже рынка — сохранённый на момент отправки (как в строке
    списка), цену рынка и число образцов пересчитываем для подписи карточки.
    """
    if batch_age_minutes(batch_id, _utcnow()) > DIGEST_TTL_MINUTES:
        return DigestPage(status=DIGEST_EXPIRED, notes=[])
    settings = get_settings()
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
        if user is None:
            return DigestPage(status=DIGEST_EMPTY, notes=[])
        rows = session.execute(
            select(SentNotification, Listing)
            .join(Listing, Listing.id == SentNotification.listing_id)
            .where(SentNotification.user_id == user.id, SentNotification.batch_id == batch_id)
        ).all()
        brand_names = {row.id: row.name for row in session.execute(select(Brand.id, Brand.name))}
        rows = order_digest(
            rows,
            key=lambda row: (
                brand_names.get(row[1].brand_id) if row[1].brand_id else None,
                row[0].below_market_percent,
                row[1].id,
            ),
        )
        window = rows[offset:]
        if not window:
            return DigestPage(status=DIGEST_EMPTY, notes=[])

        source_names = {row.id: row.name for row in session.execute(select(Source.id, Source.name))}
        model_names = load_model_names(session)
        notes: list[PendingNotification] = []
        consumed = 0
        for sent, listing in window:
            if len(notes) == DIGEST_PAGE:
                break
            consumed += 1
            if not listing.is_active:
                continue
            notes.append(
                build_pending(
                    listing,
                    chat_id=telegram_id,
                    filter_id=sent.filter_id or 0,
                    below_percent=float(sent.below_market_percent)
                    if sent.below_market_percent is not None
                    else None,
                    estimate=market_estimate(
                        session, listing, min_samples=settings.bot_below_avg_min_samples
                    ),
                    source_names=source_names,
                    brand_names=brand_names,
                    model_names=model_names,
                )
            )
        if not notes:
            return DigestPage(status=DIGEST_GONE, notes=[])
        rest = window[consumed:]
        remaining = sum(1 for _, listing in rest if listing.is_active)
        return DigestPage(
            status=DIGEST_OK,
            notes=notes,
            next_offset=offset + consumed if remaining else None,
            remaining=remaining,
        )
