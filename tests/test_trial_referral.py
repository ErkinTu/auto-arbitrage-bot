"""Триал, пейвол и рефералка: правила, из-за нарушения которых теряются деньги.

Главные инварианты: триал одноразовый, пейвол-сообщение разовое, реферальный
бонус идемпотентный и не начисляется за самого себя.
"""

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.bot.db as botdb
import app.worker as worker
from app.db.base import Base
from app.models import BotUser, UserEvent
from app.services.events import EVENT_START, EVENT_TRIAL_EXPIRED, EVENT_TRIAL_STARTED
from app.services.referral import (
    attach_referrer,
    ensure_invite_code,
    grant_due_bonuses,
    parse_deep_link,
    referral_stats,
)
from app.services.trial import is_trial_active, paywall_candidates, start_trial, trial_used

TRIAL_DAYS = 3
BONUS_DAYS = 7
FRIENDS_PER_BONUS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        trial_days=TRIAL_DAYS,
        trial_enabled=True,
        referral_enabled=True,
        referral_bonus_days=BONUS_DAYS,
        referral_friends_per_bonus=FRIENDS_PER_BONUS,
        referral_max_bonuses=0,  # без потолка: тесты про сам механизм начисления
        subscription_price_kgs=2000,
        subscription_period_days=30,
        subscription_enabled=True,
        subscription_testing=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def bot_db(sessions, monkeypatch):
    monkeypatch.setattr(botdb, "SessionLocal", sessions)
    monkeypatch.setattr(botdb, "get_settings", lambda: _settings())
    return sessions


def _add_user(session, telegram_id: int, **kwargs) -> BotUser:
    user = BotUser(telegram_id=telegram_id, **kwargs)
    session.add(user)
    session.flush()
    return user


# --------------------------------------------------------------------------
# Триал
# --------------------------------------------------------------------------


def test_trial_granted_once_per_user(sessions) -> None:
    """Иначе /start = бесконечный бесплатный доступ."""
    with sessions() as s:
        user = _add_user(s, 1)
        first = start_trial(s, user, days=TRIAL_DAYS)
        s.commit()
        assert first is not None
        assert is_trial_active(user)

        assert start_trial(s, user, days=TRIAL_DAYS) is None
        assert user.trial_expires_at == first  # повтор ничего не продлевает


def test_expired_trial_still_counts_as_used(sessions) -> None:
    with sessions() as s:
        user = _add_user(s, 1, trial_started_at=_now() - timedelta(days=10),
                         trial_expires_at=_now() - timedelta(days=7))
        assert trial_used(user) is True
        assert is_trial_active(user) is False
        assert start_trial(s, user, days=TRIAL_DAYS) is None


def test_trial_does_not_downgrade_member_or_subscriber(sessions) -> None:
    with sessions() as s:
        member = _add_user(s, 1, access_status="member")
        subscriber = _add_user(s, 2, access_status="subscriber",
                               subscription_expires_at=_now() + timedelta(days=20))
        start_trial(s, member, days=TRIAL_DAYS)
        start_trial(s, subscriber, days=TRIAL_DAYS)
        assert member.access_status == "member"
        assert subscriber.access_status == "subscriber"


def test_trial_logs_funnel_event(sessions) -> None:
    with sessions() as s:
        user = _add_user(s, 1)
        start_trial(s, user, days=TRIAL_DAYS)
        s.commit()
        events = s.scalars(select(UserEvent.event)).all()
    assert events == [EVENT_TRIAL_STARTED]


def test_decide_access_status_trial_is_third_source() -> None:
    assert botdb.decide_access_status(False, False, "revoked", True) == ("trial", True)
    # Приоритет: группа > оплата > триал.
    assert botdb.decide_access_status(True, False, "revoked", True) == ("member", True)
    assert botdb.decide_access_status(False, True, "trial", True) == ("subscriber", True)
    assert botdb.decide_access_status(False, False, "trial", False) == ("revoked", False)
    # Сбой get_chat_member не должен отбирать доступ у триальщика.
    assert botdb.decide_access_status(None, False, "trial", True) == ("trial", True)


def test_resolve_access_lets_trial_user_in(bot_db) -> None:
    with bot_db() as s:
        _add_user(s, 77, access_status="revoked",
                  trial_started_at=_now(), trial_expires_at=_now() + timedelta(days=2))
        s.commit()
    assert botdb.resolve_access(77, "u", "U", is_member=False) is True
    with bot_db() as s:
        assert s.scalar(select(BotUser.access_status).where(BotUser.telegram_id == 77)) == "trial"


# --------------------------------------------------------------------------
# Пейвол
# --------------------------------------------------------------------------


def test_paywall_candidates_only_expired_unpaid_unnotified(sessions) -> None:
    now = _now()
    with sessions() as s:
        _add_user(s, 1, access_status="trial", trial_started_at=now - timedelta(days=4),
                  trial_expires_at=now - timedelta(hours=1))  # ← только он
        _add_user(s, 2, access_status="trial", trial_started_at=now,
                  trial_expires_at=now + timedelta(days=2))  # триал ещё идёт
        _add_user(s, 3, access_status="subscriber", trial_expires_at=now - timedelta(days=1),
                  subscription_expires_at=now + timedelta(days=10))  # оплатил
        _add_user(s, 4, access_status="member", trial_expires_at=now - timedelta(days=1))
        _add_user(s, 5, access_status="revoked", trial_expires_at=now - timedelta(days=5),
                  trial_expired_notified_at=now - timedelta(days=4))  # уже писали
        _add_user(s, 6)  # триала не было вовсе
        s.commit()

        assert [u.telegram_id for u in paywall_candidates(s, now)] == [1]


def test_worker_sends_paywall_notice_exactly_once(sessions, monkeypatch) -> None:
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(worker, "send_message", lambda cid, text: (sent.append((cid, text)) or True))

    now = _now()
    with sessions() as s:
        _add_user(s, 1, access_status="trial", trial_started_at=now - timedelta(days=4),
                  trial_expires_at=now - timedelta(hours=2))
        s.commit()

    assert worker.send_trial_paywall_notices() == 1
    assert worker.send_trial_paywall_notices() == 0  # второй раз молчим
    assert [cid for cid, _ in sent] == [1]
    assert "/subscribe" in sent[0][1]

    with sessions() as s:
        user = s.scalar(select(BotUser).where(BotUser.telegram_id == 1))
        assert user.trial_expired_notified_at is not None
        assert user.access_status == "revoked"
        assert s.scalars(select(UserEvent.event)).all() == [EVENT_TRIAL_EXPIRED]


def test_paywall_does_not_send_to_subscribe_while_payments_are_being_tested(
    sessions, monkeypatch
) -> None:
    """Пока оплата обкатывается, единственное пейвол-сообщение не зовёт в тупик."""
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(
        worker, "get_settings", lambda: _settings(subscription_testing=True)
    )
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(worker, "send_message", lambda cid, text: (sent.append((cid, text)) or True))

    now = _now()
    with sessions() as s:
        _add_user(s, 1, access_status="trial", trial_started_at=now - timedelta(days=4),
                  trial_expires_at=now - timedelta(hours=2))
        s.commit()

    assert worker.send_trial_paywall_notices() == 1
    assert "/subscribe" not in sent[0][1]
    assert "вручную" in sent[0][1]


def test_paywall_notice_marked_even_if_delivery_failed(sessions, monkeypatch) -> None:
    """Заблокировавший бота не должен получать попытку каждую минуту."""
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    monkeypatch.setattr(worker, "send_message", lambda cid, text: False)
    now = _now()
    with sessions() as s:
        _add_user(s, 1, access_status="trial", trial_started_at=now - timedelta(days=4),
                  trial_expires_at=now - timedelta(hours=2))
        s.commit()

    assert worker.send_trial_paywall_notices() == 1
    assert worker.send_trial_paywall_notices() == 0


def test_paywall_skipped_when_trial_disabled(sessions, monkeypatch) -> None:
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: _settings(trial_enabled=False))
    monkeypatch.setattr(worker, "send_message", lambda cid, text: True)
    assert worker.send_trial_paywall_notices() == 0


# --------------------------------------------------------------------------
# Рефералка
# --------------------------------------------------------------------------


def test_parse_deep_link() -> None:
    assert parse_deep_link("ref_ab12cd") == "ab12cd"
    assert parse_deep_link("ref_AB12CD") == "ab12cd"
    assert parse_deep_link("something") is None
    assert parse_deep_link("ref_") is None
    assert parse_deep_link(None) is None


def test_invite_code_is_stable_and_unique(sessions) -> None:
    with sessions() as s:
        first, second = _add_user(s, 1), _add_user(s, 2)
        code = ensure_invite_code(s, first)
        assert ensure_invite_code(s, first) == code  # не перевыпускается
        assert ensure_invite_code(s, second) != code


def test_attach_referrer_rules(sessions) -> None:
    with sessions() as s:
        inviter = _add_user(s, 1)
        code = ensure_invite_code(s, inviter)
        s.commit()

        newcomer = _add_user(s, 2)
        assert attach_referrer(s, newcomer, code) is inviter
        # Повторная привязка (другой ссылкой) невозможна.
        other = _add_user(s, 3)
        other_code = ensure_invite_code(s, other)
        assert attach_referrer(s, newcomer, other_code) is None
        assert newcomer.referrer_id == inviter.id

        # Самоприглашение запрещено.
        assert attach_referrer(s, inviter, code) is None
        # Несуществующий код.
        assert attach_referrer(s, _add_user(s, 4), "nosuch") is None
        # Не новичок: уже активировал триал — задним числом не привязываем.
        veteran = _add_user(s, 5, trial_started_at=_now())
        assert attach_referrer(s, veteran, code) is None


def test_bonus_granted_per_threshold_and_is_idempotent(sessions) -> None:
    with sessions() as s:
        inviter = _add_user(s, 1)
        s.flush()
        for tid in (2, 3):
            _add_user(s, tid, referrer_id=inviter.id, trial_started_at=_now())
        s.commit()

        # Двух друзей мало.
        assert grant_due_bonuses(s, inviter, friends_per_bonus=FRIENDS_PER_BONUS,
                                 bonus_days=BONUS_DAYS) == 0
        assert inviter.subscription_expires_at is None

        # Третий — не активировал триал, значит не в счёт.
        _add_user(s, 4, referrer_id=inviter.id)
        s.commit()
        assert grant_due_bonuses(s, inviter, friends_per_bonus=FRIENDS_PER_BONUS,
                                 bonus_days=BONUS_DAYS) == 0

        _add_user(s, 5, referrer_id=inviter.id, trial_started_at=_now())
        s.commit()
        assert grant_due_bonuses(s, inviter, friends_per_bonus=FRIENDS_PER_BONUS,
                                 bonus_days=BONUS_DAYS) == 1
        first_expiry = inviter.subscription_expires_at
        assert first_expiry is not None
        assert inviter.access_status == "subscriber"

        # Повторный вызов без новых друзей ничего не добавляет.
        assert grant_due_bonuses(s, inviter, friends_per_bonus=FRIENDS_PER_BONUS,
                                 bonus_days=BONUS_DAYS) == 0
        assert inviter.subscription_expires_at == first_expiry


def test_bonus_catches_up_several_thresholds_at_once(sessions) -> None:
    with sessions() as s:
        inviter = _add_user(s, 1)
        s.flush()
        for tid in range(2, 8):  # 6 друзей = 2 бонуса
            _add_user(s, tid, referrer_id=inviter.id, trial_started_at=_now())
        s.commit()
        now = _now()
        assert grant_due_bonuses(s, inviter, friends_per_bonus=FRIENDS_PER_BONUS,
                                 bonus_days=BONUS_DAYS, now=now) == 2
        assert inviter.referral_bonuses_granted == 2
        granted_days = (inviter.subscription_expires_at - now).days
        assert granted_days == 2 * BONUS_DAYS


def test_bonus_stops_at_max_bonuses(sessions) -> None:
    """Потолок: 6 друзей заслуживают 2 бонуса, но при max_bonuses=1 дадим один."""
    with sessions() as s:
        inviter = _add_user(s, 1)
        s.flush()
        for tid in range(2, 8):  # 6 друзей = 2 бонуса по заслугам
            _add_user(s, tid, referrer_id=inviter.id, trial_started_at=_now())
        s.commit()
        now = _now()
        assert (
            grant_due_bonuses(
                s,
                inviter,
                friends_per_bonus=FRIENDS_PER_BONUS,
                bonus_days=BONUS_DAYS,
                max_bonuses=1,
                now=now,
            )
            == 1
        )
        assert inviter.referral_bonuses_granted == 1
        assert (inviter.subscription_expires_at - now).days == BONUS_DAYS

        # Ещё трое друзей потолок не пробивают.
        for tid in range(8, 11):
            _add_user(s, tid, referrer_id=inviter.id, trial_started_at=_now())
        s.commit()
        assert (
            grant_due_bonuses(
                s,
                inviter,
                friends_per_bonus=FRIENDS_PER_BONUS,
                bonus_days=BONUS_DAYS,
                max_bonuses=1,
            )
            == 0
        )
        assert inviter.referral_bonuses_granted == 1

        stats = referral_stats(
            s, inviter, friends_per_bonus=FRIENDS_PER_BONUS, max_bonuses=1
        )
        assert stats.limit_reached is True
        assert stats.friends_to_next_bonus == 0


def test_lowering_max_bonuses_does_not_take_days_back(sessions) -> None:
    """Понизили потолок задним числом — уже выданные дни остаются."""
    with sessions() as s:
        inviter = _add_user(s, 1)
        s.flush()
        for tid in range(2, 8):
            _add_user(s, tid, referrer_id=inviter.id, trial_started_at=_now())
        s.commit()
        grant_due_bonuses(s, inviter, friends_per_bonus=FRIENDS_PER_BONUS, bonus_days=BONUS_DAYS)
        assert inviter.referral_bonuses_granted == 2
        expiry = inviter.subscription_expires_at

        assert (
            grant_due_bonuses(
                s,
                inviter,
                friends_per_bonus=FRIENDS_PER_BONUS,
                bonus_days=BONUS_DAYS,
                max_bonuses=1,
            )
            == 0
        )
        assert inviter.referral_bonuses_granted == 2
        assert inviter.subscription_expires_at == expiry


def test_referral_stats_counts_only_qualified(sessions) -> None:
    with sessions() as s:
        inviter = _add_user(s, 1)
        s.flush()
        _add_user(s, 2, referrer_id=inviter.id, trial_started_at=_now())
        _add_user(s, 3, referrer_id=inviter.id)  # перешёл, но не активировал
        s.commit()
        stats = referral_stats(s, inviter, friends_per_bonus=FRIENDS_PER_BONUS)
    assert (stats.invited_total, stats.qualified) == (2, 1)
    assert stats.friends_to_next_bonus == FRIENDS_PER_BONUS - 1


# --------------------------------------------------------------------------
# Склейка в bot/db.py
# --------------------------------------------------------------------------


def test_handle_start_logs_once_and_attaches_referrer(bot_db) -> None:
    with bot_db() as s:
        inviter = _add_user(s, 1)
        code = ensure_invite_code(s, inviter)
        _add_user(s, 2)
        s.commit()

    info = botdb.handle_start(2, username="u", first_name="U", deep_link_payload=f"ref_{code}")
    assert info.first_start is True
    assert info.referrer_attached is True

    again = botdb.handle_start(2, username="u", first_name="U")
    assert again.first_start is False  # событие «старт» пишем один раз на юзера

    with bot_db() as s:
        starts = s.scalars(select(UserEvent).where(UserEvent.event == EVENT_START)).all()
        assert len(starts) == 1
        assert s.scalar(select(BotUser.referrer_id).where(BotUser.telegram_id == 2)) is not None


def test_activate_trial_grants_and_rewards_referrer(bot_db) -> None:
    with bot_db() as s:
        inviter = _add_user(s, 1)
        s.flush()
        # Двое уже дошли до триала, третий — прямо сейчас.
        for tid in (2, 3):
            _add_user(s, tid, referrer_id=inviter.id, trial_started_at=_now())
        _add_user(s, 4, referrer_id=inviter.id)
        s.commit()

    result = botdb.activate_trial(4)
    assert result.ok is True
    assert result.referrer_chat_id == 1
    assert result.referrer_bonus_days == BONUS_DAYS

    repeat = botdb.activate_trial(4)
    assert (repeat.ok, repeat.already_used) == (False, True)

    with bot_db() as s:
        inviter = s.scalar(select(BotUser).where(BotUser.telegram_id == 1))
        assert inviter.referral_bonuses_granted == 1
        assert inviter.subscription_expires_at is not None


def test_funnel_report_counts_unique_users(sessions, monkeypatch, capsys) -> None:
    """Воронку считаем по журналу событий и по УНИКАЛЬНЫМ юзерам."""
    import app.cli as cli
    from app.services.events import EVENT_PAID, EVENT_PAYMENT_CREATED, log_event

    monkeypatch.setattr(cli, "SessionLocal", sessions)
    with sessions() as s:
        first, second = _add_user(s, 1), _add_user(s, 2)
        s.flush()
        for user in (first, second):
            log_event(s, user.id, EVENT_START)
        start_trial(s, first, days=TRIAL_DAYS)
        # Один и тот же человек жал «оплатить» дважды — это один человек.
        log_event(s, first.id, EVENT_PAYMENT_CREATED)
        log_event(s, first.id, EVENT_PAYMENT_CREATED)
        log_event(s, first.id, EVENT_PAID)
        s.commit()

    cli.command_funnel(SimpleNamespace(days=30))
    out = capsys.readouterr().out

    def row(title: str) -> int:
        match = re.search(rf"^{title}\s+(\d+)", out, re.MULTILINE)
        assert match, f"no row {title!r} in:\n{out}"
        return int(match.group(1))

    assert row("Стартовали бота") == 2
    assert row("Активировали триал") == 1
    assert row("Начали оплату") == 1  # дубль нажатия не удваивает
    assert row("Оплатили") == 1
    assert row("Активный триал") == 1


def test_activate_trial_without_referrer_notifies_nobody(bot_db) -> None:
    with bot_db() as s:
        _add_user(s, 9)
        s.commit()
    result = botdb.activate_trial(9)
    assert result.ok is True
    assert result.referrer_chat_id is None


# --------------------------------------------------------------------------
# Демо-лот для онбординга
# --------------------------------------------------------------------------


@pytest.fixture()
def demo_db(sessions, monkeypatch):
    """Как bot_db, но ещё и сбрасывает модульный кэш демо-лота между тестами."""
    monkeypatch.setattr(botdb, "SessionLocal", sessions)
    monkeypatch.setattr(botdb, "get_settings", lambda: _settings(bot_below_avg_min_samples=2))
    monkeypatch.setattr(botdb, "_demo_cache", (0.0, None))
    return sessions


def test_demo_listing_is_a_real_below_market_car(demo_db) -> None:
    from datetime import date
    from decimal import Decimal

    from app.models import Brand, CarModel, DailyMarketStat, Listing, Source

    with demo_db() as s:
        source, brand = Source(name="mashina.kg"), Brand(name="Toyota")
        s.add_all([source, brand])
        s.flush()
        model = CarModel(brand_id=brand.id, name="Camry")
        s.add(model)
        s.flush()
        s.add_all(
            [
                Listing(
                    source_id=source.id,
                    external_id="1",
                    brand_id=brand.id,
                    car_model_id=model.id,
                    title="Toyota Camry 2019",
                    source_slug="toyota-camry-1",
                    year=2019,
                    mileage_km=90_000,
                    city="Бишкек",
                    current_price_usd=Decimal("8000"),
                    is_active=True,
                    analytics_eligible=True,
                    source_created_at=_now(),
                    payload_hash="x" * 8,
                ),
                DailyMarketStat(
                    stat_date=date.today(),
                    brand_id=brand.id,
                    car_model_id=model.id,
                    year=2019,
                    mileage_bucket="50-100k",
                    customs_status="unknown",
                    avg_price_usd=Decimal("10000"),
                    median_price_usd=Decimal("10000"),
                    min_price_usd=Decimal("10000"),
                    max_price_usd=Decimal("10000"),
                    sample_count=5,
                ),
            ]
        )
        s.commit()

    demo = botdb.get_demo_listing(chat_id=42)
    assert demo is not None
    assert demo.chat_id == 42
    assert demo.below_market_percent == 20.0  # 8000 против средней 10000

    # Второй вызов идёт из кэша, но адресуется уже другому чату.
    assert botdb.get_demo_listing(chat_id=43).chat_id == 43


def test_demo_listing_absent_on_empty_base(demo_db) -> None:
    assert botdb.get_demo_listing(chat_id=1) is None
