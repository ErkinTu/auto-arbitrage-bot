"""Расписание доступа: блоки всегда в хвост, горизонт не убегает.

Главный инвариант — `subscription_expires_at == MAX(ends_at)`: горизонт остаётся
единственным ответом на «есть ли доступ», а блоки лишь размечают его по
скоростям. Если они разойдутся, человек либо получит доступ без тарифа (и уедет
на fail-safe `delayed`, за который не платил), либо тариф без доступа.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import AccessBlock, BotUser
from app.services.access_schedule import (
    active_tier,
    exceeds_horizon,
    full_speed_starts_at,
    load_active_tiers,
    schedule_for_user,
    tail_tier,
)
from app.services.plans import ORIGIN_MANUAL, ORIGIN_PAYMENT, TIER_DELAYED, TIER_FULL
from app.services.subscription import extend_paid_access

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _user(session, telegram_id: int = 1, **kwargs) -> BotUser:
    user = BotUser(telegram_id=telegram_id, access_status="revoked", **kwargs)
    session.add(user)
    session.flush()
    return user


def test_block_is_appended_to_the_tail_not_started_now(sessions) -> None:
    """Купленный полный тариф встаёт ПОСЛЕ оплаченного отстающего.

    Это и есть решение владельца «доплаты разницы нет»: скорость сейчас не
    меняется, но и оплаченные дни не сгорают.
    """
    with sessions() as s:
        user = _user(s)
        extend_paid_access(s, user, 7, tier=TIER_DELAYED, origin=ORIGIN_PAYMENT, now=NOW)
        extend_paid_access(s, user, 30, tier=TIER_FULL, origin=ORIGIN_PAYMENT, now=NOW)
        s.commit()

        entries = schedule_for_user(s, user.id, NOW)
        assert [entry.tier for entry in entries] == [TIER_DELAYED, TIER_FULL]
        assert entries[0].starts_at == NOW
        assert entries[0].ends_at == NOW + timedelta(days=7)
        # Смежность без дыр: второй блок начинается ровно там, где кончился первый.
        assert entries[1].starts_at == entries[0].ends_at
        assert entries[1].ends_at == NOW + timedelta(days=37)
        # Скорость сейчас — прежняя, полная только с 25.08.
        assert active_tier(s, user.id, NOW) == TIER_DELAYED
        assert full_speed_starts_at(entries) == NOW + timedelta(days=7)


def test_horizon_always_equals_the_last_block_end(sessions) -> None:
    with sessions() as s:
        user = _user(s)
        for tier, days in ((TIER_DELAYED, 7), (TIER_FULL, 30), (TIER_FULL, 7)):
            extend_paid_access(s, user, days, tier=tier, origin=ORIGIN_PAYMENT, now=NOW)
        s.commit()
        last_end = s.scalar(select(func.max(AccessBlock.ends_at)).where(AccessBlock.user_id == user.id))
        assert user.subscription_expires_at.replace(tzinfo=timezone.utc) == last_end.replace(
            tzinfo=timezone.utc
        )
        assert user.subscription_expires_at == NOW + timedelta(days=44)


def test_same_tier_blocks_are_kept_apart_but_shown_as_one(sessions) -> None:
    """В БД два платежа = два блока (видно, за что деньги), в тексте — один отрезок."""
    with sessions() as s:
        user = _user(s)
        extend_paid_access(s, user, 30, tier=TIER_FULL, origin=ORIGIN_PAYMENT, now=NOW)
        extend_paid_access(s, user, 30, tier=TIER_FULL, origin=ORIGIN_PAYMENT, now=NOW)
        s.commit()
        assert s.scalar(select(func.count(AccessBlock.id))) == 2
        entries = schedule_for_user(s, user.id, NOW)
        assert len(entries) == 1
        assert entries[0].ends_at == NOW + timedelta(days=60)
        assert full_speed_starts_at(entries) is None  # полная скорость уже идёт


def test_purchase_after_expiry_starts_now(sessions) -> None:
    with sessions() as s:
        user = _user(s, subscription_expires_at=NOW - timedelta(days=3))
        extend_paid_access(s, user, 7, tier=TIER_DELAYED, origin=ORIGIN_PAYMENT, now=NOW)
        s.commit()
        entries = schedule_for_user(s, user.id, NOW)
        assert entries[0].starts_at == NOW
        assert user.subscription_expires_at == NOW + timedelta(days=7)


def test_expired_blocks_drop_out_of_the_schedule_and_tail(sessions) -> None:
    with sessions() as s:
        user = _user(s)
        extend_paid_access(
            s, user, 7, tier=TIER_FULL, origin=ORIGIN_PAYMENT, now=NOW - timedelta(days=30)
        )
        s.commit()
        assert schedule_for_user(s, user.id, NOW) == []
        assert active_tier(s, user.id, NOW) is None
        assert tail_tier(s, user.id, NOW) is None


def test_tail_tier_is_the_last_block_not_the_active_one(sessions) -> None:
    """Бонусные дни наследуют скорость хвоста: они туда и дописываются."""
    with sessions() as s:
        user = _user(s)
        extend_paid_access(s, user, 7, tier=TIER_DELAYED, origin=ORIGIN_PAYMENT, now=NOW)
        extend_paid_access(s, user, 30, tier=TIER_FULL, origin=ORIGIN_PAYMENT, now=NOW)
        s.commit()
        assert active_tier(s, user.id, NOW) == TIER_DELAYED
        assert tail_tier(s, user.id, NOW) == TIER_FULL


def test_overlapping_blocks_resolve_to_full(sessions) -> None:
    """Перекрытие возможно только ручным SQL; тогда выигрывает то, за что заплатили."""
    with sessions() as s:
        user = _user(s)
        s.add_all(
            [
                AccessBlock(
                    user_id=user.id,
                    tier=TIER_DELAYED,
                    starts_at=NOW - timedelta(days=1),
                    ends_at=NOW + timedelta(days=10),
                    origin=ORIGIN_MANUAL,
                ),
                AccessBlock(
                    user_id=user.id,
                    tier=TIER_FULL,
                    starts_at=NOW - timedelta(days=1),
                    ends_at=NOW + timedelta(days=5),
                    origin=ORIGIN_MANUAL,
                ),
            ]
        )
        s.commit()
        assert load_active_tiers(s, {user.id}, NOW) == {user.id: TIER_FULL}


def test_horizon_limit_blocks_buying_too_far_ahead(sessions) -> None:
    """Возвратов у нас нет, поэтому дальше двух месяцев доступ не продаём."""
    with sessions() as s:
        user = _user(s, subscription_expires_at=NOW + timedelta(days=40))
        assert exceeds_horizon(user, 30, max_horizon_days=60, now=NOW) is True
        assert exceeds_horizon(user, 7, max_horizon_days=60, now=NOW) is False
        # 0 = лимит выключен.
        assert exceeds_horizon(user, 30, max_horizon_days=0, now=NOW) is False


def test_no_active_blocks_means_no_tiers_at_all(sessions) -> None:
    with sessions() as s:
        assert load_active_tiers(s, set(), NOW) == {}
