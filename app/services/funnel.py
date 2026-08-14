"""Воронка: старты → триалы → пейвол → платежи → оплаты.

Считается по `user_events` (журнал переходов), а не по `bot_users`: там только
текущее состояние, и «отвал по дням» по нему не построить.

Живёт в services, а не в cli.py, потому что читателей двое: команда `funnel`
печатает это в терминал, админ-панель показывает на главной. Две реализации
одной воронки рано или поздно разошлись бы — и спорить пришлось бы о том, какая
права.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import UserEvent
from app.services.events import (
    EVENT_PAID,
    EVENT_PAYMENT_CREATED,
    EVENT_START,
    EVENT_TRIAL_EXPIRED,
    EVENT_TRIAL_STARTED,
    FUNNEL_EVENTS,
)

STEP_TITLES = {
    EVENT_START: "Стартовали бота",
    EVENT_TRIAL_STARTED: "Активировали триал",
    EVENT_TRIAL_EXPIRED: "Дошли до пейвола",
    EVENT_PAYMENT_CREATED: "Начали оплату",
    EVENT_PAID: "Оплатили",
}


def funnel_steps(session: Session, *, days: int) -> list[dict]:
    """Шаги воронки за период. Проценты считаем здесь, а не у читателя."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Уникальные пользователи на каждом шаге: человек, нажавший «оплатить»
    # трижды, — это один человек, а не три.
    counts = {
        row[0]: int(row[1])
        for row in session.execute(
            select(UserEvent.event, func.count(func.distinct(UserEvent.user_id)))
            .where(UserEvent.event.in_(FUNNEL_EVENTS), UserEvent.created_at >= since)
            .group_by(UserEvent.event)
        ).all()
    }

    base = counts.get(EVENT_START, 0)
    steps: list[dict] = []
    previous: int | None = None
    for event in FUNNEL_EVENTS:
        users = counts.get(event, 0)
        steps.append(
            {
                "event": event,
                "title": STEP_TITLES[event],
                "users": users,
                "of_start_percent": round(users / base * 100, 1) if base else None,
                "of_previous_percent": (
                    round(users / previous * 100, 1) if previous else None
                ),
            }
        )
        previous = users
    return steps
