"""Расписание оплаченного доступа: очередь блоков `access_blocks`.

Одна идея на весь модуль: **блок всегда дописывается в хвост**. Купленный
«быстрый» тариф не понижает и не заменяет уже оплаченный «медленный», а встаёт
после него (доплаты разницы нет — решение владельца, `docs/payments/PRICING_PLAN.md` §3).
Поэтому доступ выглядит как `[delayed 12 дней][full 30 дней]`, переключение
скорости случается само в срок, без джобы и пересчёта, и человеку можно назвать
точную дату начала полной скорости.

Горизонт доступа (`bot_users.subscription_expires_at`) остаётся кэшем
`MAX(ends_at)`: гейт доступа, матчинг, напоминания, `/status` и админка читают
его как раньше. Эта таблица отвечает только на вопрос «с какой скоростью».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccessBlock, BotUser, Payment
from app.services.plans import TIER_FULL


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    """Наивную дату трактуем как UTC (SQLite теряет tz; в Postgres tz есть)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def tail_end(user: BotUser, now: datetime) -> datetime:
    """Конец расписания: с этого момента начнётся следующий купленный блок.

    Берём горизонт с юзера, а не MAX(ends_at) из таблицы: горизонт — единственный
    источник математики срока (его двигают и оплата, и бонусы, и CLI), а блоки
    лишь размечают его по скоростям.
    """
    expires = _as_aware(user.subscription_expires_at)
    return expires if expires is not None and expires > now else now


def append_block(
    session: Session,
    user: BotUser,
    days: int,
    *,
    tier: str,
    origin: str,
    payment: Payment | None = None,
    now: datetime | None = None,
) -> AccessBlock:
    """Дописывает блок в хвост расписания. Горизонт двигает вызывающий.

    `starts_at = max(now, горизонт)`, `ends_at = starts_at + days` — ровно та же
    арифметика, что у `subscription.next_expiry`, поэтому конец последнего блока
    и `subscription_expires_at` всегда совпадают.
    """
    now = now or _utcnow()
    starts_at = tail_end(user, now)
    block = AccessBlock(
        user_id=user.id,
        tier=tier,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=days),
        origin=origin,
        payment_id=payment.id if payment is not None else None,
    )
    session.add(block)
    return block


def load_active_tiers(
    session: Session, user_ids: set[int] | list[int], now: datetime | None = None
) -> dict[int, str]:
    """Скорость по каждому юзеру одним запросом — для цикла матчинга.

    Перекрытие блоков в норме невозможно (пишем строго в хвост), но ручной SQL
    его допускает; тогда выигрывает `full` — за оба отрезка человек заплатил.
    """
    if not user_ids:
        return {}
    now = now or _utcnow()
    rows = session.execute(
        select(AccessBlock.user_id, AccessBlock.tier).where(
            AccessBlock.user_id.in_(list(user_ids)),
            AccessBlock.starts_at <= now,
            AccessBlock.ends_at > now,
        )
    )
    tiers: dict[int, str] = {}
    for user_id, tier in rows:
        if tiers.get(user_id) != TIER_FULL:
            tiers[user_id] = tier
    return tiers


def active_tier(session: Session, user_id: int, now: datetime | None = None) -> str | None:
    """Скорость одного юзера; None — оплаченного блока на сейчас нет."""
    return load_active_tiers(session, [user_id], now).get(user_id)


def tail_tier(session: Session, user_id: int, now: datetime | None = None) -> str | None:
    """Скорость ПОСЛЕДНЕГО блока расписания; None — расписание пусто.

    Нужна бонусным дням (рефералка, CLI): они дописываются в хвост, значит и
    скорость логично брать оттуда, а не из активного сейчас блока.
    """
    now = now or _utcnow()
    return session.scalar(
        select(AccessBlock.tier)
        .where(AccessBlock.user_id == user_id, AccessBlock.ends_at > now)
        .order_by(AccessBlock.ends_at.desc())
        .limit(1)
    )


@dataclass(slots=True)
class ScheduleEntry:
    """Отрезок расписания для показа человеку (уже склеенный по скорости)."""

    tier: str
    starts_at: datetime
    ends_at: datetime

    @property
    def delayed(self) -> bool:
        return self.tier != TIER_FULL


def schedule_for_user(
    session: Session, user_id: int, now: datetime | None = None
) -> list[ScheduleEntry]:
    """Активный и будущие отрезки доступа, смежные одинаковые — склеены.

    Склейка только для показа: в БД блоки не сливаем (у каждого свой платёж, так
    видно, за что деньги), но в `/status` две строки «полная скорость» подряд
    читались бы как ошибка.
    """
    now = now or _utcnow()
    blocks = session.scalars(
        select(AccessBlock)
        .where(AccessBlock.user_id == user_id, AccessBlock.ends_at > now)
        .order_by(AccessBlock.starts_at, AccessBlock.id)
    ).all()
    entries: list[ScheduleEntry] = []
    for block in blocks:
        starts_at = _as_aware(block.starts_at)
        ends_at = _as_aware(block.ends_at)
        if entries and entries[-1].tier == block.tier and entries[-1].ends_at >= starts_at:
            entries[-1].ends_at = max(entries[-1].ends_at, ends_at)
            continue
        entries.append(ScheduleEntry(tier=block.tier, starts_at=starts_at, ends_at=ends_at))
    return entries


def full_speed_starts_at(entries: list[ScheduleEntry]) -> datetime | None:
    """Когда начнётся полная скорость, если сейчас она ещё не действует.

    None — либо полная скорость уже идёт (первый отрезок полный), либо её в
    расписании нет вовсе. Нужна текстам: молчать о дате нельзя, иначе оплативший
    дорогой тариф прочтёт первый тихий час как обман.
    """
    if not entries or not entries[0].delayed:
        return None
    for entry in entries:
        if not entry.delayed:
            return entry.starts_at
    return None


def horizon_after(user: BotUser, days: int, now: datetime | None = None) -> datetime:
    """Куда уедет горизонт доступа, если добавить `days` дней."""
    now = now or _utcnow()
    return tail_end(user, now) + timedelta(days=days)


def exceeds_horizon(
    user: BotUser, days: int, *, max_horizon_days: int, now: datetime | None = None
) -> bool:
    """Уводит ли покупка доступ дальше разрешённого горизонта.

    Зачем потолок: возвратов у нас нет (Finik — разовые платежи, возврат только
    руками через их поддержку), и чем дальше оплачено, тем вероятнее просьба
    вернуть деньги. Два месяца при месячном максимальном плане = продлить заранее
    можно ровно один раз, этого достаточно, чтобы не терять доступ на стыке.
    """
    if max_horizon_days <= 0:
        return False
    now = now or _utcnow()
    return horizon_after(user, days, now) > now + timedelta(days=max_horizon_days)
