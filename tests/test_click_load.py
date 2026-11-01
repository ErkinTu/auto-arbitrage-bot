"""Защита от частых нажатий (план 01.11.2026, п. К).

Гонки проверяются одновременными нажатиями через `asyncio.gather`: до правки
лимит альбомов засчитывался ПОСЛЕ отправки, и нажатия внахлёст проходили все.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramRetryAfter

import app.bot.db as botdb
from app.bot import handlers, notifier, photos, showcase, throttle
from app.bot.keyboards import BestCB, DigestCB, PhotosCB, PlanCB
from app.services.plans import PLANS
from tests.test_all_photos import TWO, STALE, _AlbumMessage, _gallery
from tests.test_rich_cards import (
    _HandlerMessage,
    _Query,
    _run_start,
    _showcase_page,
    _start_info,
)

# --- примитивы ---------------------------------------------------------------------


def test_token_bucket_burst_then_rate() -> None:
    bucket = throttle.TokenBucket(burst=3, rate=1.0)
    assert [bucket.allow(1, now=0.0) for _ in range(4)] == [True, True, True, False]
    assert bucket.allow(2, now=0.0)  # другой человек — своё ведро
    assert not bucket.allow(1, now=0.5)
    assert bucket.allow(1, now=1.6)  # за 1.6 с накапал токен
    assert throttle.TokenBucket(burst=0, rate=1.0).allow(1)  # выключатель


def test_rate_window_cooldown_and_cap() -> None:
    window = throttle.RateWindow(min_interval=5.0, max_count=3, window=600.0)
    assert window.wait("a", now=0.0) == 0
    assert window.wait("a", now=2.0) == pytest.approx(3.0)  # кулдаун
    assert window.wait("a", now=5.0) == 0
    assert window.wait("a", now=10.0) == 0
    # Три за окно — четвёртое ждёт, пока выйдет первое (600 с от 0).
    assert window.wait("a", now=20.0) == pytest.approx(580.0)
    assert window.wait("b", now=20.0) == 0
    window.forget("b")
    assert window.wait("b", now=21.0) == 0  # отменённое не считается


def test_exclusive_blocks_second_operation_of_the_same_key() -> None:
    with throttle.exclusive("x") as first:
        with throttle.exclusive("x") as second, throttle.exclusive("y") as other:
            assert first and not second and other
    with throttle.exclusive("x") as again:
        assert again  # освободилось


def test_middleware_drops_flood_and_tells_once(monkeypatch) -> None:
    middleware = throttle.ThrottlingMiddleware(burst=2, rate=0.001)
    handled: list[int] = []

    async def handler(event, data):
        handled.append(1)

    class _Callback(SimpleNamespace):
        async def answer(self, text=None, **kwargs):
            self.alerts.append(text)

    # Проверка `isinstance(event, CallbackQuery)` — на наш дублёр.
    monkeypatch.setattr(throttle, "CallbackQuery", _Callback)
    query = _Callback(from_user=SimpleNamespace(id=5), alerts=[])

    async def press(times: int) -> None:
        for _ in range(times):
            await middleware(handler, query, {})

    asyncio.run(press(5))
    assert len(handled) == 2
    # Всплывашка «слишком часто» — одна, дальше молча (без «часиков»).
    assert query.alerts == [throttle.TOO_FAST_TEXT, None, None]


# --- «Все фото» ---------------------------------------------------------------------


def _wire_photos(monkeypatch, gallery, fetched=None, delay: float = 0.0):
    notifier._photo_ids.clear()  # кэш file_id из других тестов подменил бы ссылки
    fetch_calls: list[str] = []
    monkeypatch.setattr(handlers.db, "listing_gallery", lambda listing_id: gallery)
    monkeypatch.setattr(handlers.db, "save_gallery", lambda listing_id, urls: None)
    monkeypatch.setattr(handlers.db, "save_seller_phone", lambda listing_id, value: None)

    def fake_fetch(source_name, external_id, url, **kwargs):
        fetch_calls.append(kwargs.get("timeout"))
        time.sleep(delay)
        return photos.Card(urls=fetched or [])

    monkeypatch.setattr(handlers.photos, "fetch_card", fake_fetch)
    return fetch_calls


def test_concurrent_presses_cannot_overrun_album_limit(monkeypatch) -> None:
    """Гонка п. К1: 5 одновременных нажатий по РАЗНЫМ машинам при лимите 2."""
    monkeypatch.setattr(handlers.get_settings(), "bot_photos_albums_per_hour", 2)
    _wire_photos(monkeypatch, _gallery(TWO, last_seen_at=STALE), fetched=TWO, delay=0.05)
    message = _AlbumMessage()

    async def burst() -> None:
        await asyncio.gather(
            *(
                handlers.photos_cb(_Query(message), PhotosCB(listing_id=listing_id))
                for listing_id in range(1, 6)
            )
        )

    asyncio.run(burst())
    # Одна операция на человека: остальные нажатия внахлёст отбиты «ещё отправляю».
    assert len(message.albums) == 1
    assert len(photos._albums_sent[777]) == 1


def test_sequential_presses_respect_limit_via_reservation(monkeypatch) -> None:
    monkeypatch.setattr(handlers.get_settings(), "bot_photos_albums_per_hour", 2)
    _wire_photos(monkeypatch, _gallery(TWO))
    message = _AlbumMessage()
    for listing_id in range(1, 4):
        asyncio.run(handlers.photos_cb(_Query(message), PhotosCB(listing_id=listing_id)))
    assert len(message.albums) == 2
    assert message.answers[-1][0].startswith("За час можно открыть 2 альбома")


def test_failed_album_returns_the_reserved_slot(monkeypatch) -> None:
    _wire_photos(monkeypatch, _gallery([], source_name="mashina.kg"))  # фото нет
    message = _AlbumMessage()
    asyncio.run(handlers.photos_cb(_Query(message), PhotosCB(listing_id=42)))
    assert message.answers[0][0] == handlers.PHOTOS_MISSING_TEXT
    assert photos._albums_sent == {}


def test_same_listing_again_says_photos_are_above(monkeypatch) -> None:
    _wire_photos(monkeypatch, _gallery(TWO))
    message = _AlbumMessage()
    query = _Query(message)
    asyncio.run(handlers.photos_cb(query, PhotosCB(listing_id=42)))
    photos._recent.clear()  # дебаунс 10 с прошёл
    asyncio.run(handlers.photos_cb(query, PhotosCB(listing_id=42)))
    assert len(message.albums) == 1
    assert query.alerts[-1] == handlers.PHOTOS_SHOWN_TEXT


def test_live_check_is_cached_and_uses_short_timeout(monkeypatch) -> None:
    calls = _wire_photos(monkeypatch, _gallery(TWO, last_seen_at=STALE, seller_phone="996500328668"), fetched=TWO)
    for chat in (1, 2):  # второй человек — то же объявление
        message = _AlbumMessage()
        query = _Query(message)
        query.from_user = SimpleNamespace(id=chat)
        asyncio.run(handlers.photos_cb(query, PhotosCB(listing_id=42)))
        assert message.albums == [TWO]
    # Источник спрошен один раз, с таймаутом нажатия, а не общим 20 с.
    assert calls == [handlers.get_settings().bot_click_timeout_seconds]


def test_slow_source_does_not_starve_db_threads(monkeypatch) -> None:
    """Главная точка п. К: запросы к источнику — в своём пуле, база отвечает."""
    from app.bot import executors

    release = threading.Event()
    monkeypatch.setattr(executors, "_click_pool", None)
    monkeypatch.setattr(handlers.get_settings(), "bot_click_source_threads", 2)

    async def scenario() -> float:
        stuck = [asyncio.ensure_future(executors.run_click(release.wait, 5)) for _ in range(6)]
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await asyncio.to_thread(lambda: None)  # «запрос к базе»
        elapsed = time.monotonic() - started
        release.set()
        await asyncio.gather(*stuck)
        return elapsed

    assert asyncio.run(scenario()) < 1.0


# --- витрина ---------------------------------------------------------------------


def _wire_showcase(monkeypatch):
    calls: list[int] = []

    def get_showcase(chat_id, offset, skip_seen=False):
        calls.append(offset)
        return _showcase_page(3), 3

    monkeypatch.setattr(handlers.db, "get_showcase", get_showcase)
    monkeypatch.setattr("app.bot.notifier.INTERACTIVE_DELAY_SECONDS", 0)
    return calls


def test_best_cooldown_and_cap(monkeypatch) -> None:
    calls = _wire_showcase(monkeypatch)
    settings = handlers.get_settings()
    monkeypatch.setattr(settings, "bot_showcase_cooldown_seconds", 5.0)
    monkeypatch.setattr(settings, "bot_showcase_pages_per_10min", 2)
    message = _HandlerMessage()
    asyncio.run(handlers.cmd_best(message))
    asyncio.run(handlers.cmd_best(message))  # сразу — молча
    assert calls == [0]
    rate = handlers._showcase_rate()
    rate._hits[777] = [t - 6 for t in rate._hits[777]]  # кулдаун прошёл
    asyncio.run(handlers.cmd_best(message))
    assert calls == [0, 0]
    rate._hits[777] = [t - 6 for t in rate._hits[777]]
    answers_before = len(message.answers)
    asyncio.run(handlers.cmd_best(message))  # потолок за 10 минут
    assert calls == [0, 0]
    assert message.answers[answers_before][0].startswith("Вы уже посмотрели всё лучшее")


def test_best_more_double_tap_sends_once_and_keeps_button(monkeypatch) -> None:
    calls = _wire_showcase(monkeypatch)
    message = _HandlerMessage()
    first, second = _Query(message), _Query(message)

    async def taps() -> None:
        await asyncio.gather(
            handlers.cb_best_more(first, BestCB(offset=3)),
            handlers.cb_best_more(second, BestCB(offset=3)),
        )

    asyncio.run(taps())
    assert calls == [3]
    assert second.alerts == [throttle.TOO_FAST_TEXT]


def test_start_showcase_not_repeated_within_minutes(monkeypatch) -> None:
    first = _run_start(monkeypatch, _start_info(trial_active=True), _showcase_page(3))
    assert len(first.bot.photos) == 3
    again = _run_start(monkeypatch, _start_info(), _showcase_page(3))
    assert again.bot.photos == []
    # Без витрины кнопка триала всё равно предлагается — не «примера нет».
    from app.bot.messages import START_TRIAL_PROMPT_TEXT

    assert again.answers[-1][0] == START_TRIAL_PROMPT_TEXT


def test_expired_showcase_is_served_stale_while_another_recomputes(monkeypatch) -> None:
    """Пересчёт не держит замок для всех: истёкший кэш отдаётся прошлым."""
    stale = _showcase_page(3)
    monkeypatch.setattr(showcase, "_cache", (0.0, stale))
    monkeypatch.setattr(showcase, "_cache_ready", True)
    computing = threading.Event()
    release = threading.Event()

    def slow_compute(session, **kwargs):
        computing.set()
        release.wait(5)
        return _showcase_page(1)

    monkeypatch.setattr(showcase, "compute_showcase", slow_compute)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    warm = threading.Thread(
        target=showcase.get_showcase, args=(_Session,), kwargs={"min_samples": 5, "refresh": True}
    )
    warm.start()
    assert computing.wait(5)
    started = time.monotonic()
    served = showcase.get_showcase(_Session, min_samples=5)
    assert time.monotonic() - started < 1.0
    assert [item.listing_id for item in served] == [item.listing_id for item in stale]
    release.set()
    warm.join(5)
    assert len(showcase.get_showcase(_Session, min_samples=5)) == 1


# --- дайджест ---------------------------------------------------------------------


def test_digest_double_tap_sends_once(monkeypatch) -> None:
    pages: list[int] = []

    def digest_page(telegram_id, batch, offset):
        pages.append(offset)
        return botdb.DigestPage(status=botdb.DIGEST_OK, notes=_showcase_page(3))

    monkeypatch.setattr(handlers.db, "digest_page", digest_page)
    monkeypatch.setattr("app.bot.notifier.INTERACTIVE_DELAY_SECONDS", 0)
    message = _HandlerMessage()

    async def taps() -> None:
        await asyncio.gather(
            handlers.digest_more_cb(_Query(message), DigestCB(batch=9, offset=0)),
            handlers.digest_more_cb(_Query(message), DigestCB(batch=9, offset=0)),
        )

    asyncio.run(taps())
    assert pages == [0]


# --- оплата ---------------------------------------------------------------------


def _wire_payment(monkeypatch, reusable=True):
    created: list[str] = []
    inits: list[str] = []
    settings = handlers.get_settings()
    monkeypatch.setattr(settings.__class__, "subscription_allowed_for", lambda self, tid: True)

    def create(telegram_id, plan_code):
        order = f"order-{len(created)}"
        created.append(order)
        now = botdb._utcnow()
        return botdb.PaymentDraft(
            ok=True,
            plan_code=plan_code,
            order_id=order,
            starts_at=now,
            starts_now=True,
            access_until=now + timedelta(days=30),
        )

    def init_payment(*, order_id, amount, description):
        inits.append(order_id)
        return SimpleNamespace(ok=True, redirect_url=f"https://pay/{order_id}")

    monkeypatch.setattr(handlers.db, "create_pending_payment", create)
    monkeypatch.setattr(handlers.db, "payment_reusable", lambda order_id, draft: reusable)
    monkeypatch.setattr(handlers, "init_payment", init_payment)
    return created, inits


def _press_plan(code: str) -> _HandlerMessage:
    message = _HandlerMessage()
    asyncio.run(handlers.cb_plan_selected(_Query(message), PlanCB(code=code)))
    return message


def test_same_plan_again_reuses_the_payment_link(monkeypatch) -> None:
    created, inits = _wire_payment(monkeypatch)
    code = PLANS[0].code
    first = _press_plan(code)
    second = _press_plan(code)
    assert created == ["order-0"] and inits == ["order-0"]
    url = lambda m: m.answers[-1][1].inline_keyboard[0][0].url  # noqa: E731
    assert url(first) == url(second) == "https://pay/order-0"
    _press_plan(PLANS[1].code)  # другой тариф — свой платёж
    assert created == ["order-0", "order-1"]


def test_paid_or_moved_payment_is_not_reused(monkeypatch) -> None:
    created, _ = _wire_payment(monkeypatch, reusable=False)
    code = PLANS[0].code
    _press_plan(code)
    _press_plan(code)
    assert created == ["order-0", "order-1"]


def test_concurrent_plan_taps_create_one_payment(monkeypatch) -> None:
    created, _ = _wire_payment(monkeypatch)
    message = _HandlerMessage()
    code = PLANS[0].code

    async def taps() -> None:
        await asyncio.gather(
            *(handlers.cb_plan_selected(_Query(message), PlanCB(code=code)) for _ in range(3))
        )

    asyncio.run(taps())
    assert created == ["order-0"]


def test_payment_reusable_checks_status_and_dates(monkeypatch) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    from app.models import BotUser, Payment

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(botdb, "SessionLocal", factory)
    with factory() as session:
        user = BotUser(telegram_id=1)
        session.add(user)
        session.commit()
    draft = botdb.create_pending_payment(1, plan_code=PLANS[0].code)
    assert botdb.payment_reusable(draft.order_id, draft)
    with factory() as session:  # оплатили что-то ещё — хвост сдвинулся
        user = session.query(BotUser).one()
        user.subscription_expires_at = botdb._utcnow() + timedelta(days=7)
        session.commit()
    assert not botdb.payment_reusable(draft.order_id, draft)
    with factory() as session:
        user = session.query(BotUser).one()
        user.subscription_expires_at = None
        session.query(Payment).one().status = "paid"
        session.commit()
    assert not botdb.payment_reusable(draft.order_id, draft)


# --- 429 на ответ нажатию -----------------------------------------------------------


class _FloodedBot:
    def __init__(self) -> None:
        self.calls = 0

    async def send_message(self, chat_id, text, **kwargs):
        self.calls += 1
        raise TelegramRetryAfter(method=SimpleNamespace(), message="flood", retry_after=30)


def test_interactive_send_is_not_retried_on_429(monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(notifier.asyncio, "sleep", fake_sleep)
    bot = _FloodedBot()
    sent = asyncio.run(
        notifier.send_notifications(bot, _showcase_page(1), interactive=True)
    )
    assert sent == 0 and bot.calls == 1 and 30 not in slept
