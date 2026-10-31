"""Журнал событий воронки (`user_events`).

Зачем отдельная таблица: `bot_users` хранит только ТЕКУЩЕЕ состояние, поэтому по
нему нельзя ответить «сколько стартовало на прошлой неделе» и «на какой день
отваливаются» — переходы нигде не сохраняются. Пишем узкий набор событий, по
одному на переход воронки; читает их `cli.py funnel`.

Событие пишется в ту же сессию, что и сам переход, и коммитится вместе с ним:
запись «оплатил» не должна пережить откат самой оплаты (и наоборот).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import UserEvent

EVENT_START = "start"  # /start (в т.ч. повторный — считаем по уникальным юзерам)
EVENT_TRIAL_STARTED = "trial_started"
EVENT_TRIAL_EXPIRED = "trial_expired"  # пишется, когда ушёл пейвол
EVENT_PAYMENT_CREATED = "payment_created"  # нажал «Оплатить», платёж заведён
EVENT_PAID = "paid"
EVENT_REFERRAL_BONUS = "referral_bonus"
EVENT_PAUSED = "paused"  # /pause — кто и надолго ли уходит
EVENT_RESUMED = "resumed"  # /play (payload: сколько часов был на паузе)
EVENT_PAUSE_REMINDER = "pause_reminder"  # ежедневное «вы на паузе»

# Порядок = порядок воронки в отчёте.
FUNNEL_EVENTS = (
    EVENT_START,
    EVENT_TRIAL_STARTED,
    EVENT_TRIAL_EXPIRED,
    EVENT_PAYMENT_CREATED,
    EVENT_PAID,
)


def log_event(
    session: Session, user_id: int, event: str, payload: dict[str, Any] | None = None
) -> None:
    """Добавляет событие в сессию (без commit — коммитит вызывающий переход)."""
    session.add(UserEvent(user_id=user_id, event=event, payload=payload))
