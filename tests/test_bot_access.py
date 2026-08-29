from datetime import datetime, timedelta, timezone

import pytest
from aiogram.types import CallbackQuery
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.bot.db as botdb
from app.db.base import Base
from app.bot.access import AccessMiddleware
from app.bot.keyboards import BrandCB, PlanCB
from app.bot.messages import (
    access_denied_text,
    plan_confirm_text,
    trial_already_used_text,
)
from app.services.access_schedule import ScheduleEntry
from app.services.plans import PLANS, find_plan
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


def test_create_pending_payment_takes_price_from_the_registry(sessions) -> None:
    """Сумма и срок — из реестра планов, а не из данных клиента.

    Кнопку в чате можно нажать через неделю; подменяемой цены в этом пути быть
    не должно, поэтому наружу торчит только код плана.
    """
    botdb.resolve_access(555, None, None, is_member=True)
    plan = find_plan("week_delayed")
    draft = botdb.create_pending_payment(555, plan_code=plan.code)
    assert draft.ok and draft.order_id
    assert draft.starts_now is True  # доступа не было — дни идут с сегодня
    with sessions() as s:
        payment = s.scalar(select(Payment).where(Payment.order_id == draft.order_id))
        assert payment.status == "pending"
        assert payment.amount == plan.price_kgs
        assert payment.period_days == plan.days
        assert payment.plan_code == plan.code


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


def _draft(plan_code: str, **overrides) -> botdb.PaymentDraft:
    return botdb.PaymentDraft(ok=True, plan_code=plan_code, order_id="o", **overrides)


def test_plan_confirm_names_both_dates_for_upgrade_over_delayed() -> None:
    """Апгрейд поверх отстающего обязан назвать дату начала полной скорости.

    Без даты оплативший 1990 прочтёт первый тихий час как обман — он ждёт
    мгновенного ускорения, а получает его после конца уже оплаченного блока.
    """
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    current_end = now + timedelta(days=15)
    plan = find_plan("month_full")
    text = plan_confirm_text(
        plan,
        _draft(
            plan.code,
            starts_at=current_end,
            starts_now=False,
            access_until=current_end + timedelta(days=30),
            schedule_before=(
                ScheduleEntry(tier="delayed", starts_at=now, ends_at=current_end),
            ),
        ),
    )
    assert "30.08.2026" in text  # начало полной скорости
    assert "29.09.2026" in text  # конец доступа
    assert "Доплаты разницы нет" in text
    assert "Ничего не сгорает" in text
    assert "неактивна" not in text


def test_plan_confirm_promises_no_downgrade_right_now() -> None:
    """Дешёвый тариф поверх полного не понижает скорость сейчас — так и говорим."""
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    current_end = now + timedelta(days=20)
    plan = find_plan("week_delayed")
    text = plan_confirm_text(
        plan,
        _draft(
            plan.code,
            starts_at=current_end,
            starts_now=False,
            access_until=current_end + timedelta(days=7),
            schedule_before=(ScheduleEntry(tier="full", starts_at=now, ends_at=current_end),),
        ),
    )
    assert "Полная скорость сохраняется до 04.09.2026" in text
    assert "понижения" in text


def test_plan_confirm_for_fresh_purchase_names_only_the_end() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    plan = find_plan("month_full")
    text = plan_confirm_text(
        plan,
        _draft(plan.code, starts_at=now, starts_now=True, access_until=now + timedelta(days=30)),
    )
    assert "14.09.2026" in text
    assert "полная скорость всё время" in text
    assert "Бесплатный период" not in text  # без триала лишнего абзаца нет


def test_plan_confirm_warns_about_trial_days_burning() -> None:
    """Оплата во время триала обнуляет его остаток — предупреждаем заранее."""
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    plan = find_plan("month_full")
    text = plan_confirm_text(
        plan,
        _draft(
            plan.code,
            starts_at=now,
            starts_now=True,
            access_until=now + timedelta(days=30),
            trial_expires_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
        ),
    )
    assert "18.08.2026" in text
    assert "не приплюсуются" in text


# --- вход без доступа -------------------------------------------------------


def _callback(data: str) -> CallbackQuery:
    """Апдейт-кнопка без похода в Telegram: _is_entry смотрит только на data."""
    return CallbackQuery.model_construct(data=data)


def test_plan_button_is_an_entry_point_for_a_user_without_access() -> None:
    """Нажатие на тариф не должно упираться в пейвол.

    Поймано 29.08.2026: /subscribe — входная команда, поэтому меню тарифов
    открывалось всем, а callback выбора плана middleware резал как обычный
    апдейт → человек без доступа получал «Доступ закончился… Продлить:
    /subscribe» ровно в ответ на попытку продлить. Оплатить не мог тот, кому
    это нужнее всего; на тестовом аккаунте с доступом баг не воспроизводился.
    """
    for plan in PLANS:
        assert AccessMiddleware._is_entry(_callback(PlanCB(code=plan.code).pack())) is True


def test_ordinary_callback_still_requires_access() -> None:
    """Расширение списка входных префиксов не должно открыть бота целиком."""
    assert AccessMiddleware._is_entry(_callback(BrandCB(brand_id=7).pack())) is False
