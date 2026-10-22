"""Онбординг и конверсия (разбор 2026-10-21): пресеты, триал+фильтр, витрина,
напоминания, итоги триала, разовые рассылки.

Главные инварианты: группа марок и «не старше N лет» матчатся честно (и не
устаревают), базовый фильтр не трогает чужие фильтры, напоминания не уходят
старым пользователям и не повторяются, нулевые итоги не показываются.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.bot.campaigns as campaigns
import app.bot.db as botdb
import app.bot.showcase as showcase
import app.worker as worker
from app.bot.matching import (
    PRIORITY_SPECIFIC,
    filter_matches,
    filter_priority,
    match_new_listings,
)
from app.db.base import Base
from app.models import (
    BotUser,
    Brand,
    CarModel,
    DailyMarketStat,
    Listing,
    ListingStatusHistory,
    SentNotification,
    Source,
    UserEvent,
    UserFilter,
)
from app.services.engagement import (
    EVENT_TRIAL_SUMMARY,
    filter_nudge_candidates,
    nudge_event,
    trial_nudge_candidates,
    trial_stats,
    trial_summary_candidates,
)
from app.services.events import EVENT_START
from app.services.filter_presets import (
    BASE_PRESET_CODE,
    load_brand_groups,
    min_year_for_age,
    presets,
)
from app.services.regions import LOCAL
from app.services.telegram import SendOutcome


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        trial_days=3,
        trial_enabled=True,
        bot_max_filters_per_user=5,
        bot_base_filter_percent=20,
        bot_below_avg_min_samples=2,
        bot_below_avg_factor=0.95,
        bot_max_below_market_percent=50.0,
        bot_require_complete_listings=True,
        scheduler_timezone="Asia/Bishkek",
        bot_active_start_hour=0,
        bot_active_end_hour=24,
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
    for module in (botdb, worker, campaigns):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.services.filter_presets.get_settings", lambda: settings)
    monkeypatch.setattr("app.bot.messages.get_settings", lambda: settings)
    monkeypatch.setattr(botdb, "SessionLocal", factory)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(showcase, "_cache", (0.0, []))
    return factory


def _catalog(session) -> dict[str, int]:
    """Toyota/Honda (японцы) и BMW, по модели на марку; id марки по имени."""
    session.add(Source(name="mashina.kg"))
    ids = {}
    for name in ("Toyota", "Honda", "BMW"):
        brand = Brand(name=name)
        session.add(brand)
        session.flush()
        session.add(CarModel(brand_id=brand.id, name=f"{name}-model"))
        ids[name] = brand.id
    session.flush()
    return ids


def _model_id(session, brand_id: int) -> int:
    return session.scalar(select(CarModel.id).where(CarModel.brand_id == brand_id))


def _listing(
    session,
    brand_id: int,
    *,
    external_id: str,
    price: str = "8000",
    year: int = 2020,
    city: str = "Бишкек",
    published_hours_ago: float = 1,
    event: bool = False,
) -> Listing:
    listing = Listing(
        source_id=session.scalar(select(Source.id)),
        external_id=external_id,
        brand_id=brand_id,
        car_model_id=_model_id(session, brand_id),
        title=f"car {external_id}",
        source_slug=f"car-{external_id}",
        year=year,
        mileage_km=90_000,
        city=city,
        current_price_usd=Decimal(price),
        is_active=True,
        analytics_eligible=True,
        source_created_at=_now() - timedelta(hours=published_hours_ago),
        payload_hash="x" * 8,
    )
    session.add(listing)
    session.flush()
    if event:
        session.add(
            ListingStatusHistory(
                listing_id=listing.id,
                event_type="new",
                old_status=None,
                new_status="active",
                observed_at=_now(),
            )
        )
    return listing


def _stat(session, brand_id: int, *, avg: str = "10000", year: int = 2020) -> None:
    session.add(
        DailyMarketStat(
            stat_date=date.today(),
            brand_id=brand_id,
            car_model_id=_model_id(session, brand_id),
            year=year,
            mileage_bucket="50-100k",
            customs_status="unknown",
            avg_price_usd=Decimal(avg),
            median_price_usd=Decimal(avg),
            min_price_usd=Decimal(avg),
            max_price_usd=Decimal(avg),
            sample_count=5,
        )
    )


def _user(session, telegram_id: int, **kwargs) -> BotUser:
    user = BotUser(telegram_id=telegram_id, **kwargs)
    session.add(user)
    session.flush()
    return user


# --------------------------------------------------------------------------
# Пресеты: группа марок и возраст
# --------------------------------------------------------------------------


def test_brand_group_matches_only_its_brands(sessions) -> None:
    with sessions() as s:
        ids = _catalog(s)
        groups = load_brand_groups(s)
        japan = UserFilter(brand_group="japan")
        assert filter_matches(_listing(s, ids["Toyota"], external_id="t"), japan, groups)
        assert filter_matches(_listing(s, ids["Honda"], external_id="h"), japan, groups)
        assert not filter_matches(_listing(s, ids["BMW"], external_id="b"), japan, groups)
        # Без карты групп — не доказано, значит не шлём.
        assert not filter_matches(_listing(s, ids["Toyota"], external_id="t2"), japan)


def test_max_age_slides_with_the_calendar(sessions) -> None:
    with sessions() as s:
        ids = _catalog(s)
        young = UserFilter(max_age_years=10)
        in_2026 = datetime(2026, 10, 21, tzinfo=timezone.utc)
        in_2027 = datetime(2027, 10, 21, tzinfo=timezone.utc)
        car_2016 = _listing(s, ids["Toyota"], external_id="a", year=2016)
        assert min_year_for_age(10, in_2026) == 2016
        assert filter_matches(car_2016, young, now=in_2026)
        # Через год та же машина уже старше 10 лет — фильтр не «застыл» на 2016.
        assert not filter_matches(car_2016, young, now=in_2027)


def test_brand_group_filter_has_specific_priority() -> None:
    assert filter_priority(UserFilter(brand_group="japan", below_market_only=False)) == (
        PRIORITY_SPECIFIC
    )


def test_presets_are_at_most_four_and_all_below_market() -> None:
    import app.services.filter_presets as fp

    original = fp.get_settings
    fp.get_settings = lambda: _settings()
    try:
        items = presets()
    finally:
        fp.get_settings = original
    assert len(items) <= 4
    assert items[0].code == BASE_PRESET_CODE and items[0].below_market_percent == 20
    assert all(item.below_market_percent > 0 for item in items)


def test_preset_filter_created_and_duplicate_refused(sessions) -> None:
    with sessions() as s:
        _user(s, 1)
        s.commit()
    created = botdb.create_preset_filter(1, "japan_10y")
    assert created.brand_group == "japan" and created.max_age_years == 10
    assert created.region == LOCAL and created.below_market_only
    with pytest.raises(botdb.DuplicateFilterError):
        botdb.create_preset_filter(1, "japan_10y")
    with pytest.raises(LookupError):
        botdb.create_preset_filter(1, "no-such-preset")


def test_base_filter_is_set_only_when_user_has_none(sessions) -> None:
    with sessions() as s:
        user = _user(s, 1)
        _user(s, 2)
        s.add(UserFilter(user_id=user.id, below_market_only=False))
        s.commit()
    assert botdb.ensure_base_filter(1) is None  # свои фильтры не трогаем
    base = botdb.ensure_base_filter(2)
    assert base is not None and base.below_market_percent == 20 and base.region == LOCAL
    assert botdb.ensure_base_filter(2) is None  # повторное нажатие — без дубля


def test_matching_stores_percent_and_honours_group(sessions) -> None:
    with sessions() as s:
        ids = _catalog(s)
        user = _user(s, 7, access_status="member")
        s.add(
            UserFilter(
                user_id=user.id,
                brand_group="japan",
                below_market_only=True,
                below_market_percent=10,
                created_at=_now() - timedelta(hours=1),
            )
        )
        _stat(s, ids["Toyota"])
        _stat(s, ids["BMW"])
        _listing(s, ids["Toyota"], external_id="t", price="8000", event=True)
        _listing(s, ids["BMW"], external_id="b", price="8000", event=True)
        s.commit()
        pending = match_new_listings(
            s, _now() - timedelta(hours=2), factor=0.95, min_samples=2
        )
        assert [p.title for p in pending] == ["car t"]
        row = s.scalar(select(SentNotification))
        assert row.below_market_percent == Decimal("20.0")


# --------------------------------------------------------------------------
# Витрина «лучшие сейчас»
# --------------------------------------------------------------------------


def test_showcase_sorted_and_drops_implausible_or_stale(sessions) -> None:
    with sessions() as s:
        ids = _catalog(s)
        _stat(s, ids["Toyota"])
        _listing(s, ids["Toyota"], external_id="10", price="9000")  # 10%
        _listing(s, ids["Toyota"], external_id="30", price="7000")  # 30%
        _listing(s, ids["Toyota"], external_id="80", price="2000")  # 80% — мусор
        _listing(s, ids["Toyota"], external_id="old", price="6000", published_hours_ago=48)
        _listing(s, ids["Toyota"], external_id="cn", price="6000", city="Китай")
        s.commit()
        items = showcase.compute_showcase(s, min_samples=2)
    assert [item.title for item in items] == ["car 30", "car 10"]

    page, next_offset = showcase.page_for_chat(items, chat_id=5, offset=0, page=1)
    assert page[0].chat_id == 5 and next_offset == 1
    assert showcase.page_for_chat(items, chat_id=5, offset=1, page=1)[1] is None


# --------------------------------------------------------------------------
# Напоминания и итоги
# --------------------------------------------------------------------------


def _started(s, telegram_id: int, minutes_ago: float, **kwargs) -> BotUser:
    user = _user(s, telegram_id, access_status="revoked", **kwargs)
    s.add(
        UserEvent(
            user_id=user.id,
            event=EVENT_START,
            created_at=_now() - timedelta(minutes=minutes_ago),
        )
    )
    return user


def test_trial_nudge_windows(sessions) -> None:
    now = _now()
    with sessions() as s:
        too_early = _started(s, 1, minutes_ago=5)
        due_first = _started(s, 2, minutes_ago=30)
        due_second = _started(s, 3, minutes_ago=25 * 60)
        ancient = _started(s, 4, minutes_ago=90 * 24 * 60)
        took_trial = _started(s, 5, minutes_ago=30, trial_started_at=now)
        s.add(UserEvent(user_id=due_second.id, event=nudge_event("trial", 1)))
        s.commit()

        first = {u.telegram_id for u in trial_nudge_candidates(s, 1, now)}
        second = {u.telegram_id for u in trial_nudge_candidates(s, 2, now)}
    assert first == {due_first.telegram_id}
    assert second == {due_second.telegram_id}
    assert too_early.telegram_id not in first | second
    assert ancient.telegram_id not in first | second  # старым — не напоминаем
    assert took_trial.telegram_id not in first


def test_send_nudges_once_and_with_button(sessions, monkeypatch) -> None:
    sent = []

    def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, kwargs.get("reply_markup")))
        return SendOutcome.DELIVERED

    monkeypatch.setattr(worker, "send_message", fake_send)
    with sessions() as s:
        _started(s, 1, minutes_ago=30)
        trial_user = _user(
            s,
            2,
            access_status="trial",
            trial_started_at=_now() - timedelta(minutes=30),
            trial_expires_at=_now() + timedelta(days=3),
        )
        s.commit()
        assert trial_user.id

    assert worker.send_nudges() == 2
    assert worker.send_nudges() == 0  # журнал не даёт повторить
    buttons = {chat_id: markup["inline_keyboard"][0][0]["callback_data"] for chat_id, markup in sent}
    assert buttons[1].startswith("trial:")  # триал + базовый фильтр
    assert buttons[2] == f"preset:{BASE_PRESET_CODE}"


def test_filter_nudge_skips_users_with_filters(sessions) -> None:
    now = _now()
    with sessions() as s:
        trial = dict(
            access_status="trial",
            trial_started_at=now - timedelta(minutes=30),
            trial_expires_at=now + timedelta(days=3),
        )
        _user(s, 1, **trial)
        with_filter = _user(s, 2, **trial)
        s.add(UserFilter(user_id=with_filter.id))
        s.commit()
        assert {u.telegram_id for u in filter_nudge_candidates(s, 1, now)} == {1}


def test_trial_summary_counts_and_skips_zero(sessions, monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(
        worker,
        "send_message",
        lambda chat_id, text, **kw: sent.append((chat_id, text)) or SendOutcome.DELIVERED,
    )
    now = _now()
    ending = dict(
        access_status="trial",
        trial_started_at=now - timedelta(days=3) + timedelta(hours=6),
        trial_expires_at=now + timedelta(hours=6),
    )
    with sessions() as s:
        ids = _catalog(s)
        active = _user(s, 1, **ending)
        silent = _user(s, 2, **ending)
        _user(s, 3, access_status="trial", trial_started_at=now, trial_expires_at=now + timedelta(days=3))
        for index, percent in enumerate(("12.5", "31.0", None)):
            listing = _listing(s, ids["Toyota"], external_id=f"s{index}")
            s.add(
                SentNotification(
                    user_id=active.id,
                    listing_id=listing.id,
                    below_market_percent=Decimal(percent) if percent else None,
                    sent_at=now - timedelta(hours=1),
                )
            )
        s.commit()
        assert {u.telegram_id for u in trial_summary_candidates(s, now)} == {1, 2}
        stats = trial_stats(s, active)
        assert (stats.sent, stats.below_market, stats.best_percent) == (3, 2, 31.0)
        assert silent.id

    # Витрина пуста (нет статистики) — молчащему ничего не шлём, но и не повторяем.
    assert worker.send_trial_summaries() == 1
    assert [chat_id for chat_id, _ in sent] == [1]
    assert "31%" in sent[0][1]
    assert worker.send_trial_summaries() == 0
    with sessions() as s:
        assert s.scalar(
            select(UserEvent.id).where(
                UserEvent.user_id == silent.id, UserEvent.event == EVENT_TRIAL_SUMMARY
            )
        )


# --------------------------------------------------------------------------
# Разовые рассылки
# --------------------------------------------------------------------------


def test_burned_segment_counts_week_matches(sessions, monkeypatch) -> None:
    with sessions() as s:
        ids = _catalog(s)
        _stat(s, ids["Toyota"])
        burned = dict(
            access_status="revoked",
            trial_started_at=_now() - timedelta(days=10),
            trial_expires_at=_now() - timedelta(days=7),
            trial_expired_notified_at=_now() - timedelta(days=7),
        )
        with_filter = _user(s, 1, **burned)
        _user(s, 2, **burned)
        _user(s, 3, access_status="revoked")  # пейвола не было — не сегмент
        s.add(
            UserFilter(
                user_id=with_filter.id,
                region=LOCAL,
                below_market_only=True,
                below_market_percent=20,
            )
        )
        _listing(s, ids["Toyota"], external_id="a", price="7000", published_hours_ago=30)
        _listing(s, ids["Toyota"], external_id="b", price="9500")  # всего 5% — мимо
        s.commit()

        recipients = {r.user.telegram_id: r for r in campaigns.recipients_for(s, "burned")}
        assert set(recipients) == {1, 2}
        assert "1 машина" in recipients[1].text
        assert "по вашему фильтру" not in recipients[2].text

        monkeypatch.setattr(campaigns, "send_message", lambda *a, **k: SendOutcome.UNDELIVERABLE)
        monkeypatch.setattr(campaigns, "SEND_PAUSE_SECONDS", 0)
        outcomes = campaigns.send_campaign(s, "burned", list(recipients.values()))
        assert outcomes["UNDELIVERABLE"] == 2
        assert campaigns.recipients_for(s, "burned") == []  # повторный запуск — пусто
        assert s.get(BotUser, with_filter.id).is_active is False


# --------------------------------------------------------------------------
# Только полные данные и правдоподобная скидка (замер 21.10.2026: из 54 пушей
# новичку у 35 не было пробега, Lexus ES 2020 за $1800 ушёл как «−93%»)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "mileage", "expected"),
    [
        (2018, 120_000, True),
        (2018, None, False),  # пробег не указан
        (1998, 200, False),  # заглушка у подержанной
        (2026, 27, True),  # новая из салона — малый пробег законен
        (1994, 2_485_288, False),  # опечатка
    ],
)
def test_is_complete_listing(year, mileage, expected) -> None:
    from app.bot.matching import is_complete_listing

    listing = Listing(brand_id=1, car_model_id=1, year=year, mileage_km=mileage)
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    assert is_complete_listing(listing, now) is expected


def test_push_skips_incomplete_and_implausible(sessions) -> None:
    with sessions() as s:
        ids = _catalog(s)
        user = _user(s, 7, access_status="member")
        s.add(
            UserFilter(
                user_id=user.id,
                region=LOCAL,
                below_market_only=True,
                below_market_percent=10,
                created_at=_now() - timedelta(hours=1),
            )
        )
        _stat(s, ids["Toyota"])
        _listing(s, ids["Toyota"], external_id="ok", price="8000", event=True)  # −20%
        _listing(s, ids["Toyota"], external_id="junk", price="1500", event=True)  # −85%
        no_km = _listing(s, ids["Toyota"], external_id="nokm", price="8000", event=True)
        no_km.mileage_km = None
        s.commit()
        since = _now() - timedelta(hours=2)
        pending = match_new_listings(
            s, since, factor=0.95, min_samples=2, require_complete=True, max_below_percent=50
        )
    assert [p.title for p in pending] == ["car ok"]


def test_new_filter_skips_backlog_published_before_it(sessions) -> None:
    """Накопительный эффект: объявление опубликовано ДО фильтра, а замечено
    нами ПОСЛЕ (ночь, догон парсера) — новичку это не новость, его отдаёт превью.
    Подешевевшее после создания фильтра — новость, когда бы ни опубликовали."""
    with sessions() as s:
        ids = _catalog(s)
        user = _user(s, 7, access_status="member")
        s.add(
            UserFilter(
                user_id=user.id,
                region=LOCAL,
                below_market_only=True,
                below_market_percent=10,
                created_at=_now() - timedelta(minutes=30),
            )
        )
        _stat(s, ids["Toyota"])
        _listing(s, ids["Toyota"], external_id="fresh", published_hours_ago=0.1, event=True)
        _listing(s, ids["Toyota"], external_id="backlog", published_hours_ago=5, event=True)
        cheaper = _listing(s, ids["Toyota"], external_id="cheaper", published_hours_ago=5)
        s.add(
            ListingStatusHistory(
                listing_id=cheaper.id,
                event_type="price_changed",
                old_status="active",
                new_status="active",
                observed_at=_now(),
            )
        )
        s.commit()
        pending = match_new_listings(s, _now() - timedelta(hours=3), factor=0.95, min_samples=2)
    assert sorted(p.title for p in pending) == ["car cheaper", "car fresh"]
