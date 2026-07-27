from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.bot.db as botdb
from app.db.base import Base
from app.models import BotUser, Payment


@pytest.fixture()
def sessions(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(botdb, "SessionLocal", TestingSession)
    return TestingSession


def test_decide_access_status_member_beats_subscription() -> None:
    assert botdb.decide_access_status(True, False) == ("member", True)
    assert botdb.decide_access_status(True, True) == ("member", True)
    assert botdb.decide_access_status(False, True) == ("subscriber", True)
    assert botdb.decide_access_status(False, False) == ("revoked", False)


def test_unknown_membership_keeps_current_status() -> None:
    """Сбой get_chat_member не должен разжаловывать: matching шлёт только member/subscriber."""
    assert botdb.decide_access_status(None, False, "member") == ("member", True)
    assert botdb.decide_access_status(None, False, "revoked") == ("revoked", False)
    # Подписка действует и при неизвестном членстве.
    assert botdb.decide_access_status(None, True, "subscriber") == ("subscriber", True)


def test_unknown_membership_does_not_downgrade_existing_member(sessions) -> None:
    botdb.resolve_access(444, "u", "U", is_member=True)
    assert botdb.resolve_access(444, "u", "U", is_member=None) is True
    with sessions() as s:
        user = s.scalar(select(BotUser).where(BotUser.telegram_id == 444))
        assert user.access_status == "member"


def test_unknown_membership_for_new_user_denies_without_crash(sessions) -> None:
    """У новичка access_status ещё None — не должно быть ни падения, ни доступа."""
    assert botdb.resolve_access(555, "n", "N", is_member=None) is False
    with sessions() as s:
        user = s.scalar(select(BotUser).where(BotUser.telegram_id == 555))
        assert user.access_status == "revoked"


def test_resolve_access_group_member_gets_access(sessions) -> None:
    has_access = botdb.resolve_access(111, "u", "U", is_member=True)
    assert has_access is True
    with sessions() as s:
        user = s.scalar(select(BotUser).where(BotUser.telegram_id == 111))
        assert user.access_status == "member"


def test_resolve_access_non_member_without_subscription_denied(sessions) -> None:
    has_access = botdb.resolve_access(222, None, None, is_member=False)
    assert has_access is False
    with sessions() as s:
        user = s.scalar(select(BotUser).where(BotUser.telegram_id == 222))
        assert user.access_status == "revoked"


def test_resolve_access_active_subscriber_gets_access(sessions) -> None:
    with sessions() as s:
        s.add(
            BotUser(
                telegram_id=333,
                access_status="subscriber",
                subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=5),
            )
        )
        s.commit()
    has_access = botdb.resolve_access(333, None, None, is_member=False)
    assert has_access is True
    with sessions() as s:
        user = s.scalar(select(BotUser).where(BotUser.telegram_id == 333))
        assert user.access_status == "subscriber"


def test_resolve_access_expired_subscriber_revoked(sessions) -> None:
    with sessions() as s:
        s.add(
            BotUser(
                telegram_id=444,
                access_status="subscriber",
                subscription_expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
        s.commit()
    assert botdb.resolve_access(444, None, None, is_member=False) is False
    with sessions() as s:
        user = s.scalar(select(BotUser).where(BotUser.telegram_id == 444))
        assert user.access_status == "revoked"


def test_create_pending_payment_persists_row(sessions) -> None:
    from decimal import Decimal

    botdb.resolve_access(555, None, None, is_member=True)
    order_id = botdb.create_pending_payment(
        555, amount=Decimal("500"), currency="KGS", period_days=30
    )
    assert order_id
    with sessions() as s:
        payment = s.scalar(select(Payment).where(Payment.order_id == order_id))
        assert payment.status == "pending"
        assert payment.amount == Decimal("500")
        assert payment.period_days == 30
