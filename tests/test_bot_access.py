from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.bot.db as botdb
from app.db.base import Base
from app.bot.messages import (
    access_denied_text,
    subscribe_prompt,
    subscribe_renew_prompt,
    trial_already_used_text,
)
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


# --- текст отказа -----------------------------------------------------------
# Про закрытую группу человеку без доступа говорить нельзя: она объясняет, как
# доступ выдаётся другим, а ему не оставляет следующего шага.


def test_denied_offers_trial_when_it_is_still_available() -> None:
    text = access_denied_text(trial_days=3, trial_available=True, payments_open=False)
    assert "3 дня" in text
    assert "/subscribe" not in text  # оплата тут ни при чём, триал ещё не взят


def test_denied_after_trial_sends_to_payment_when_it_is_open() -> None:
    text = access_denied_text(trial_days=3, trial_available=False, payments_open=True)
    assert "/subscribe" in text


def test_denied_after_trial_offers_manual_help_while_payment_is_closed() -> None:
    """Пока оплата обкатывается, /subscribe отвечает «скоро» — звать туда нельзя."""
    text = access_denied_text(trial_days=3, trial_available=False, payments_open=False)
    assert "/subscribe" not in text
    assert "вручную" in text


def test_trial_already_used_avoids_dead_end_while_payment_is_closed() -> None:
    assert "/subscribe" not in trial_already_used_text(payments_open=False)
    assert "/subscribe" in trial_already_used_text(payments_open=True)


@pytest.mark.parametrize(
    "text",
    [
        access_denied_text(trial_days=3, trial_available=True, payments_open=False),
        access_denied_text(trial_days=3, trial_available=False, payments_open=True),
        access_denied_text(trial_days=3, trial_available=False, payments_open=False),
        trial_already_used_text(payments_open=True),
        trial_already_used_text(payments_open=False),
    ],
)
def test_no_text_mentions_the_closed_group(text: str) -> None:
    assert "групп" not in text.lower()


# --- гонка вставки пользователя ---------------------------------------------


def test_resolve_access_survives_parallel_insert_of_the_same_new_user(sessions) -> None:
    """Два апдейта одного новичка пришли одновременно — оба должны отработать.

    Ловили на проде 08.08.2026: Telegram отдал накопленные апдейты пачкой после
    перезапуска, оба потока увидели «юзера нет», второй INSERT упал на уникальном
    telegram_id, и aiogram уронил апдейт — человеку бот не ответил.

    Воспроизводим исход гонки: строка соседа уже закоммичена, а наша вставка
    свалилась с IntegrityError. Повтор обязан отработать по ветке «юзер найден».
    """
    telegram_id = 8787285475
    original = botdb._resolve_access_once
    calls: list[int] = []

    def racing(tg_id, username, first_name, is_member):
        calls.append(1)
        if len(calls) == 1:
            with sessions() as other:
                other.add(BotUser(telegram_id=tg_id, username=username, first_name=first_name))
                other.commit()
            raise IntegrityError("INSERT INTO bot_users", {}, Exception("duplicate key"))
        return original(tg_id, username, first_name, is_member)

    botdb._resolve_access_once = racing
    try:
        has_access = botdb.resolve_access(telegram_id, None, "Myrzabekov", False)
    finally:
        botdb._resolve_access_once = original

    assert len(calls) == 2, "повтор после гонки не сработал"
    assert has_access is False  # не член группы, без подписки — доступа нет
    with sessions() as session:
        users = session.scalars(
            select(BotUser).where(BotUser.telegram_id == telegram_id)
        ).all()
    assert len(users) == 1, "дубль пользователя"


def test_insert_race_retried_only_once(sessions) -> None:
    """Если конфликт повторился — пробрасываем: это уже не гонка, а поломка."""
    calls: list[int] = []

    def always_conflicting():
        calls.append(1)
        raise IntegrityError("INSERT INTO bot_users", {}, Exception("duplicate key"))

    with pytest.raises(IntegrityError):
        botdb._retry_on_user_insert_race(always_conflicting)
    assert len(calls) == 2


def test_subscribe_renew_prompt_says_days_stack() -> None:
    """У активной подписки /subscribe обязан звучать иначе.

    Обычное «оформите подписку» человек с активным доступом читает как «оплата
    не прошла» и платит второй раз. Проверяем главное: срок назван, сказано что
    дни складываются, и нет слова «неактивна».
    """
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    expires = now + timedelta(days=29)
    text = subscribe_renew_prompt(2000, 30, expires, now=now)
    assert "13.09.2026" in text
    assert "осталось 29 дн." in text
    assert "приплюсуются" in text
    assert "2 000 сом" in text
    assert "неактивна" not in text


def test_subscribe_renew_prompt_last_day() -> None:
    """Меньше суток — не «осталось 0 дн.», а человеческое «меньше суток»."""
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    text = subscribe_renew_prompt(2000, 30, now + timedelta(hours=5), now=now)
    assert "меньше суток" in text
    assert "осталось 0" not in text


def test_subscribe_prompt_warns_about_trial_days_burning() -> None:
    """Оплата во время триала обнуляет его остаток — предупреждаем заранее."""
    trial_until = datetime(2026, 8, 18, tzinfo=timezone.utc)
    text = subscribe_prompt(2000, 30, trial_expires_at=trial_until)
    assert "18.08.2026" in text
    assert "не приплюсуются" in text
    # Без триала лишнего абзаца быть не должно.
    assert "Бесплатный период" not in subscribe_prompt(2000, 30)
