"""Пауза бота: /pause, /play, исключение из рассылки и ежедневное напоминание.

Решения владельца 31.10.2026 (`docs/planning/PUSH_PIPELINE_PLAN.md`): пауза —
до /play, без сроков; раз в день в обед — «вы на паузе» с кнопкой; на паузе из
worker приходят только это напоминание и напоминание о продлении.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import Chat, Message, User
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.bot.db as botdb
import app.worker as worker
from app.bot.access import AccessMiddleware
from app.bot.messages import status_text
from app.db.base import Base
from app.models import BotUser, UserEvent
from app.services.engagement import filter_nudge_candidates
from app.services.events import EVENT_PAUSED, EVENT_PAUSE_REMINDER, EVENT_RESUMED
from app.services.telegram import SendOutcome

TZ = "Asia/Bishkek"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        scheduler_timezone=TZ,
        bot_active_start_hour=0,
        bot_active_end_hour=24,
        bot_pause_reminder_hour=13,
        bot_new_lookback_minutes=180,
        trial_enabled=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def sessions(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = _settings()
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr("app.bot.messages.get_settings", lambda: settings)
    monkeypatch.setattr(botdb, "SessionLocal", factory)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    return factory


def _user(session, telegram_id: int, **kwargs) -> BotUser:
    user = BotUser(telegram_id=telegram_id, access_status=kwargs.pop("access_status", "member"), **kwargs)
    session.add(user)
    session.flush()
    return user


def _events(session, name: str) -> list[UserEvent]:
    return list(session.scalars(select(UserEvent).where(UserEvent.event == name)).all())


# --- /pause и /play ------------------------------------------------------------


def test_pause_and_play_round_trip(sessions) -> None:
    with sessions() as s:
        _user(s, 1)
        s.commit()

    first = botdb.set_paused(1, True)
    assert first.changed and first.paused_at is not None
    again = botdb.set_paused(1, True)
    # Повтор не сдвигает начало паузы и не пишет второе событие.
    assert not again.changed
    assert again.paused_at.replace(tzinfo=None) == first.paused_at.replace(tzinfo=None)

    resumed = botdb.set_paused(1, False)
    assert resumed.changed
    assert not botdb.set_paused(1, False).changed  # «и так включено»

    with sessions() as s:
        user = s.scalar(select(BotUser).where(BotUser.telegram_id == 1))
        assert user.paused_at is None
        assert user.is_active  # пауза ≠ «заблокировал бота»
        assert len(_events(s, EVENT_PAUSED)) == 1
        resumed_events = _events(s, EVENT_RESUMED)
        assert len(resumed_events) == 1
        assert "paused_hours" in resumed_events[0].payload


def test_pause_for_unknown_user_returns_none(sessions) -> None:
    assert botdb.set_paused(404, True) is None
    assert botdb.set_paused(404, False) is None


def test_status_shows_pause_first(sessions) -> None:
    paused_at = datetime(2026, 10, 31, 8, 5, tzinfo=timezone.utc)  # 14:05 в Бишкеке
    text = status_text(access_status="member", expires_at=None, active=False, paused_at=paused_at)
    assert text.startswith("⏸ Уведомления на паузе с 31.10 14:05")
    assert "/play" in text
    assert "закрытой группы" in text  # статус доступа никуда не делся
    plain = status_text(access_status="member", expires_at=None, active=False)
    assert "⏸" not in plain


@pytest.mark.parametrize("text", ["/pause", "/play"])
def test_pause_commands_work_without_access(text: str) -> None:
    """Свои уведомления можно включить/выключить всегда — это входные команды."""
    message = Message(
        message_id=1,
        date=_now(),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="x"),
        text=text,
    )
    assert AccessMiddleware._is_entry(message)


# --- исключение из рассылки и касаний -----------------------------------------


def test_paused_user_gets_no_nudges(sessions) -> None:
    now = _now()
    trial = dict(
        access_status="trial",
        trial_started_at=now - timedelta(minutes=30),
        trial_expires_at=now + timedelta(days=3),
    )
    with sessions() as s:
        _user(s, 1, **trial)
        _user(s, 2, paused_at=now - timedelta(minutes=5), **trial)
        s.commit()
        assert {u.telegram_id for u in filter_nudge_candidates(s, 1, now)} == {1}


# --- ежедневное напоминание -----------------------------------------------------


def _freeze_local(monkeypatch, hour: int, minute: int = 0) -> datetime:
    """Подменяет «сейчас» в worker на сегодня hour:minute по Бишкеку."""
    local = datetime.now(ZoneInfo(TZ)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    monkeypatch.setattr(worker, "local_now", lambda tz: local)
    return local


def _capture(monkeypatch, outcome=SendOutcome.DELIVERED) -> list[tuple[int, dict]]:
    sent: list[tuple[int, dict]] = []

    def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, kwargs.get("reply_markup")))
        return outcome

    monkeypatch.setattr(worker, "send_message", fake_send)
    return sent


def test_pause_reminder_once_a_day_after_lunch(sessions, monkeypatch) -> None:
    local = _freeze_local(monkeypatch, 13, 30)
    yesterday = (local - timedelta(days=1)).astimezone(timezone.utc)
    with sessions() as s:
        _user(s, 1, paused_at=yesterday)  # ждёт напоминания
        _user(s, 2, paused_at=local.astimezone(timezone.utc) - timedelta(minutes=10))  # сегодня
        _user(s, 3)  # не на паузе
        _user(s, 4, paused_at=yesterday, access_status="revoked")  # доступа нет
        _user(s, 5, paused_at=yesterday, is_active=False)  # заблокировал бота
        s.commit()

    sent = _capture(monkeypatch)
    assert worker.send_pause_reminders() == 1
    assert [chat_id for chat_id, _ in sent] == [1]
    assert sent[0][1]["inline_keyboard"][0][0]["callback_data"] == "play:resume"

    assert worker.send_pause_reminders() == 0  # второго за день нет
    with sessions() as s:
        assert len(_events(s, EVENT_PAUSE_REMINDER)) == 1
        user = s.scalar(select(BotUser).where(BotUser.telegram_id == 1))
        assert user.pause_reminded_on == local.date()


def test_pause_reminder_waits_for_lunch(sessions, monkeypatch) -> None:
    local = _freeze_local(monkeypatch, 12, 59)
    with sessions() as s:
        _user(s, 1, paused_at=(local - timedelta(days=2)).astimezone(timezone.utc))
        s.commit()
    sent = _capture(monkeypatch)
    assert worker.send_pause_reminders() == 0
    assert sent == []


def test_pause_reminder_repeats_next_day(sessions, monkeypatch) -> None:
    local = _freeze_local(monkeypatch, 14)
    with sessions() as s:
        _user(
            s,
            1,
            paused_at=(local - timedelta(days=3)).astimezone(timezone.utc),
            pause_reminded_on=(local - timedelta(days=1)).date(),
        )
        s.commit()
    _capture(monkeypatch)
    assert worker.send_pause_reminders() == 1


def test_pause_reminder_retries_after_temporary_failure(sessions, monkeypatch) -> None:
    local = _freeze_local(monkeypatch, 13)
    with sessions() as s:
        _user(s, 1, paused_at=(local - timedelta(days=1)).astimezone(timezone.utc))
        s.commit()
    _capture(monkeypatch, outcome=SendOutcome.FAILED)
    assert worker.send_pause_reminders() == 0
    with sessions() as s:
        assert s.scalar(select(BotUser.pause_reminded_on)) is None  # следующим циклом — снова
    _capture(monkeypatch)
    assert worker.send_pause_reminders() == 1


def test_renewal_reminder_still_reaches_paused_subscriber(sessions, monkeypatch) -> None:
    """Напоминание о продлении — про деньги, пауза его не глушит."""
    settings = _settings(subscription_enabled=True, subscription_reminder_days_before=3)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    with sessions() as s:
        _user(
            s,
            1,
            access_status="subscriber",
            subscription_expires_at=_now() + timedelta(days=2),
            paused_at=_now() - timedelta(days=5),
        )
        s.commit()
    sent: list[int] = []
    monkeypatch.setattr(
        worker, "send_message", lambda chat_id, text, **kw: sent.append(chat_id) or SendOutcome.DELIVERED
    )
    worker.send_renewal_reminders_if_due(None)
    assert sent == [1]
