"""Список и карточка пользователей в панели: статус по датам, а не сохранённый.

Бот пересчитывает `access_status`, только когда человек ему пишет, поэтому у
молчащих он висит «Триалом»/«Подпиской» после конца срока. Панель обязана
показывать то, что действует сейчас, — и в строке, и на чипах-счётчиках.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import BotUser, Payment
from app.services.admin_users import get_user, list_users

NOW = datetime.now(timezone.utc)
DAY = timedelta(days=1)


@pytest.fixture()
def session():
    import app.models  # noqa: F401

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Factory() as s:
        yield s


def _user(session, telegram_id: int, stored: str, *, sub=None, trial=None) -> BotUser:
    user = BotUser(
        telegram_id=telegram_id,
        username=f"u{telegram_id}",
        access_status=stored,
        subscription_expires_at=sub,
        trial_expires_at=trial,
    )
    session.add(user)
    session.flush()
    return user


def _payment(session, user: BotUser, status: str) -> None:
    session.add(
        Payment(
            user_id=user.id,
            order_id=f"o-{user.id}-{status}-{datetime.now().timestamp()}",
            amount=Decimal("600"),
            period_days=7,
            status=status,
        )
    )
    session.flush()


def _by_tg(page: dict) -> dict[int, dict]:
    return {item["telegram_id"]: item for item in page["items"]}


@pytest.fixture()
def population(session):
    # Сохранённый статус устарел: триал/подписка кончились, человек молчит.
    _user(session, 1, "trial", trial=NOW - 62 * DAY)
    _user(session, 2, "subscriber", sub=NOW - 8 * DAY, trial=NOW - 60 * DAY)
    # Действующие.
    _user(session, 3, "trial", trial=NOW + 1 * DAY)
    _user(session, 4, "subscriber", sub=NOW + 2 * DAY)
    # Подписку выдали руками, а бот ещё не пересчитал — она всё равно действует.
    _user(session, 5, "revoked", sub=NOW + 5 * DAY)
    # Группу по датам не вычислить: верим сохранённому, даже без сроков.
    _user(session, 6, "member", sub=NOW - 30 * DAY)
    _user(session, 7, "revoked")
    session.commit()


def test_list_uses_status_by_dates_not_stored(session, population) -> None:
    items = _by_tg(list_users(session, limit=50))

    assert items[1]["access_status"] == "revoked"
    assert items[2]["access_status"] == "revoked"
    assert items[3]["access_status"] == "trial"
    assert items[4]["access_status"] == "subscriber"
    assert items[5]["access_status"] == "subscriber"
    assert items[6]["access_status"] == "member"
    assert items[7]["access_status"] == "revoked"


def test_status_chips_and_filter_count_by_dates(session, population) -> None:
    page = list_users(session, limit=50)
    assert page["status_counts"] == {"member": 1, "subscriber": 2, "trial": 1, "revoked": 3}

    subscribers = list_users(session, status="subscriber", limit=50)
    assert subscribers["total"] == 2
    assert {item["telegram_id"] for item in subscribers["items"]} == {4, 5}


def test_expiry_shown_only_for_active_access(session, population) -> None:
    items = _by_tg(list_users(session, limit=50))

    assert items[3]["expires_at"] is not None
    assert items[4]["expires_at"] is not None
    assert items[1]["expires_at"] is None
    assert items[6]["expires_at"] is None


def test_lapsed_prefers_subscription_over_trial(session, population) -> None:
    items = _by_tg(list_users(session, limit=50))

    assert items[1]["lapsed_kind"] == "trial"
    # Был и триал, и подписка: важнее, что человек был платным.
    assert items[2]["lapsed_kind"] == "subscription"
    assert items[7]["lapsed_kind"] is None and items[7]["lapsed_at"] is None
    # У действующих «закончилось» не показываем.
    assert items[4]["lapsed_kind"] is None


def test_active_sorted_by_expiry_then_recently_lapsed(session, population) -> None:
    order = [item["telegram_id"] for item in list_users(session, limit=50)["items"]]

    # Сначала те, у кого срок ближе всего, потом без доступа — свежие первыми.
    assert order[:3] == [3, 4, 5]
    assert order.index(2) < order.index(1)


def test_list_counts_only_paid_payments(session) -> None:
    user = _user(session, 10, "revoked")
    _payment(session, user, "paid")
    _payment(session, user, "paid")
    for _ in range(10):
        _payment(session, user, "expired")
    _payment(session, user, "pending")
    session.commit()

    assert _by_tg(list_users(session))[10]["paid_payments_count"] == 2
    # В карточке — все попытки, чтобы видеть и брошенные оплаты.
    assert len(get_user(session, user.id)["payments"]) == 13


def test_card_status_by_dates(session, population) -> None:
    stale = session.query(BotUser).filter_by(telegram_id=1).one()
    detail = get_user(session, stale.id)

    assert detail["access_status"] == "revoked"
    assert detail["access"]["status"] == "revoked"
    assert detail["access"]["trial_active"] is False
