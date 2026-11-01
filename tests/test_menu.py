"""Меню в стиле BotFather (план 01.11.2026, п. В): экраны и навигация."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

import app.bot.db as botdb
from app.bot import handlers, menu_handlers
from app.bot import menu_screens as ms
from app.bot.access import AccessMiddleware
from app.bot.states import FilterPanel
from app.models import SentNotification
from tests.test_filter_panel import _dto, _seed, sessions  # noqa: F401 — фикстура

NOW = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)


def _info(**overrides):
    base = dict(
        access_status="subscriber",
        expires_at=NOW + timedelta(days=29),
        active=True,
        trial_active=False,
        trial_expires_at=None,
        trial_used=True,
        tier="full",
        schedule=(),
        paused_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# --- экраны ----------------------------------------------------------------------------


def test_home_summarises_everything_on_one_screen() -> None:
    text, markup = ms.render_home(ms.HomeData(info=_info(), filters_used=2, filters_limit=5, blocks=3))
    assert "Доступ: подписка до 30.11" in text and "полная скорость" in text
    assert "Уведомления: включены · Фильтров: 2 из 5 · Скрыто: 3" in text
    assert _texts(markup) == [
        "📋 Фильтры (2/5)",
        "🚫 Чёрный список (3)",
        "🔔 Уведомления",
        "💎 Доступ",
        "👥 Пригласить",
        "❓ Помощь",
        "✖ Закрыть",
    ]


@pytest.mark.parametrize(
    ("info", "line"),
    [
        (None, "Доступ: нет"),
        (_info(access_status="member"), "Доступ: участник группы"),
        (_info(trial_active=True, trial_expires_at=NOW, active=False), "Доступ: пробный период до 01.11"),
        (_info(tier="delayed"), "с отставанием ~1 ч"),
        (_info(active=False), "Доступ: закончился"),
    ],
)
def test_access_line(info, line) -> None:
    assert line in ms.access_line(info)


def test_paused_shows_on_home_and_notify_screen() -> None:
    text, markup = ms.render_home(
        ms.HomeData(info=_info(paused_at=NOW), filters_used=0, filters_limit=5, blocks=0)
    )
    assert "Уведомления: на паузе" in text and "🔔 Уведомления ⏸" in _texts(markup)
    _, notify = ms.render_notify(NOW)
    assert _texts(notify)[0] == "▶️ Включить"
    _, notify = ms.render_notify(None)
    assert _texts(notify)[0] == "⏸ Поставить на паузу"


def test_filters_list_and_empty_state() -> None:
    text, markup = ms.render_filters([_dto(id=5), _dto(id=6, brand_name=None, model_name=None)], 5)
    assert text.startswith("<b>📋 Фильтры — 2 из 5</b>\n1. Toyota Camry")
    assert _datas(markup) == ["m:f:5", "m:f:6", "m:fa:0", "m:home:0"]
    assert all(len(t) <= ms.BUTTON_LABEL_MAX for t in _texts(markup))

    text, markup = ms.render_filters([], 5)
    assert "Фильтров пока нет" in text
    assert _texts(markup) == ["➕ Добавить фильтр", "◀ Назад"]


def test_filter_card_sells_itself_with_stats() -> None:
    text, markup = ms.render_filter(_dto(id=5), 1, ms.FilterStats(sent=14, best_percent=31.4))
    assert "За 7 дней прислал: 14 объявлений (лучшее — ниже рынка на 31%)" in text
    assert _datas(markup) == ["m:fe:5", "m:fd:5", "m:fl:0"]
    text, _ = ms.render_filter(_dto(id=5), 1, ms.FilterStats(sent=0, best_percent=None))
    assert "ничего не прислал" in text


def test_delete_asks_first() -> None:
    _, markup = ms.render_filter_delete(_dto(id=5))
    assert _datas(markup) == ["m:fdy:5", "m:f:5"]


def test_callback_data_fits_telegram_limit() -> None:
    assert len(ms.MenuCB(s=ms.FILTER_DELETE_OK, a=2_147_483_647).pack().encode()) <= 64


def test_menu_is_an_entry_point_but_other_buttons_are_not() -> None:
    assert AccessMiddleware._is_entry(CallbackQuery.model_construct(data="m:fl:0")) is True
    assert AccessMiddleware._is_entry(CallbackQuery.model_construct(data="model:1")) is False
    message = Message.model_construct(text="/menu")
    assert AccessMiddleware._is_entry(message) is True


# --- навигация через хендлер -------------------------------------------------------------


class _MenuMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=1)
        self.message_id = 77
        self.edits: list[tuple[str, object]] = []
        self.answers: list[tuple[str, object]] = []
        self.deleted = False
        self.not_modified = False

    async def edit_text(self, text, reply_markup=None, **kwargs):
        if self.not_modified:
            raise TelegramBadRequest(method=SimpleNamespace(), message="Bad Request: message is not modified")
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))

    async def delete(self):
        self.deleted = True


class _MenuQuery:
    def __init__(self, message: _MenuMessage) -> None:
        self.message = message
        self.from_user = SimpleNamespace(id=1)
        self.toasts: list[str | None] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.toasts.append(text)


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def _press(screen: str, arg: int = 0, *, has_access: bool = True, message=None, state=None):
    message = message or _MenuMessage()
    query = _MenuQuery(message)
    asyncio.run(
        menu_handlers.cb_menu(query, ms.MenuCB(s=screen, a=arg), state or _state(), has_access=has_access)
    )
    return message, query


@pytest.fixture
def fake_db(monkeypatch):
    store = {
        "filters": [_dto(id=5), _dto(id=6, brand_name="Lexus", model_name="RX")],
        "blocks": [botdb.BlockDTO(id=9, kind="model", label="Toyota Prius")],
        "paused": None,
    }

    def delete_filter(telegram_id, filter_id):
        before = len(store["filters"])
        store["filters"] = [f for f in store["filters"] if f.id != filter_id]
        return len(store["filters"]) < before

    def delete_block(telegram_id, block_id):
        before = len(store["blocks"])
        store["blocks"] = [b for b in store["blocks"] if b.id != block_id]
        return len(store["blocks"]) < before

    def set_paused(telegram_id, paused):
        changed = (store["paused"] is None) == paused
        store["paused"] = NOW if paused else None
        return SimpleNamespace(changed=changed, paused_at=store["paused"])

    db = menu_handlers.db
    monkeypatch.setattr(db, "list_filters", lambda telegram_id: list(store["filters"]))
    monkeypatch.setattr(db, "get_filter", lambda t, fid: next((f for f in store["filters"] if f.id == fid), None))
    monkeypatch.setattr(db, "delete_filter", delete_filter)
    monkeypatch.setattr(db, "list_blocks", lambda telegram_id: list(store["blocks"]))
    monkeypatch.setattr(db, "delete_block", delete_block)
    monkeypatch.setattr(db, "set_paused", set_paused)
    monkeypatch.setattr(db, "filter_sent_stats", lambda t, fid, days=7: (3, 25.0))
    monkeypatch.setattr(db, "get_subscription_info", lambda t: _info(paused_at=store["paused"]))
    monkeypatch.setattr(
        db, "filters_quota", lambda t: botdb.FiltersQuota(used=len(store["filters"]), limit=5)
    )
    monkeypatch.setattr(db, "list_brands", lambda: [])
    return store


def test_navigation_redraws_the_same_message(fake_db) -> None:
    message, _ = _press(ms.FILTERS)
    text, markup = message.edits[-1]
    assert "Фильтры — 2 из 5" in text and "m:f:6" in _datas(markup)
    message, _ = _press(ms.FILTER, 6, message=message)
    assert "Фильтр 2" in message.edits[-1][0] and "прислал: 3" in message.edits[-1][0]
    assert message.answers == []  # ни одного нового сообщения


def test_delete_with_confirmation_and_toast(fake_db) -> None:
    message, _ = _press(ms.FILTER_DELETE, 5)
    assert message.edits[-1][0].startswith("Удалить фильтр?")
    message, query = _press(ms.FILTER_DELETE_OK, 5, message=message)
    assert [f.id for f in fake_db["filters"]] == [6]
    assert query.toasts[-1] == "Фильтр удалён"
    assert "Фильтры — 1 из 5" in message.edits[-1][0]


def test_stale_filter_button_says_it_is_gone(fake_db) -> None:
    message, _ = _press(ms.FILTER, 999)
    text, markup = message.edits[-1]
    assert text == "Фильтр уже удалён." and _datas(markup) == ["m:fl:0"]


def test_blacklist_return_item(fake_db) -> None:
    message, query = _press(ms.BLACKLIST_REMOVE, 9)
    assert fake_db["blocks"] == [] and query.toasts[-1] == "Вернул в выдачу"
    assert "Чёрный список пуст" in message.edits[-1][0]


def test_pause_and_play_from_menu(fake_db) -> None:
    message, query = _press(ms.PAUSE)
    assert fake_db["paused"] == NOW and query.toasts[-1]
    assert "на паузе" in message.edits[-1][0]
    message, _ = _press(ms.PLAY, message=message)
    assert fake_db["paused"] is None and "включены" in message.edits[-1][0]


def test_sections_without_access_point_to_access(fake_db) -> None:
    for screen in (ms.FILTERS, ms.FILTER_DELETE_OK, ms.BLACKLIST):
        message, _ = _press(screen, 5, has_access=False)
        text, markup = message.edits[-1]
        assert "работает при активном доступе" in text
        assert _datas(markup) == ["m:ac:0", "m:home:0"]
    assert len(fake_db["filters"]) == 2  # без доступа удалить нельзя


def test_notify_works_without_access(fake_db) -> None:
    message, _ = _press(ms.NOTIFY, has_access=False)
    assert "Уведомления: включены" in message.edits[-1][0]


def test_edit_opens_panel_in_the_same_message(fake_db) -> None:
    state = _state()
    message, _ = _press(ms.FILTER_EDIT, 5, state=state)
    assert "Изменение фильтра" in message.edits[-1][0]
    assert asyncio.run(state.get_state()) == FilterPanel.active.state
    assert asyncio.run(state.get_data())["panel_id"] == 77


def test_add_filter_shows_presets_or_limit(fake_db) -> None:
    message, _ = _press(ms.FILTER_ADD)
    text, markup = message.edits[-1]
    assert text == handlers.PRESETS_PROMPT_TEXT and _datas(markup)[-1] == "m:fl:0"
    fake_db["filters"] = [_dto(id=n) for n in range(5)]
    message, _ = _press(ms.FILTER_ADD)
    assert "5" in message.edits[-1][0] and _datas(message.edits[-1][1]) == ["m:fl:0"]


def test_close_deletes_menu_message(fake_db) -> None:
    message, _ = _press(ms.CLOSE)
    assert message.deleted


def test_double_tap_not_modified_is_swallowed(fake_db) -> None:
    message = _MenuMessage()
    message.not_modified = True
    _press(ms.HELP, message=message)  # не падает


def test_save_panel_offers_way_back_to_filters(monkeypatch) -> None:
    monkeypatch.setattr(handlers.db, "update_filter", lambda *a, **k: _dto(id=5))
    message = _MenuMessage()
    query = _MenuQuery(message)
    state = _state()
    data = {"draft": {"done": []}, "filter_id": 5}
    asyncio.run(handlers._save_panel(query, state, data))
    assert _datas(message.edits[-1][1]) == ["m:fl:0"]


# --- статистика фильтра ---------------------------------------------------------------------


def test_filter_sent_stats_counts_last_week_only(sessions) -> None:  # noqa: F811
    with sessions() as s:
        user, mine, _ = _seed(s)
        old = datetime.now(timezone.utc) - timedelta(days=10)
        s.add_all(
            [
                SentNotification(user_id=user.id, listing_id=1, filter_id=mine.id, below_market_percent=12),
                SentNotification(user_id=user.id, listing_id=2, filter_id=mine.id, below_market_percent=31),
                SentNotification(user_id=user.id, listing_id=3, filter_id=mine.id, sent_at=old),
            ]
        )
        s.commit()
        filter_id = mine.id
    assert botdb.filter_sent_stats(1, filter_id) == (2, 31.0)
    assert botdb.filter_sent_stats(2, filter_id) == (0, None)  # чужой фильтр
