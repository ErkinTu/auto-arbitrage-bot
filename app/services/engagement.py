"""Напоминания и итоги триала: кому и что слать из worker-а.

Только выборки — отправку делает worker (`send_nudges`, `send_trial_summaries`).
Учёт отправленного — в журнале `user_events`, по событию на шаг: отдельные
колонки на `bot_users` для каждого касания размножались бы с каждой новой
идеей, а журнал заодно отвечает «сколько напоминаний сработало» в воронке.

Окна по времени, а не «всем, у кого нет триала»: напоминание «вы не включили
бесплатный период» человеку, нажавшему /start три месяца назад, — спам, а не
забота. Поэтому старые пользователи под напоминания не попадают вовсе (для
них — разовая рассылка руками, `cli.py broadcast`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import BotUser, SentNotification, UserEvent, UserFilter
from app.services.events import EVENT_START

# Первое напоминание — через 12 минут (рекомендация разбора: 10–15), второе —
# через сутки, третьего нет.
NUDGE_FIRST_AFTER = timedelta(minutes=12)
NUDGE_SECOND_AFTER = timedelta(hours=24)
NUDGE_WINDOW = timedelta(hours=48)

NUDGE_TRIAL = "trial"  # нажал /start, не включил триал
NUDGE_FILTER = "filter"  # доступ есть, фильтров нет — бот молчит

# Итоги — примерно за 12 ч до конца триала: после конца единственное сообщение
# приходит уже на истёкшем доступе, и показывать «что вы получили» поздно.
TRIAL_SUMMARY_BEFORE = timedelta(hours=12)

EVENT_TRIAL_SUMMARY = "trial_summary"


def nudge_event(kind: str, step: int) -> str:
    return f"nudge_{kind}_{step}"


def _has_event(event: str):
    return select(UserEvent.user_id).where(UserEvent.event == event)


def _no_paid_access(now: datetime):
    return and_(
        BotUser.access_status != "member",
        or_(
            BotUser.subscription_expires_at.is_(None),
            BotUser.subscription_expires_at <= now,
        ),
    )


def _step_window(anchor, step: int, now: datetime):
    """Шаг 1 — в [12 мин, 24 ч) от якоря, шаг 2 — в [24 ч, 48 ч)."""
    if step == 1:
        return and_(anchor <= now - NUDGE_FIRST_AFTER, anchor > now - NUDGE_SECOND_AFTER)
    return and_(anchor <= now - NUDGE_SECOND_AFTER, anchor > now - NUDGE_WINDOW)


def trial_nudge_candidates(session: Session, step: int, now: datetime) -> list[BotUser]:
    """Нажали /start, триал так и не включили, платного доступа нет.

    Шаг 2 — только тем, кто получил шаг 1: если первое не ушло (ночь, сбой),
    второе без первого звучит странно.
    """
    started_at = (
        select(UserEvent.user_id, func.min(UserEvent.created_at).label("started_at"))
        .where(UserEvent.event == EVENT_START)
        .group_by(UserEvent.user_id)
        .subquery()
    )
    query = (
        select(BotUser)
        .join(started_at, started_at.c.user_id == BotUser.id)
        .where(
            BotUser.is_active.is_(True),
            BotUser.trial_started_at.is_(None),
            _no_paid_access(now),
            _step_window(started_at.c.started_at, step, now),
            BotUser.id.not_in(_has_event(nudge_event(NUDGE_TRIAL, step))),
        )
    )
    if step == 2:
        query = query.where(BotUser.id.in_(_has_event(nudge_event(NUDGE_TRIAL, 1))))
    return list(session.scalars(query).all())


def filter_nudge_candidates(session: Session, step: int, now: datetime) -> list[BotUser]:
    """На триале, но без единого фильтра: доступ есть, а уведомлений нет.

    Якорь — начало триала. Платных и членов группы не трогаем: у них и так
    есть /filters, а напоминание о «бесплатном» им не по адресу.
    """
    has_filter = select(UserFilter.user_id).where(UserFilter.enabled.is_(True))
    query = select(BotUser).where(
        BotUser.is_active.is_(True),
        BotUser.trial_expires_at > now,
        _no_paid_access(now),
        _step_window(BotUser.trial_started_at, step, now),
        BotUser.id.not_in(has_filter),
        BotUser.id.not_in(_has_event(nudge_event(NUDGE_FILTER, step))),
    )
    if step == 2:
        query = query.where(BotUser.id.in_(_has_event(nudge_event(NUDGE_FILTER, 1))))
    return list(session.scalars(query).all())


def trial_summary_candidates(session: Session, now: datetime) -> list[BotUser]:
    """Триал кончается в ближайшие 12 ч, платного доступа нет, итогов не слали."""
    return list(
        session.scalars(
            select(BotUser).where(
                BotUser.is_active.is_(True),
                BotUser.trial_expires_at > now,
                BotUser.trial_expires_at <= now + TRIAL_SUMMARY_BEFORE,
                _no_paid_access(now),
                BotUser.id.not_in(_has_event(EVENT_TRIAL_SUMMARY)),
            )
        ).all()
    )


@dataclass(slots=True)
class TrialStats:
    sent: int
    below_market: int
    best_percent: float | None


def trial_stats(session: Session, user: BotUser) -> TrialStats:
    """Что человек получил за триал: всего, ниже рынка, лучшая скидка.

    % берётся из `sent_notifications.below_market_percent` — на момент отправки
    (пишется только положительный, иначе NULL).
    У строк, записанных до появления колонки, он NULL: они идут в «всего», но
    не в «ниже рынка» — занижаем, а не выдумываем.
    """
    since = user.trial_started_at
    row = session.execute(
        select(
            func.count(SentNotification.id),
            func.count(SentNotification.below_market_percent),
            func.max(SentNotification.below_market_percent),
        ).where(
            SentNotification.user_id == user.id,
            SentNotification.sent_at >= since,
        )
    ).one()
    best = float(row[2]) if row[2] is not None else None
    return TrialStats(sent=row[0] or 0, below_market=row[1] or 0, best_percent=best)
