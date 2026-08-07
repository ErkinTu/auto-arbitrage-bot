from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.worker as worker
from app.db.base import Base
from app.models import BotUser, Payment


def _setup(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", TestingSession)
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            subscription_enabled=True,
            scheduler_timezone="Asia/Bishkek",
            subscription_reminder_days_before=3,
        ),
    )
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(worker, "send_message", lambda cid, text: (sent.append((cid, text)) or True))
    return TestingSession, sent


def test_reminders_only_for_soon_expiring_unreminded_subscribers(monkeypatch) -> None:
    SessionMaker, sent = _setup(monkeypatch)
    now = datetime.now(timezone.utc)
    with SessionMaker() as s:
        s.add_all(
            [
                # Истекает через 2 дня, не напоминали → должен получить.
                BotUser(telegram_id=1, access_status="subscriber",
                        subscription_expires_at=now + timedelta(days=2)),
                # Истекает через 10 дней → рано.
                BotUser(telegram_id=2, access_status="subscriber",
                        subscription_expires_at=now + timedelta(days=10)),
                # Уже напоминали за этот период.
                BotUser(telegram_id=3, access_status="subscriber",
                        subscription_expires_at=now + timedelta(days=1),
                        renewal_reminded_at=now),
                # Уже истекла.
                BotUser(telegram_id=4, access_status="subscriber",
                        subscription_expires_at=now - timedelta(days=1)),
                # Суперюзер группы — подписки нет.
                BotUser(telegram_id=5, access_status="member"),
            ]
        )
        s.commit()

    ran = worker.send_renewal_reminders_if_due(None)
    assert ran is not None
    assert [cid for cid, _ in sent] == [1]
    with SessionMaker() as s:
        u1 = s.scalar(select(BotUser).where(BotUser.telegram_id == 1))
        assert u1.renewal_reminded_at is not None


def test_reminders_idempotent_same_day(monkeypatch) -> None:
    SessionMaker, sent = _setup(monkeypatch)
    ran = worker.send_renewal_reminders_if_due(None)
    # Повторный запуск в тот же день не делает работу.
    assert worker.send_renewal_reminders_if_due(ran) is None


def test_reminders_skip_when_subscription_disabled(monkeypatch) -> None:
    _SessionMaker, sent = _setup(monkeypatch)
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            subscription_enabled=False,
            scheduler_timezone="Asia/Bishkek",
            subscription_reminder_days_before=3,
        ),
    )
    assert worker.send_renewal_reminders_if_due(None) is None
    assert sent == []


def test_expire_stale_payments_only_touches_old_pending(monkeypatch) -> None:
    """Finik молчит о неуспехе, поэтому pending гасим по таймауту сами."""
    SessionMaker, _sent = _setup(monkeypatch)
    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(payment_pending_ttl_hours=6)
    )
    now = datetime.now(timezone.utc)
    with SessionMaker() as s:
        user = BotUser(telegram_id=1)
        s.add(user)
        s.flush()
        s.add_all(
            [
                Payment(  # просроченный pending -> expired
                    user_id=user.id,
                    order_id="old",
                    amount=Decimal("2000.00"),
                    currency="KGS",
                    period_days=30,
                    status="pending",
                    created_at=now - timedelta(hours=7),
                ),
                Payment(  # свежий pending — человек ещё платит
                    user_id=user.id,
                    order_id="fresh",
                    amount=Decimal("2000.00"),
                    currency="KGS",
                    period_days=30,
                    status="pending",
                    created_at=now - timedelta(minutes=5),
                ),
                Payment(  # оплаченный трогать нельзя
                    user_id=user.id,
                    order_id="paid",
                    amount=Decimal("2000.00"),
                    currency="KGS",
                    period_days=30,
                    status="paid",
                    created_at=now - timedelta(days=3),
                ),
            ]
        )
        s.commit()

    assert worker.expire_stale_payments() == 1
    with SessionMaker() as s:
        statuses = {p.order_id: p.status for p in s.scalars(select(Payment)).all()}
    assert statuses == {"old": "expired", "fresh": "pending", "paid": "paid"}
    # Повторный прогон уже нечего гасить.
    assert worker.expire_stale_payments() == 0
