"""Панель фильтра (план 31.10.2026, п. 2.7): одно сообщение, 7 шагов, правка.

Проверяем чистые функции (`bot/filter_panel.py`), правку фильтра в базе
(`db.update_filter`) и сценарий целиком через хендлеры с фейковым ботом.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.bot.db as botdb
from app.bot import filter_panel as fp
from app.bot import handlers
from app.bot.db import BrandDTO, FilterDTO, ModelDTO
from app.bot.keyboards import CUSTOM_PRESET, FilterEditCB, PresetCB, filters_kb
from app.bot.states import FilterPanel
from app.db.base import Base
from app.models import BotUser, Brand, CarModel, SentNotification, UserFilter
from app.services.regions import BISHKEK, FOREIGN, LOCAL

TOYOTA = BrandDTO(id=1, name="Toyota")
HONDA = BrandDTO(id=2, name="Honda")
CAMRY = ModelDTO(id=10, name="Camry")


def _all_buttons(markup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


# --- отрисовка ----------------------------------------------------------------------


def test_new_panel_shows_seven_steps_with_statuses_and_summary() -> None:
    draft = fp.new_draft()
    draft.update(brand_id=1, brand_name="Toyota", year_from=2015)
    text = fp.render_text(draft, fp.STEP_MILEAGE)
    assert "🔧 Новый фильтр" in text
    assert "✅ 1 Марка: Toyota" in text
    assert "⬜ 2 Модель: любая" in text
    assert "✅ 3 Годы: от 2015" in text
    assert "✏️ 4 Пробег: любой  ← сейчас" in text
    assert "⬜ 7 Регион: вся КР" in text
    assert "Итог: Toyota (все модели) · 2015–… г. · регион: Местный рынок (вся КР)" in text


def test_all_cars_hint_only_without_brand_and_market() -> None:
    assert fp.ALL_CARS_HINT in fp.render_text(fp.new_draft(), fp.STEP_BRAND)
    with_market = {**fp.new_draft(), "below_market_only": True, "below_market_percent": 10}
    assert fp.ALL_CARS_HINT not in fp.render_text(with_market, fp.STEP_BRAND)
    with_brand = {**fp.new_draft(), "brand_id": 1, "brand_name": "Toyota"}
    assert fp.ALL_CARS_HINT not in fp.render_text(with_brand, fp.STEP_BRAND)


def test_names_are_html_escaped() -> None:
    draft = {**fp.new_draft(), "brand_id": 1, "brand_name": "A<b>&"}
    text = fp.render_text(draft, fp.STEP_BRAND)
    assert "A&lt;b&gt;&amp;" in text and "A<b>&" not in text


def test_edit_panel_title_and_save_button() -> None:
    assert "✏️ Изменение фильтра" in fp.render_text(fp.new_draft(), 1, editing=True)
    labels = [b.text for b in _all_buttons(fp.render_keyboard(fp.new_draft(), 1, editing=True))]
    assert "💾 Сохранить" in labels and "💾 Создать фильтр" not in labels


def test_keyboard_has_jump_row_and_fits_telegram_limits() -> None:
    models = [ModelDTO(id=1000 + n, name=f"Model {n}") for n in range(111)]  # как у Toyota
    draft = {**fp.new_draft(), "brand_id": 1, "brand_name": "Toyota"}
    for step in fp.STEPS:
        markup = fp.render_keyboard(
            draft, step, brands=[TOYOTA, HONDA], models=models, model_page=3
        )
        buttons = _all_buttons(markup)
        assert len(buttons) <= 100
        assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
        jump = [b.text for b in markup.inline_keyboard[-2]]
        assert jump == [f"[{n}]" if n == step else str(n) for n in fp.STEPS]


def test_models_are_paginated() -> None:
    models = [ModelDTO(id=1000 + n, name=f"Model {n}") for n in range(111)]
    draft = {**fp.new_draft(), "brand_id": 1}
    texts = [b.text for b in _all_buttons(fp.render_keyboard(draft, fp.STEP_MODEL, models=models))]
    assert "Model 0" in texts and "Model 30" not in texts and "1/4" in texts
    page2 = [b.text for b in _all_buttons(fp.render_keyboard(draft, fp.STEP_MODEL, models=models, model_page=1))]
    assert "Model 30" in page2


def test_model_step_without_brand_points_back() -> None:
    texts = [b.text for b in _all_buttons(fp.render_keyboard(fp.new_draft(), fp.STEP_MODEL))]
    assert "→ 1 Марка" in texts
    assert "сначала шаг 1" in fp.render_text(fp.new_draft(), fp.STEP_MODEL)


def test_current_choice_is_marked() -> None:
    draft = {**fp.new_draft(), "below_market_only": True, "below_market_percent": 10}
    texts = [b.text for b in _all_buttons(fp.render_keyboard(draft, fp.STEP_MARKET))]
    assert "• ниже на 10% •" in texts and "Не важно" in texts


# --- применение кнопок ----------------------------------------------------------------


def test_changing_brand_resets_model_and_group() -> None:
    draft = {**fp.new_draft(), "brand_id": 1, "car_model_id": 10, "model_name": "Camry"}
    draft, advance = fp.apply_choice(draft, "brand", "2")
    assert advance and draft["brand_id"] == 2 and draft["car_model_id"] is None
    same, _ = fp.apply_choice({**draft, "car_model_id": 11}, "brand", "2")
    assert same["car_model_id"] == 11  # та же марка — модель не сбрасываем
    grouped, _ = fp.apply_choice(draft, "group", "japan")
    assert grouped["brand_group"] == "japan" and grouped["brand_id"] is None
    everything, _ = fp.apply_choice(grouped, "brand", "0")
    assert everything["brand_id"] is None and everything["brand_group"] is None


@pytest.mark.parametrize(
    ("action", "value", "expected"),
    [
        ("years", "2015", {"year_from": 2015, "year_to": None, "max_age_years": None}),
        ("years", "age5", {"year_from": None, "year_to": None, "max_age_years": 5}),
        ("years", "any", {"year_from": None, "year_to": None, "max_age_years": None}),
        ("mileage", "150000", {"mileage_max_km": 150000}),
        ("price", "any", {"price_max_usd": None}),
        ("market", "20", {"below_market_only": True, "below_market_percent": 20}),
        ("market", "off", {"below_market_only": False, "below_market_percent": None}),
        ("region", "all", {"region": None}),
        ("region", BISHKEK, {"region": BISHKEK}),
    ],
)
def test_quick_buttons(action: str, value: str, expected: dict) -> None:
    draft, advance = fp.apply_choice({**fp.new_draft(), "price_max_usd": 9000.0}, action, value)
    assert advance
    assert {key: draft[key] for key in expected} == expected


@pytest.mark.parametrize(("action", "value"), [("brand", "x"), ("group", "mars"), ("region", "atlantis"), ("market", "lots"), ("bogus", "1")])
def test_unknown_values_change_nothing(action: str, value: str) -> None:
    draft = fp.new_draft()
    assert fp.apply_choice(draft, action, value) == (draft, False)


# --- ручной ввод ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2015-2022", (2015, 2022)),
        ("2022 – 2015", (2015, 2022)),
        ("от 2018", (2018, None)),
        ("2018+", (2018, None)),
        ("до 2012", (None, 2012)),
        ("2018", (2018, 2018)),
    ],
)
def test_parse_years(text: str, expected) -> None:
    years = fp.parse_years(text)
    assert (years["year_from"], years["year_to"]) == expected


@pytest.mark.parametrize("text", ["вчера", "1800", "от 3000", "15-20", ""])
def test_parse_years_rejects(text: str) -> None:
    assert fp.parse_years(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("150000", 150000), ("150 000", 150000), ("150к", 150000), ("15k", 15000), ("$15 000", 15000), ("до 200 000 км", 200000)],
)
def test_parse_amount(text: str, expected: int) -> None:
    assert fp.parse_amount(text, 5_000_000) == expected


@pytest.mark.parametrize("text", ["много", "0", "-5", "9999999"])
def test_parse_amount_rejects(text: str) -> None:
    assert fp.parse_amount(text, 5_000_000) is None


def test_apply_text_by_step() -> None:
    draft, error = fp.apply_text(fp.new_draft(), fp.STEP_PRICE, "15к")
    assert error is None and draft["price_max_usd"] == 15000.0
    _, error = fp.apply_text(fp.new_draft(), fp.STEP_MILEAGE, "много")
    assert error and "150000" in error
    _, error = fp.apply_text(fp.new_draft(), fp.STEP_BRAND, "Toyota")
    assert error == "На этом шаге выберите вариант кнопкой."


# --- черновик <-> фильтр ----------------------------------------------------------------


def _dto(**overrides) -> FilterDTO:
    base = dict(
        id=5,
        brand_name="Toyota",
        model_name="Camry",
        year_from=2015,
        year_to=None,
        mileage_max_km=150000,
        price_max_usd=15000.0,
        below_market_only=True,
        below_market_percent=10,
        skip_foreign=False,
        region=LOCAL,
        brand_group=None,
        max_age_years=None,
        brand_id=1,
        car_model_id=10,
    )
    base.update(overrides)
    return FilterDTO(**base)


def test_draft_round_trip_keeps_every_field() -> None:
    item = _dto()
    fields = fp.filter_fields(fp.draft_from_filter(item))
    assert fields == {
        "brand_id": 1,
        "car_model_id": 10,
        "year_from": 2015,
        "year_to": None,
        "mileage_max_km": 150000,
        "price_max_usd": 15000.0,
        "below_market_only": True,
        "below_market_percent": 10,
        "skip_foreign": False,
        "region": LOCAL,
        "brand_group": None,
        "max_age_years": None,
    }


def test_preset_filter_with_group_and_age_survives_editing() -> None:
    """«Японцы до 10 лет»: группа и возраст показываются и сохраняются как были."""
    item = _dto(brand_name=None, model_name=None, brand_id=None, car_model_id=None, brand_group="japan", max_age_years=10, year_from=None)
    draft = fp.draft_from_filter(item)
    text = fp.render_text(draft, fp.STEP_PRICE, editing=True)
    assert "✅ 1 Марка: Японские марки" in text and "не старше 10 лет" in text
    fields = fp.filter_fields(draft)
    assert fields["brand_group"] == "japan" and fields["max_age_years"] == 10


def test_market_off_drops_percent() -> None:
    draft, _ = fp.apply_choice(fp.draft_from_filter(_dto()), "market", "off")
    assert fp.filter_fields(draft)["below_market_percent"] is None


# --- db.update_filter -----------------------------------------------------------------


@pytest.fixture()
def sessions(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(botdb, "SessionLocal", factory)
    return factory


def _seed(session) -> tuple[BotUser, UserFilter, UserFilter]:
    brand = Brand(name="Toyota")
    session.add(brand)
    session.flush()
    session.add(CarModel(brand_id=brand.id, name="Camry"))
    user, other = BotUser(telegram_id=1), BotUser(telegram_id=2)
    session.add_all([user, other])
    session.flush()
    created = datetime.now(timezone.utc) - timedelta(days=3)
    mine = UserFilter(user_id=user.id, brand_id=brand.id, below_market_only=False, created_at=created)
    second = UserFilter(user_id=user.id, below_market_only=True, below_market_percent=10, region=LOCAL)
    session.add_all([mine, second])
    session.flush()
    return user, mine, second


def _fields(**overrides) -> dict:
    base = dict(
        brand_id=None,
        car_model_id=None,
        year_from=None,
        year_to=None,
        mileage_max_km=None,
        price_max_usd=None,
        below_market_only=False,
        below_market_percent=None,
        region=LOCAL,
    )
    base.update(overrides)
    return base


def test_update_keeps_the_row_history_and_starting_point(sessions) -> None:
    with sessions() as s:
        user, mine, _ = _seed(s)
        # SQLite в тестах FK не проверяет: объявление для строки истории не нужно.
        s.add(SentNotification(user_id=user.id, listing_id=1, filter_id=mine.id))
        s.commit()
        filter_id, created_at = mine.id, mine.created_at

    updated = botdb.update_filter(1, filter_id, **_fields(brand_id=None, price_max_usd=9000.0, region=FOREIGN))
    assert updated.id == filter_id and updated.price_max_usd == 9000.0 and updated.region == FOREIGN
    with sessions() as s:
        row = s.get(UserFilter, filter_id)
        assert row.created_at.replace(tzinfo=None) == created_at.replace(tzinfo=None)
        assert s.scalar(select(SentNotification.filter_id)) == filter_id  # история не потерялась


def test_update_rejects_duplicate_of_another_filter_but_not_itself(sessions) -> None:
    with sessions() as s:
        _, mine, second = _seed(s)
        s.commit()
        mine_id = mine.id
    with pytest.raises(botdb.DuplicateFilterError):
        botdb.update_filter(1, mine_id, **_fields(below_market_only=True, below_market_percent=10))
    # Сохранить без изменений — не дубль самого себя.
    brand_id = botdb.get_filter(1, mine_id).brand_id
    botdb.update_filter(1, mine_id, **_fields(brand_id=brand_id, region=None))


def test_update_of_foreign_or_missing_filter_is_lookup_error(sessions) -> None:
    with sessions() as s:
        _, mine, _ = _seed(s)
        s.commit()
        mine_id = mine.id
    with pytest.raises(LookupError):
        botdb.update_filter(2, mine_id, **_fields())  # чужой
    with pytest.raises(LookupError):
        botdb.update_filter(1, 999, **_fields())
    assert botdb.get_filter(2, mine_id) is None


def test_my_filters_has_edit_and_delete_per_filter() -> None:
    rows = filters_kb([_dto(id=5), _dto(id=6)]).inline_keyboard
    assert [[b.text for b in row] for row in rows] == [
        ["✏️ Изменить 1", "🗑 Удалить 1"],
        ["✏️ Изменить 2", "🗑 Удалить 2"],
    ]


# --- сценарий через хендлеры ------------------------------------------------------------


class _Bot:
    def __init__(self) -> None:
        self.edits: list[tuple[int, str]] = []
        self.last_markup = None

    async def edit_message_text(self, text, chat_id, message_id, reply_markup=None, **kwargs):
        self.edits.append((message_id, text))
        self.last_markup = reply_markup


class _Message:
    def __init__(self, bot: _Bot, message_id: int = 50, text: str | None = None) -> None:
        self.bot = bot
        self.message_id = message_id
        self.text = text
        self.chat = SimpleNamespace(id=1)
        self.from_user = SimpleNamespace(id=1)
        self.answers: list[str] = []
        self.edited: list[str] = []
        self.deleted = False

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append(text)
        return SimpleNamespace(message_id=90 + len(self.answers))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edited.append(text)

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        self.edited.append("<markup removed>")

    async def delete(self):
        self.deleted = True


class _Query:
    def __init__(self, message: _Message) -> None:
        self.message = message
        self.bot = message.bot
        self.from_user = SimpleNamespace(id=1)
        self.alerts: list[str | None] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.alerts.append(text)


@pytest.fixture()
def flow(monkeypatch):
    calls: dict[str, list] = {"create": [], "update": [], "preview": []}
    monkeypatch.setattr(handlers.db, "filters_quota", lambda tid: botdb.FiltersQuota(used=0, limit=5))
    monkeypatch.setattr(handlers.db, "list_brands", lambda: [TOYOTA, HONDA])
    monkeypatch.setattr(handlers.db, "list_models", lambda brand_id: [CAMRY] if brand_id == 1 else [])

    def create(tid, **fields):
        calls["create"].append(fields)
        return _dto(id=77, **{k: v for k, v in fields.items() if k in FilterDTO.__dataclass_fields__},
                    brand_name="Toyota" if fields["brand_id"] else None,
                    model_name="Camry" if fields["car_model_id"] else None)

    def update(tid, filter_id, **fields):
        calls["update"].append((filter_id, fields))
        return _dto(id=filter_id, **{k: v for k, v in fields.items() if k in FilterDTO.__dataclass_fields__})

    async def preview(message, filter_id):
        calls["preview"].append(filter_id)

    monkeypatch.setattr(handlers.db, "create_filter", create)
    monkeypatch.setattr(handlers.db, "update_filter", update)
    monkeypatch.setattr(handlers.db, "get_filter", lambda tid, fid: _dto(id=fid) if fid == 5 else None)
    monkeypatch.setattr(handlers, "_send_filter_preview", preview)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    return state, calls


def _press(panel: _Message, state, action: str, value: str = "") -> _Query:
    query = _Query(panel)
    asyncio.run(handlers.cb_panel(query, fp.PanelCB(action=action, value=value), state))
    return query


def test_create_flow_in_one_message(flow) -> None:
    state, calls = flow
    bot = _Bot()
    panel = _Message(bot)
    asyncio.run(handlers.cb_preset(_Query(panel), PresetCB(code=CUSTOM_PRESET), state))
    assert asyncio.run(state.get_state()) == FilterPanel.active.state
    assert panel.edited and "🔧 Новый фильтр" in panel.edited[0]  # панель на месте пресетов

    _press(panel, state, "brand", "1")  # Toyota → шаг 2
    _press(panel, state, "model", "10")  # Camry → шаг 3
    typed = _Message(bot, message_id=60, text="2015-2020")
    asyncio.run(handlers.panel_text(typed, state))
    assert typed.deleted  # ввод человека убран из чата
    _press(panel, state, "mileage", "150000")
    _press(panel, state, "step", str(fp.STEP_MARKET))  # прыжок мимо цены
    _press(panel, state, "market", "10")

    # Все перерисовки — правки ОДНОГО сообщения, новых сообщений бот не слал.
    assert {message_id for message_id, _ in bot.edits} == {50}
    assert panel.answers == []
    last_text = bot.edits[-1][1]
    assert "✅ 1 Марка: Toyota" in last_text and "✅ 3 Годы: 2015–2020" in last_text
    assert "✏️ 7 Регион" in last_text

    _press(panel, state, "save")
    [fields] = calls["create"]
    assert fields["brand_id"] == 1 and fields["car_model_id"] == 10
    assert (fields["year_from"], fields["year_to"]) == (2015, 2020)
    assert fields["below_market_percent"] == 10 and fields["region"] == LOCAL
    assert calls["preview"] == [77]
    assert asyncio.run(state.get_state()) is None
    assert "✅ Фильтр создан" in panel.edited[-1]


def test_invalid_text_shows_notice_and_stays_on_step(flow) -> None:
    state, _ = flow
    bot = _Bot()
    panel = _Message(bot)
    asyncio.run(handlers.cb_preset(_Query(panel), PresetCB(code=CUSTOM_PRESET), state))
    _press(panel, state, "step", str(fp.STEP_PRICE))
    asyncio.run(handlers.panel_text(_Message(bot, 61, "дорого"), state))
    data = asyncio.run(state.get_data())
    assert data["step"] == fp.STEP_PRICE
    assert "⚠️ Не понял цену" in bot.edits[-1][1]


def test_duplicate_keeps_panel_open_with_notice(flow, monkeypatch) -> None:
    state, _ = flow

    def duplicate(tid, **fields):
        raise botdb.DuplicateFilterError(3)

    monkeypatch.setattr(handlers.db, "create_filter", duplicate)
    bot = _Bot()
    panel = _Message(bot)
    asyncio.run(handlers.cb_preset(_Query(panel), PresetCB(code=CUSTOM_PRESET), state))
    _press(panel, state, "save")
    assert asyncio.run(state.get_state()) == FilterPanel.active.state
    assert "Такой фильтр у вас уже есть" in bot.edits[-1][1]


def test_edit_flow_updates_the_same_filter(flow) -> None:
    state, calls = flow
    bot = _Bot()
    filters_message = _Message(bot, message_id=40)
    asyncio.run(handlers.cb_edit_filter(_Query(filters_message), FilterEditCB(filter_id=5), state))
    assert "✏️ Изменение фильтра" in filters_message.answers[0]  # панель — новым сообщением
    panel = _Message(bot, message_id=91)  # id из answer()
    _press(panel, state, "step", str(fp.STEP_PRICE))
    _press(panel, state, "price", "any")
    _press(panel, state, "save")
    [(filter_id, fields)] = calls["update"]
    assert filter_id == 5 and fields["price_max_usd"] is None and fields["brand_id"] == 1
    assert calls["create"] == [] and calls["preview"] == []
    assert "✅ Фильтр изменён" in panel.edited[-1]


def test_old_panel_and_restart_are_handled(flow) -> None:
    state, _ = flow
    bot = _Bot()
    panel = _Message(bot)
    asyncio.run(handlers.cb_preset(_Query(panel), PresetCB(code=CUSTOM_PRESET), state))
    other = _Message(bot, message_id=999)  # другая (старая) панель
    query = _press(other, state, "brand", "1")
    assert "устарела" in query.alerts[-1]
    # После рестарта бота состояния нет — ловит отдельный хендлер.
    stale = _Query(_Message(bot))
    asyncio.run(handlers.cb_panel_stale(stale))
    assert "устарела" in stale.alerts[-1]


def test_cancel_closes_panel(flow) -> None:
    state, calls = flow
    bot = _Bot()
    panel = _Message(bot)
    asyncio.run(handlers.cb_preset(_Query(panel), PresetCB(code=CUSTOM_PRESET), state))
    _press(panel, state, "cancel")
    assert asyncio.run(state.get_state()) is None
    assert "Отменено" in panel.edited[-1] and calls["create"] == []


# --- статусы шагов: ✅ = шаг пройден (проверка владельцем 01.11.2026) ------------------


def _marks(text: str) -> dict[int, str]:
    """Номер шага -> значок: строки вида «✅ 1 Марка: …»."""
    import re

    return {
        int(match.group(2)): match.group(1)
        for match in re.finditer(r"^(\S+) (\d) \w+:", text, re.M)
    }


def test_choosing_all_brands_marks_brand_and_skipped_model_done() -> None:
    """Регрессия: «Все марки» оставалось ⬜, авто-пропуск модели — тоже."""
    draft, _ = fp.apply_choice(fp.new_draft(), "brand", "0")
    marks = _marks(fp.render_text(draft, fp.STEP_YEARS))
    assert marks[1] == "✅" and marks[2] == "✅"
    assert marks[4] == "⬜"  # до пробега ещё не дошли


def test_default_region_turns_done_once_chosen() -> None:
    draft = fp.new_draft()
    assert _marks(fp.render_text(draft, fp.STEP_BRAND))[7] == "⬜"
    draft, _ = fp.apply_choice(draft, "region", LOCAL)  # «вся КР» — тоже ответ
    assert _marks(fp.render_text(draft, fp.STEP_BRAND))[7] == "✅"


def test_any_choice_counts_as_done() -> None:
    draft, _ = fp.apply_choice(fp.new_draft(), "mileage", "any")
    draft, _ = fp.apply_choice(draft, "market", "off")
    marks = _marks(fp.render_text(draft, fp.STEP_BRAND))
    assert marks[4] == "✅" and marks[6] == "✅" and marks[5] == "⬜"


def test_typed_value_marks_step_done() -> None:
    draft, _ = fp.apply_text(fp.new_draft(), fp.STEP_YEARS, "2015-2020")
    assert fp.STEP_YEARS in draft["done"]


def test_saved_filter_opens_with_every_step_done() -> None:
    draft = fp.draft_from_filter(_dto(brand_id=None, brand_name=None, car_model_id=None, model_name=None, region=LOCAL))
    marks = _marks(fp.render_text(draft, fp.STEP_PRICE, editing=True))
    assert all(marks[n] == "✅" for n in fp.STEPS if n != fp.STEP_PRICE)
