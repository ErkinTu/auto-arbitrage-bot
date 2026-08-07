"""Бесплатный триал: выдача, активность, кандидаты на пейвол.

Правила, ради которых это отдельный модуль:

- **Триал выдаётся один раз на telegram_id.** Метка `trial_started_at` НИКОГДА не
  обнуляется — иначе `/start` превращается в бесконечный бесплатный доступ.
- **Срок триала — отдельная колонка** (`trial_expires_at`), не
  `subscription_expires_at`. Смешивать нельзя: бесплатные дни попали бы в
  напоминание о продлении и в метрики оплат, а оплата поверх триала стала бы
  неотличима от продления.
- **Пейвол наступает сам собой**: `matching.py` шлёт только тем, у кого доступ
  активен, поэтому с истечением `trial_expires_at` уведомления прекращаются без
  отдельного «выключателя». Явное действие нужно только одно — разовое
  сообщение с предложением оплаты (`worker.send_trial_paywall_notices`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import BotUser
from app.services.events import EVENT_TRIAL_STARTED, log_event


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    """Наивную дату трактуем как UTC (SQLite теряет tz; в Postgres tz есть)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_trial_active(user: BotUser, now: datetime | None = None) -> bool:
    now = now or _utcnow()
    expires = _as_aware(user.trial_expires_at)
    return expires is not None and expires > now


def trial_used(user: BotUser) -> bool:
    """Триал уже выдавали (независимо от того, истёк он или нет)."""
    return user.trial_started_at is not None


def start_trial(
    session: Session, user: BotUser, *, days: int, now: datetime | None = None
) -> datetime | None:
    """Выдаёт триал. Возвращает дату окончания или None, если триал уже был.

    Идемпотентна по построению: повторный вызов (двойной тап по кнопке, гонка
    двух апдейтов) ничего не продлевает.
    """
    if trial_used(user):
        return None
    now = now or _utcnow()
    user.trial_started_at = now
    user.trial_expires_at = now + timedelta(days=days)
    user.is_active = True
    # Член группы остаётся суперюзером: его доступ шире и от триала не зависит.
    # Платный подписчик тоже не понижается — статус пересчитает resolve_access.
    if user.access_status not in ("member", "subscriber"):
        user.access_status = "trial"
    log_event(session, user.id, EVENT_TRIAL_STARTED, {"days": days})
    return user.trial_expires_at


def paywall_candidates(session: Session, now: datetime | None = None) -> list[BotUser]:
    """Кому пора показать пейвол: триал истёк, платного доступа нет, не писали.

    Условия намеренно избыточны относительно `access_status`: тот обновляется
    только когда юзер пишет боту, а пейвол должен уйти и молчуну. Поэтому
    смотрим на сами даты.
    """
    now = now or _utcnow()
    return list(
        session.scalars(
            select(BotUser).where(
                BotUser.trial_expires_at.is_not(None),
                BotUser.trial_expires_at <= now,
                BotUser.trial_expired_notified_at.is_(None),
                # Оплатил (или получил бонусные дни) — пейвол неуместен.
                or_(
                    BotUser.subscription_expires_at.is_(None),
                    BotUser.subscription_expires_at <= now,
                ),
                # Член закрытой группы платит не деньгами, а членством.
                BotUser.access_status != "member",
            )
        ).all()
    )
