"""Данные о пользователях бота для админ-панели. Только чтение.

Панель отвечает на два вопроса: «кто вот-вот отвалится» (лента) и «почему у
этого человека такой доступ» (карточка). Отсюда две особенности:

* сортировка по умолчанию — по возрастанию срока, а не по дате регистрации:
  список нужен, чтобы ловить истекающие триалы и подписки;
* карточка показывает все три источника доступа сразу (группа, подписка,
  триал), а не только победивший, — иначе «почему пускает» приходится
  выяснять запросами к базе руками.

Статус доступа считается по датам в момент показа, а не берётся из
`access_status`: бот пересчитывает сохранённый статус, только когда человек ему
пишет, и у молчащих он месяцами висит «Триалом» или «Подпиской» после конца
срока (на 13.10.2026 — 99 человек из 109 в этих двух статусах). Порядок тот же,
что в `bot/db.py::decide_access_status`.

Членство в группе по датам не вычислить, а Telegram панель не дёргает (иначе
Bot API на каждый показ списка): для «Группы» берём сохранённый ботом
`access_status` и рядом показываем, когда он проверялся
(`last_access_check_at`) — так видно и решение, и его свежесть.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import String, case, cast, func, or_, select
from sqlalchemy.orm import Session

from app.bot.messages import format_filter_line
from app.models import (
    BotUser,
    Brand,
    CarModel,
    AccessBlock,
    Payment,
    SentNotification,
    UserBlock,
    UserEvent,
    UserFilter,
)
from app.services.access_schedule import full_speed_starts_at, schedule_for_user
from app.services.plans import effective_tier
from app.services.subscription import is_subscription_active
from app.services.trial import is_trial_active, trial_used

MAX_LIMIT = 200
ACCESS_STATUSES = ("member", "subscriber", "trial", "revoked")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def effective_status(user: BotUser, now: datetime) -> str:
    """Статус доступа на момент `now`. SQL-двойник — `_effective_status_sql`."""
    if user.access_status == "member":
        return "member"
    if is_subscription_active(user, now):
        return "subscriber"
    if is_trial_active(user, now):
        return "trial"
    return "revoked"


def _effective_status_sql(now: datetime):
    return case(
        (BotUser.access_status == "member", "member"),
        (BotUser.subscription_expires_at > now, "subscriber"),
        (BotUser.trial_expires_at > now, "trial"),
        else_="revoked",
    )


def _counter(model, user_id_column, *conditions):
    """Скалярный подзапрос-счётчик.

    Джойнить четыре таблицы разом нельзя — строки перемножатся, и «3 фильтра»
    превратятся в «3 фильтра × 2 платежа». Коррелированный подзапрос на странице
    в 20 строк дешевле любой хитрости.
    """
    return (
        select(func.count(model.id))
        .where(user_id_column == BotUser.id, *conditions)
        .correlate(BotUser)
        .scalar_subquery()
    )


# Срок, который реально что-то значит для ЭТОГО пользователя: у триала свой,
# у подписчика свой, у члена группы срока нет вовсе.
def _relevant_expiry_sql(status):
    return case(
        (status == "trial", BotUser.trial_expires_at),
        (status == "subscriber", BotUser.subscription_expires_at),
        else_=None,
    )


# Когда доступ закончился — для «Нет доступа». Подписка важнее триала: она
# говорит, что человек был платным (или получал бонусные дни).
_LAPSED_AT = func.coalesce(BotUser.subscription_expires_at, BotUser.trial_expires_at)


def list_users(
    session: Session,
    *,
    status: str | None = None,
    query: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    now = _utcnow()
    status_expr = _effective_status_sql(now)

    search: list = []
    if query:
        needle = f"%{query.strip().lstrip('@')}%"
        search.append(
            or_(
                BotUser.username.ilike(needle),
                BotUser.first_name.ilike(needle),
                cast(BotUser.telegram_id, String).like(needle),
            )
        )

    conditions = list(search)
    if status in ACCESS_STATUSES:
        conditions.append(status_expr == status)

    total = session.scalar(select(func.count(BotUser.id)).where(*conditions)) or 0

    # Счётчики для чипов-фильтров: одним GROUP BY вместо четырёх запросов с
    # фронта. Считаются с учётом поиска, но БЕЗ выбранного статуса — иначе на
    # чипе «Триал» всегда стояло бы число выбранного среза, а не альтернативы.
    status_counts = {
        row[0]: int(row[1])
        for row in session.execute(
            select(status_expr, func.count(BotUser.id))
            .where(*search)
            .group_by(status_expr)
        ).all()
    }

    last_event_at = (
        select(func.max(UserEvent.created_at))
        .where(UserEvent.user_id == BotUser.id)
        .correlate(BotUser)
        .scalar_subquery()
    )

    rows = session.execute(
        select(
            BotUser,
            _counter(UserFilter, UserFilter.user_id).label("filters_count"),
            _counter(UserBlock, UserBlock.user_id).label("blocks_count"),
            # Только оплаченные: строку в `payments` создаёт каждое нажатие
            # тарифа в /subscribe, и «12 плат.» у тестового аккаунта с двумя
            # платежами по 1 сому читались как выручка. Попытки — в карточке.
            _counter(Payment, Payment.user_id, Payment.status == "paid").label("paid_count"),
            last_event_at.label("last_event_at"),
        )
        .where(*conditions)
        # NULLS LAST важен: у членов группы срока нет, и без этого они заняли бы
        # начало списка, вытеснив тех, ради кого сортировка и сделана. Среди
        # «Нет доступа» — сначала недавно отвалившиеся: их ещё можно вернуть.
        .order_by(
            _relevant_expiry_sql(status_expr).asc().nullslast(),
            _LAPSED_AT.desc().nullslast(),
            BotUser.id,
        )
        .limit(limit)
        .offset(offset)
    ).all()

    items = []
    for user, filters_count, blocks_count, paid_count, last_event in rows:
        current = effective_status(user, now)
        items.append(
            {
                "id": user.id,
                "telegram_id": user.telegram_id,
                "username": user.username,
                "first_name": user.first_name,
                "access_status": current,
                "is_superuser": user.is_superuser,
                "expires_at": _relevant_expiry_value(user, current),
                **_lapsed(user, current),
                "subscription_active": is_subscription_active(user, now),
                "trial_active": is_trial_active(user, now),
                "filters_count": int(filters_count or 0),
                "blocks_count": int(blocks_count or 0),
                "paid_payments_count": int(paid_count or 0),
                "last_event_at": _aware(last_event),
            }
        )

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "status_counts": {name: status_counts.get(name, 0) for name in ACCESS_STATUSES},
    }


def _relevant_expiry_value(user: BotUser, status: str) -> datetime | None:
    if status == "trial":
        return _aware(user.trial_expires_at)
    if status == "subscriber":
        return _aware(user.subscription_expires_at)
    return None


def _lapsed(user: BotUser, status: str) -> dict:
    """Что и когда закончилось у человека без доступа — «подписка закончилась 05.10»."""
    if status != "revoked":
        return {"lapsed_kind": None, "lapsed_at": None}
    if user.subscription_expires_at is not None:
        return {"lapsed_kind": "subscription", "lapsed_at": _aware(user.subscription_expires_at)}
    if user.trial_expires_at is not None:
        return {"lapsed_kind": "trial", "lapsed_at": _aware(user.trial_expires_at)}
    return {"lapsed_kind": None, "lapsed_at": None}


def get_user(session: Session, user_id: int) -> dict:
    user = session.get(BotUser, user_id)
    if user is None:
        raise LookupError("Пользователь не найден")

    now = _utcnow()
    current = effective_status(user, now)

    filters = session.scalars(
        select(UserFilter).where(UserFilter.user_id == user.id).order_by(UserFilter.id)
    ).all()
    brand_names = _names(session, Brand, {f.brand_id for f in filters if f.brand_id})
    model_names = _names(session, CarModel, {f.car_model_id for f in filters if f.car_model_id})

    blocks = session.scalars(
        select(UserBlock).where(UserBlock.user_id == user.id).order_by(UserBlock.id)
    ).all()
    payments = session.scalars(
        select(Payment).where(Payment.user_id == user.id).order_by(Payment.created_at.desc())
    ).all()
    access_blocks = session.scalars(
        select(AccessBlock)
        .where(AccessBlock.user_id == user.id)
        .order_by(AccessBlock.starts_at.desc(), AccessBlock.id.desc())
        .limit(50)
    ).all()
    # Скорость «сейчас» и дата полной — тем же кодом, что отвечает боту, иначе
    # панель и /status разошлись бы в ответе на один вопрос.
    schedule = schedule_for_user(session, user.id, now)
    tier = effective_tier(
        user,
        next((e.tier for e in schedule if e.starts_at <= now < e.ends_at), None),
        now,
    )
    events = session.scalars(
        select(UserEvent)
        .where(UserEvent.user_id == user.id)
        .order_by(UserEvent.created_at.desc())
        .limit(50)
    ).all()

    notifications_count = (
        session.scalar(
            select(func.count(SentNotification.id)).where(SentNotification.user_id == user.id)
        )
        or 0
    )
    last_notification_at = session.scalar(
        select(func.max(SentNotification.sent_at)).where(SentNotification.user_id == user.id)
    )
    invited_count = (
        session.scalar(select(func.count(BotUser.id)).where(BotUser.referrer_id == user.id)) or 0
    )
    referrer = session.get(BotUser, user.referrer_id) if user.referrer_id else None

    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "first_name": user.first_name,
        "access_status": current,
        "is_superuser": user.is_superuser,
        "is_active": user.is_active,
        "created_at": _aware(user.created_at),
        # Три источника доступа рядом: панель должна отвечать «почему пускает»,
        # а не только «пускает ли». Порядок — как в bot/db.py::decide_access_status.
        "access": {
            "status": current,
            "in_group": user.access_status == "member",
            "tier": tier,
            "full_speed_from": full_speed_starts_at(schedule),
            "checked_at": _aware(user.last_access_check_at),
            "subscription_active": is_subscription_active(user, now),
            "subscription_expires_at": _aware(user.subscription_expires_at),
            "trial_used": trial_used(user),
            "trial_active": is_trial_active(user, now),
            "trial_started_at": _aware(user.trial_started_at),
            "trial_expires_at": _aware(user.trial_expires_at),
        },
        "referral": {
            "invite_code": user.invite_code,
            "invited_count": invited_count,
            "bonuses_granted": user.referral_bonuses_granted,
            "referrer": _short(referrer),
        },
        "filters": [
            {
                "id": item.id,
                "enabled": item.enabled,
                # Тем же кодом, что печатает бот: пользователь и админ должны
                # видеть один и тот же текст фильтра, иначе разговор о нём
                # превращается в сверку двух разных формулировок.
                "label": format_filter_line(
                    brand_name=brand_names.get(item.brand_id),
                    model_name=model_names.get(item.car_model_id),
                    year_from=item.year_from,
                    year_to=item.year_to,
                    mileage_max_km=item.mileage_max_km,
                    price_max_usd=float(item.price_max_usd) if item.price_max_usd else None,
                    below_market_only=item.below_market_only,
                    below_market_percent=item.below_market_percent,
                    skip_foreign=item.skip_foreign,
                    region=item.region,
                    brand_group=item.brand_group,
                    max_age_years=item.max_age_years,
                ),
            }
            for item in filters
        ],
        "blocks": [
            {"id": item.id, "kind": item.kind, "label": item.label} for item in blocks
        ],
        "payments": [
            {
                "id": item.id,
                "amount": float(item.amount),
                "currency": item.currency,
                "status": item.status,
                "period_days": item.period_days,
                "plan_code": item.plan_code,
                "created_at": _aware(item.created_at),
                "paid_at": _aware(item.paid_at),
            }
            for item in payments
        ],
        "access_blocks": [
            {
                "tier": item.tier,
                "starts_at": _aware(item.starts_at),
                "ends_at": _aware(item.ends_at),
                "origin": item.origin,
                "payment_id": item.payment_id,
            }
            for item in access_blocks
        ],
        "events": [
            {"event": item.event, "created_at": _aware(item.created_at)} for item in events
        ],
        "notifications": {
            "sent_count": notifications_count,
            "last_sent_at": _aware(last_notification_at),
        },
    }


def _names(session: Session, model, ids: set[int]) -> dict[int, str]:
    """Имена марок/моделей одним запросом — на карточке фильтров до пяти."""
    if not ids:
        return {}
    rows = session.execute(select(model.id, model.name).where(model.id.in_(ids))).all()
    return {row[0]: row[1] for row in rows}


def _short(user: BotUser | None) -> dict | None:
    if user is None:
        return None
    return {"id": user.id, "telegram_id": user.telegram_id, "username": user.username}
