"""Логика подписки: продление срока, скорость доступа и признак активности.

Держим отдельно от платёжного клиента и роутера, чтобы переиспользовать в
webhook, worker-напоминаниях, ручном продлении (CLI) и тестах — эта логика
провайдеро-независима. Продление разовое:
`subscription_expires_at = max(now, текущий срок) + period_days`.

Начиная с тарифной сетки (`docs/PRICING_PLAN.md`) у каждых оплаченных дней есть
ещё и **скорость** (`full` / `delayed`), поэтому вместе со сроком всегда
пишется блок в `access_blocks` — иначе доступ был бы, а тариф к нему нет, и
`effective_tier` молча выдал бы медленный (fail-safe по умолчанию).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import BotUser, Payment
from app.services.access_schedule import append_block
from app.services.events import EVENT_PAID, log_event
from app.services.plans import ORIGIN_PAYMENT, plan_for_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    """Наивную дату трактуем как UTC (SQLite теряет tz; в Postgres tz есть)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def next_expiry(current: datetime | None, now: datetime, period_days: int) -> datetime:
    """Новый срок окончания: продлеваем от большего из (now, текущий срок).

    Если подписка ещё активна — приплюсовываем период к её концу (не теряем
    оплаченные дни); если истекла/не было — считаем от now.
    """
    current = _as_aware(current)
    base = current if current is not None and current > now else now
    return base + timedelta(days=period_days)


def is_subscription_active(user: BotUser, now: datetime | None = None) -> bool:
    now = now or _utcnow()
    expires = _as_aware(user.subscription_expires_at)
    return expires is not None and expires > now


def extend_paid_access(
    session: Session,
    user: BotUser,
    days: int,
    *,
    tier: str,
    origin: str,
    payment: Payment | None = None,
    now: datetime | None = None,
) -> datetime:
    """Продлевает «платный» доступ на days дней и чинит сопутствующие поля.

    Один хелпер на три источника дней: оплата (webhook), реферальный бонус и
    ручное продление из CLI. Бонусные дни живут в том же
    `subscription_expires_at`, что и оплаченные, — для доступа это одно и то же,
    а различить их всегда можно по `user_events`/`payments` (именно оттуда
    считается воронка, а не из `access_status`).

    Блок расписания пишется тем же вызовом и до сдвига горизонта: `append_block`
    отсчитывает `starts_at` от старого `subscription_expires_at`, то есть встаёт
    ровно в хвост. Порядок менять нельзя — иначе блок начнётся с уже нового
    конца и в расписании появится дыра.

    `renewal_reminded_at` обязательно сбрасывается: иначе продлённый юзер не
    получит напоминание перед НОВЫМ сроком — флаг остался бы от прошлого периода.
    """
    now = now or _utcnow()
    if user.id is None:  # блоку нужен user_id; у свежего юзера его ещё нет
        session.flush()
    append_block(session, user, days, tier=tier, origin=origin, payment=payment, now=now)
    user.subscription_expires_at = next_expiry(user.subscription_expires_at, now, days)
    user.access_status = "subscriber"
    user.renewal_reminded_at = None
    user.is_active = True
    return user.subscription_expires_at


def apply_paid_payment(
    session: Session,
    payment: Payment,
    *,
    provider_txn_id: str | None,
    raw_callback: dict[str, Any] | None,
    now: datetime | None = None,
) -> BotUser:
    """Помечает платёж оплаченным и продлевает подписку пользователя."""
    now = now or _utcnow()
    payment.status = "paid"
    payment.paid_at = now
    payment.finik_transaction_id = provider_txn_id
    payment.raw_callback = raw_callback

    user = session.get(BotUser, payment.user_id)
    if user is None:  # платёж без пользователя — не должно случаться
        raise ValueError(f"payment {payment.id} references missing user {payment.user_id}")
    plan = plan_for_code(payment.plan_code)
    # Дней даём столько, сколько записано в самом платеже: это то, что человеку
    # обещали в момент оплаты. Прайс мог измениться между кнопкой и webhook —
    # тариф (скорость) из плана, срок из платежа.
    extend_paid_access(
        session,
        user,
        payment.period_days,
        tier=plan.tier,
        origin=ORIGIN_PAYMENT,
        payment=payment,
        now=now,
    )
    log_event(
        session,
        user.id,
        EVENT_PAID,
        {
            "order_id": payment.order_id,
            "days": payment.period_days,
            # Без плана в событии воронку по тарифам задним числом не построить.
            "plan_code": plan.code,
            "tier": plan.tier,
        },
    )
    return user
