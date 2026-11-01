"""Меню в стиле BotFather (`/menu`, план 01.11.2026, п. В): экраны, навигация, доступ."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.bot.db as botdb
from app.bot import handlers
from app.bot import menu_screens as ms
from app.bot.access import AccessMiddleware
from app.bot.commands import BOT_COMMANDS
from app.bot.db import BlockDTO, FilterDTO
from app.bot.messages import HELP_TEXT, menu_access_line
from app.db.base import Base
from app.models import BotUser, SentNotification, UserEvent, UserFilter
from app.services.regions import LOCAL


def _dto(**overrides) -> FilterDTO:
    base = dict(
        id=5, brand_name="Toyota", model_name="Camry", year_from=2015, year_to=None,
        mileage_max_km=150000, price_max_usd=15000.0, below_market_only=True,
        below_market_percent=10, skip_foreign=False, region=LOCAL, brand_group=None,
        max_age_years=None, brand_id=1, car_model_id=10,
    )
    base.update(overrides)
    return FilterDTO(**base)


def _buttons(markup) -> list[tuple[str, str | None]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def _cb(screen: str, arg: int = 0) -> str:
    return ms.MenuCB(screen=screen, arg=arg).pack()


# --- экраны ------------------------------------------------------------------------------


def test_home_screen_summary_and_sections() -> None:
    data = ms.HomeData(
        has_access=True, access_line="подписка до 30.11.2026 · полная скорость",
        paused_at=None, filters_used=2, filters_limit=5, blocks=3,
    )
    text, markup = ms.home_screen(data)
    assert "Доступ: подписка до 30.11.2026 · полная скорость" in text
    assert "Уведомления: включены" in text
    assert "Фильтров: 2 из 5 · Скрыто: 3" in text
    labels = [label for label, _ in _buttons(markup)]
    assert labels == [
        "📋 Фильтры (2/5)", "🚫 Чёрный список (3)", "🔔 Уведомления", "💎 Доступ",
        "👥 Пригласить", "❓ Помощь", "✖ Закрыть",
    ]


def test_filters_screen_lists_and_hides_add_at_limit() -> None:
    rows = [ms.FilterRow(id=i, summary=f"Фильтр {i}") for i in (7, 8)]
    text, markup = ms.filters_screen(rows, limit=5)
    assert "Фильтры - 2 из 5" in text and "1. Фильтр 7" in text
    assert ("1. Фильтр 7", _cb(ms.FILTER, 7)) in _buttons(markup)
    assert ("➕ Добавить фильтр", _cb(ms.FILTER_ADD)) in _buttons(markup)
    _, full = ms.filters_screen(rows, limit=2)
    assert all(label != "➕ Добавить фильтр" for label, _ in _buttons(full))
    empty_text, empty = ms.filters_screen([], limit=5)
    assert "Фильтров пока нет" in empty_text
    assert ("➕ Добавить фильтр", _cb(ms.FILTER_ADD)) in _buttons(empty)


def test_filter_card_stats_and_plurals() -> None:
    row = ms.FilterRow(id=9, summary="Toyota Camry")
    text, markup = ms.filter_screen(row, 1, sent_7d=14, best_percent=31.4)
    assert "За 7 дней прислал: 14 объявлений (лучшее - ниже рынка на 31%)" in text
    assert ms.filter_screen(row, 1, sent_7d=1, best_percent=None)[0].count("1 объявление") == 1
    assert "3 объявления" in ms.filter_screen(row, 1, sent_7d=3, best_percent=None)[0]
    assert "ничего не прислал" in ms.filter_screen(row, 1, sent_7d=0, best_percent=None)[0]
    assert _buttons(markup) == [
        ("✏️ Изменить", _cb(ms.FILTER_EDIT, 9)),
        ("🗑 Удалить", _cb(ms.FILTER_DELETE, 9)),
        ("◀ К фильтрам", _cb(ms.FILTERS)),
    ]


def test_notify_screen_toggles() -> None:
    _, on = ms.notify_screen(None)
    assert _buttons(on)[0] == ("⏸ Поставить на паузу", _cb(ms.PAUSE))
    text, off = ms.notify_screen(datetime.now(timezone.utc), "31.10 14:05")
    assert "на паузе с 31.10 14:05" in text
    assert _buttons(off)[0] == ("▶️ Включить", _cb(ms.PLAY))


def test_long_labels_are_cut_and_callback_fits_telegram_limit() -> None:
    row = ms.FilterRow(id=2_147_483_647, summary="Toyota Camry · 2015-… г. · до 150 000 км · до $15 000")
    _, markup = ms.filters_screen([row], limit=5)
    label, data = _buttons(markup)[0]
    assert len(label) <= 48 and label.endswith("…")
    assert len(_cb(ms.FILTER_DELETE_YES, 2_147_483_647).encode()) <= 64
    assert len(data.encode()) <= 64


def test_menu_access_line() -> None:
    day = datetime(2026, 11, 30, tzinfo=timezone.utc)
    info = SimpleNamespace(access_status="subscriber", active=True, trial_active=False,
                           expires_at=day, trial_expires_at=None, tier="delayed")
    assert menu_access_line(info) == "подписка до 30.11.2026 · с отставанием ~1 ч"
    trial = SimpleNamespace(access_status="trial", active=False, trial_active=True,
                            expires_at=None, trial_expires_at=day, tier="full")
    assert menu_access_line(trial) == "бесплатный период до 30.11.2026"
    assert menu_access_line(SimpleNamespace(access_status="member")).startswith("участник группы")
    gone = SimpleNamespace(access_status="revoked", active=False, trial_active=False)
    assert "💎 Доступ" in menu_access_line(gone)


def test_menu_in_commands_help_and_entry() -> None:
    assert BOT_COMMANDS[0][0] == "menu"
    assert "/menu" in HELP_TEXT
    msg = Message.model_construct(text="/menu")
    assert AccessMiddleware._is_entry(msg)
    query = CallbackQuery.model_construct(data=_cb(ms.FILTERS))
    assert AccessMiddleware._is_entry(query)


# --- навигация через хендлер ----------------------------------------------------------------


class _Msg:
    def __init__(self) -> None:
        self.message_id = 50
        self.chat = SimpleNamespace(id=1)
        self.from_user = SimpleNamespace(id=999)  # сообщение меню - от бота
        self.edited: list[tuple[str, object]] = []
        self.answers: list[str] = []
        self.deleted = False

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edited.append((text, reply_markup))

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append(text)
        return SimpleNamespace(message_id=90)

    async def delete(self):
        self.deleted = True


class _Query:
    def __init__(self) -> None:
        self.message = _Msg()
        self.from_user = SimpleNamespace(id=1)
        self.notices: list[str | None] = []

    async def answer(self, text=None, **kwargs):
        self.notices.append(text)


@pytest.fixture()
def fake_db(monkeypatch):
    state = {"filters": [_dto(id=5), _dto(id=6, brand_name=None, model_name=None)],
             "blocks": [BlockDTO(id=3, kind="brand", label="BMW")], "paused_at": None,
             "events": 0}
    info = lambda: SimpleNamespace(  # noqa: E731
        access_status="trial", active=False, trial_active=True, expires_at=None,
        trial_expires_at=datetime(2026, 11, 4, tzinfo=timezone.utc), tier="full",
        paused_at=state["paused_at"], schedule=(), trial_used=True,
    )
    monkeypatch.setattr(handlers.db, "get_subscription_info", lambda tid: info())
    monkeypatch.setattr(handlers.db, "list_filters", lambda tid: list(state["filters"]))
    monkeypatch.setattr(handlers.db, "list_blocks", lambda tid: list(state["blocks"]))
    monkeypatch.setattr(handlers.db, "get_filter",
                        lambda tid, fid: next((f for f in state["filters"] if f.id == fid), None))
    monkeypatch.setattr(handlers.db, "filter_sent_stats",
                        lambda tid, fid: botdb.FilterSentStats(count=4, best_percent=22.0))

    def delete_filter(tid, fid):
        before = len(state["filters"])
        state["filters"] = [f for f in state["filters"] if f.id != fid]
        return len(state["filters"]) < before

    def delete_block(tid, bid):
        before = len(state["blocks"])
        state["blocks"] = [b for b in state["blocks"] if b.id != bid]
        return len(state["blocks"]) < before

    def set_paused(tid, paused):
        state["paused_at"] = datetime.now(timezone.utc) if paused else None
        return botdb.PauseResult(changed=True, paused_at=state["paused_at"])

    def log_menu_open(tid):
        state["events"] += 1

    monkeypatch.setattr(handlers.db, "delete_filter", delete_filter)
    monkeypatch.setattr(handlers.db, "delete_block", delete_block)
    monkeypatch.setattr(handlers.db, "set_paused", set_paused)
    monkeypatch.setattr(handlers.db, "log_menu_open", log_menu_open)
    return state


def _press(query: _Query, screen: str, arg: int = 0, *, has_access: bool = True) -> str:
    fsm = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    asyncio.run(handlers.cb_menu(query, ms.MenuCB(screen=screen, arg=arg), fsm, has_access=has_access))
    return query.message.edited[-1][0] if query.message.edited else ""


def test_navigation_filter_delete_flow(fake_db) -> None:
    query = _Query()
    home = _press(query, ms.HOME)
    assert "Фильтров: 2 из 5 · Скрыто: 1" in home and fake_db["events"] == 1
    assert "Фильтры - 2 из 5" in _press(query, ms.FILTERS)
    card = _press(query, ms.FILTER, 5)
    assert "Фильтр 1" in card and "За 7 дней прислал: 4 объявления" in card
    assert "Удалить фильтр?" in _press(query, ms.FILTER_DELETE, 5)
    after = _press(query, ms.FILTER_DELETE_YES, 5)
    assert "Фильтры - 1 из 5" in after and query.notices[-1] == "Фильтр удалён"
    # Старая кнопка удалённого фильтра - честное «уже удалён», без падения.
    assert _press(query, ms.FILTER, 5) == ms.FILTER_GONE_TEXT


def test_blacklist_return_and_pause_toggle(fake_db) -> None:
    query = _Query()
    assert "1. BMW" in _press(query, ms.BLACKLIST)
    assert "Чёрный список пуст" in _press(query, ms.BLOCK_RETURN, 3)
    assert "включены" in _press(query, ms.NOTIFY)
    assert "на паузе" in _press(query, ms.PAUSE) and fake_db["paused_at"] is not None
    assert "включены" in _press(query, ms.PLAY) and fake_db["paused_at"] is None


def test_gated_sections_without_access(fake_db) -> None:
    query = _Query()
    assert _press(query, ms.FILTERS, has_access=False) == ms.NO_ACCESS_TEXT
    assert fake_db["filters"]  # ничего не удалено
    _press(query, ms.FILTER_DELETE_YES, 5, has_access=False)
    assert len(fake_db["filters"]) == 2
    # Доступ и помощь открываются и без доступа - иначе человеку некуда идти.
    assert "Доступ" in _press(query, ms.ACCESS, has_access=False)
    assert _press(query, ms.HELP, has_access=False) == HELP_TEXT


def test_close_and_plans_use_presser_id(fake_db, monkeypatch) -> None:
    seen: list[int | None] = []

    async def send_plans(message, info, telegram_id=None):
        seen.append(telegram_id)

    monkeypatch.setattr(handlers, "_send_plans", send_plans)
    query = _Query()
    _press(query, ms.PLANS)
    assert seen == [1]  # id нажавшего, а не автора сообщения (бота)
    _press(query, ms.CLOSE)
    assert query.message.deleted


def test_unknown_screen_falls_back_to_home(fake_db) -> None:
    assert ms.MENU_TITLE in _press(_Query(), "zzz")


# --- база ------------------------------------------------------------------------------------


@pytest.fixture()
def sessions(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(botdb, "SessionLocal", factory)
    return factory


def test_filter_sent_stats_counts_week_of_this_filter(sessions) -> None:
    now = datetime.now(timezone.utc)
    with sessions() as session:
        user, other = BotUser(telegram_id=1), BotUser(telegram_id=2)
        session.add_all([user, other])
        session.flush()
        mine = UserFilter(user_id=user.id, below_market_only=True, region=LOCAL)
        session.add(mine)
        session.flush()
        rows = [
            (user.id, mine.id, now - timedelta(days=1), 12.0),
            (user.id, mine.id, now - timedelta(days=2), 31.5),
            (user.id, mine.id, now - timedelta(days=9), 50.0),  # старше недели
            (user.id, None, now, 40.0),  # другой фильтр
            (other.id, mine.id, now, 45.0),  # чужой
        ]
        for index, (uid, fid, sent_at, pct) in enumerate(rows, start=1):
            session.add(SentNotification(user_id=uid, listing_id=index, filter_id=fid,
                                         sent_at=sent_at, below_market_percent=pct))
        session.commit()
        filter_id = mine.id

    stats = botdb.filter_sent_stats(1, filter_id)
    assert stats.count == 2 and stats.best_percent == 31.5
    assert botdb.filter_sent_stats(1, 999) == botdb.FilterSentStats(count=0, best_percent=None)


def test_log_menu_open_writes_event(sessions) -> None:
    with sessions() as session:
        session.add(BotUser(telegram_id=1))
        session.commit()
    botdb.log_menu_open(1)
    botdb.log_menu_open(404)  # незнакомый - без ошибки
    with sessions() as session:
        assert session.scalars(select(UserEvent.event)).all() == ["menu_open"]
