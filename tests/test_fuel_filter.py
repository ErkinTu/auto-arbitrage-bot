"""Фильтр по топливу (план 01.11.2026, п. Е).

Значения lalafo — реальные из базы на 01.11.2026; значения mashina — пять, что
принимает фильтр API (сумма по ним = 54 035 из 54 072 объявлений).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import app.bot.db as botdb
from app.bot import filter_panel as fp
from app.bot.matching import filter_matches
from app.bot.messages import format_filter_line
from app.models import Listing, Source, UserFilter
from app.parsers.base import FetchPage
from app.services.fuel import (
    DIESEL,
    ELECTRIC,
    HYBRID,
    PETROL,
    PETROL_GAS,
    fuel_group_from_lalafo,
    fuel_groups_label,
    pack_fuel_groups,
    unpack_fuel_groups,
)
from app.services.fuel_labeling import (
    PAGE_SIZE,
    backfill_lalafo_fuel_groups,
    label_mashina_fuel,
)
from app.services.normalizer import normalize_lalafo_listing, normalize_mashina_listing
from app.services.sync import run_sync
from tests.test_filter_panel import _fields, _seed, sessions  # noqa: F401 — фикстура
from tests.test_rich_cards import _bot_session, _FeedParser, _lalafo_setup, _listing, _mashina_raw, _sync_session

# --- разбор lalafo -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "group"),
    [
        ("Бензин", PETROL),
        ("Дизель", DIESEL),
        ("Гибрид", HYBRID),
        ("Электромобиль", ELECTRIC),
        ("Газ", PETROL_GAS),
        ("Бензин, Газ", PETROL_GAS),
        ("Газ-бензин (ГБО)", PETROL_GAS),
        ("Бензин, Газ-бензин (ГБО)", PETROL_GAS),
        ("Бензин, Газ, Газ-бензин (ГБО)", PETROL_GAS),
        ("Бензин, Гибрид", HYBRID),
        ("Бензин, Гибрид, Электромобиль", HYBRID),  # плагин — это гибрид (решение 01.11)
        ("Гибрид, Электромобиль", HYBRID),
        ("Бензин, Газ, Гибрид", HYBRID),
        ("Бензин, Электромобиль", HYBRID),
        ("Бензин, Дизель", None),  # противоречие — не доказано
        ("Бензин, Газ, Гибрид, Дизель, Электромобиль", None),
        ("Газ, Электромобиль", None),
        ("Водород", None),  # незнакомое
        (None, None),
        ("", None),
    ],
)
def test_lalafo_fuel_values_map_to_groups(value, group) -> None:
    assert fuel_group_from_lalafo(value) == group


def test_pack_is_canonical_and_all_or_nothing_is_none() -> None:
    assert pack_fuel_groups([ELECTRIC, HYBRID]) == "hybrid,electric"
    assert pack_fuel_groups([HYBRID, ELECTRIC, "junk"]) == "hybrid,electric"
    assert pack_fuel_groups([]) is None
    assert pack_fuel_groups([PETROL, PETROL_GAS, HYBRID, ELECTRIC, DIESEL]) is None
    assert unpack_fuel_groups("electric,hybrid") == (HYBRID, ELECTRIC)
    assert unpack_fuel_groups(None) == ()
    assert fuel_groups_label("hybrid,electric") == "Гибрид, Электро"
    assert fuel_groups_label(None) is None


# --- нормализатор и синк -------------------------------------------------------------------


def _lalafo_raw(fuel: str | None) -> dict:
    params = [{"name": "Топливо", "value": fuel}] if fuel else []
    return {"id": 1, "title": "Toyota Camry: 2016 г.", "params": params}


def test_lalafo_normalizer_sets_group_outside_payload_hash() -> None:
    item = normalize_lalafo_listing(_lalafo_raw("Бензин, Газ"), fallback_make="Toyota")
    assert (item.fuel_type, item.fuel_group) == ("Бензин, Газ", PETROL_GAS)
    assert "fuel_group" not in item.analytics_payload()
    item.fuel_group = None  # выкат новой колонки не меняет хэш
    assert item.payload_hash == normalize_lalafo_listing(
        _lalafo_raw("Бензин, Газ"), fallback_make="Toyota"
    ).payload_hash


def test_mashina_normalizer_knows_no_fuel() -> None:
    item = normalize_mashina_listing(_mashina_raw())
    assert item.fuel_type is None and item.fuel_group is None


class _MashinaFeed(_FeedParser):
    source_name = "mashina.kg"
    needs_detail_enrichment = False

    def normalize(self, raw, *, fallback_make=None):
        return normalize_mashina_listing(raw, fallback_make=fallback_make)


def test_sync_keeps_labeled_mashina_fuel() -> None:
    """Нормализатор mashina группы не знает — обход не затирает разметку."""
    session = _sync_session()
    source, target = _lalafo_setup(session)
    source.name = "mashina.kg"
    session.commit()
    run_sync(session, _MashinaFeed([_mashina_raw()], {}), source, target, mode="fresh")
    listing = session.scalar(select(Listing))
    listing.fuel_group = HYBRID
    session.commit()

    run_sync(session, _MashinaFeed([_mashina_raw(title="Toyota Camry 70 2.5 AT!")], {}), source, target, mode="fresh")
    session.refresh(listing)
    assert listing.fuel_group == HYBRID


# --- разметка mashina ------------------------------------------------------------------------


class _FuelParser:
    """API mashina «объявления типа X»: страницы по PAGE_SIZE."""

    def __init__(self, ids_by_fuel: dict[str, list[int]], fail: set[str] = frozenset()) -> None:
        self.ids_by_fuel = ids_by_fuel
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    def fetch_fuel_page(self, fuel_value, *, page, size):
        self.calls.append((fuel_value, page))
        if fuel_value in self.fail:
            raise RuntimeError("timeout")
        ids = self.ids_by_fuel.get(fuel_value, [])
        chunk = ids[(page - 1) * size : page * size]
        return FetchPage(items=[{"id": i} for i in chunk], total=len(ids))


def _mashina_listings(session, ids: list[int]) -> int:
    source_id = session.scalar(select(Source.id).where(Source.name == "mashina.kg"))
    for external_id in ids:
        _listing(session, external_id=str(external_id))
    return source_id


def test_full_labeling_pages_through_every_fuel() -> None:
    session = _bot_session()
    petrol_ids = list(range(1, PAGE_SIZE + 6))  # две страницы
    source_id = _mashina_listings(session, [*petrol_ids, 500, 600, 700])
    parser = _FuelParser({"бензин": petrol_ids, "гибрид": [500], "газ": [600], "дизель": [999]})

    report = label_mashina_fuel(session, parser, source_id, max_pages=None)

    groups = dict(session.execute(select(Listing.external_id, Listing.fuel_group)).all())
    assert groups["1"] == groups[str(PAGE_SIZE + 5)] == PETROL
    assert (groups["500"], groups["600"]) == (HYBRID, PETROL_GAS)
    assert groups["700"] is None  # ни в одном списке — не доказано
    assert ("бензин", 2) in parser.calls and ("бензин", 3) not in parser.calls
    assert report.updated == len(petrol_ids) + 2 and report.failed == []

    again = label_mashina_fuel(session, parser, source_id, max_pages=None)
    assert again.updated == 0  # идемпотентно


def test_fresh_labeling_reads_one_page_per_fuel() -> None:
    session = _bot_session()
    source_id = _mashina_listings(session, [1])
    parser = _FuelParser({"бензин": list(range(1, 3 * PAGE_SIZE))})
    label_mashina_fuel(session, parser, source_id, max_pages=1)
    assert len(parser.calls) == 5 and all(page == 1 for _, page in parser.calls)


def test_labeling_error_skips_only_that_fuel() -> None:
    session = _bot_session()
    source_id = _mashina_listings(session, [1, 2])
    parser = _FuelParser({"бензин": [1], "гибрид": [2]}, fail={"бензин"})
    report = label_mashina_fuel(session, parser, source_id, max_pages=None)
    groups = dict(session.execute(select(Listing.external_id, Listing.fuel_group)).all())
    assert groups == {"1": None, "2": HYBRID}
    assert report.failed == ["бензин"]


def test_lalafo_backfill_maps_distinct_values() -> None:
    session = _bot_session()
    source_id = session.scalar(select(Source.id))
    for n, fuel in enumerate(["Бензин", "Бензин, Газ", "Бензин, Дизель", None, "Гибрид, Электромобиль"]):
        _listing(session, external_id=str(n), fuel_type=fuel, fuel_group=DIESEL if n == 2 else None)
    backfill_lalafo_fuel_groups(session, source_id)
    groups = [row for row in session.scalars(select(Listing.fuel_group).order_by(Listing.id))]
    assert groups == [PETROL, PETROL_GAS, None, None, HYBRID]


# --- матчинг ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chosen", "listing_group", "expected"),
    [
        (None, None, True),  # «все типы» — шлём и неизвестное
        (None, DIESEL, True),
        ("hybrid,electric", HYBRID, True),
        ("hybrid,electric", ELECTRIC, True),
        ("hybrid,electric", PETROL, False),
        ("hybrid,electric", None, False),  # неизвестное под выбор не попадает
        ("petrol", PETROL_GAS, False),  # бензин/газ — отдельно от бензина
    ],
)
def test_filter_matches_fuel(chosen, listing_group, expected) -> None:
    listing = Listing(fuel_group=listing_group, city="Бишкек")
    assert filter_matches(listing, UserFilter(fuel_groups=chosen, skip_foreign=False)) is expected


def test_filter_line_shows_fuel_only_when_chosen() -> None:
    base = dict(
        brand_name=None,
        model_name=None,
        year_from=None,
        year_to=None,
        mileage_max_km=None,
        price_max_usd=None,
        below_market_only=False,
    )
    assert format_filter_line(**base) == "Все марки"
    assert format_filter_line(**base, fuel_groups="hybrid,electric") == "Все марки · гибрид, электро"


# --- хранение ----------------------------------------------------------------------------------


def test_fuel_is_saved_and_part_of_duplicate_check(sessions) -> None:  # noqa: F811
    with sessions() as s:
        _seed(s)
        s.commit()
    hybrid = botdb.create_filter(1, **_fields(fuel_groups="hybrid"))
    assert hybrid.fuel_groups == "hybrid"
    with pytest.raises(botdb.DuplicateFilterError):
        botdb.create_filter(1, **_fields(fuel_groups="hybrid"))
    edited = botdb.update_filter(1, hybrid.id, **_fields(fuel_groups="hybrid,electric"))
    assert botdb.get_filter(1, edited.id).fuel_groups == "hybrid,electric"


# --- панель -------------------------------------------------------------------------------------


def _buttons(draft: dict) -> list[str]:
    markup = fp.render_keyboard(draft, fp.STEP_FUEL)
    return [b.text for row in markup.inline_keyboard for b in row]


def test_fuel_is_the_eighth_step_and_fits_the_step_row() -> None:
    assert fp.STEPS[-1] == fp.STEP_FUEL == 8
    markup = fp.render_keyboard(fp.new_draft(), fp.STEP_FUEL)
    step_row = markup.inline_keyboard[-2]
    assert [b.text for b in step_row] == ["1", "2", "3", "4", "5", "6", "7", "[8]"]
    assert fp.next_step(fp.STEP_REGION) == fp.STEP_FUEL
    assert fp.next_step(fp.STEP_FUEL) == fp.STEP_FUEL


def test_fuel_toggles_several_and_stays_on_step() -> None:
    draft = fp.new_draft()
    assert "• 🌐 Все типы •" in _buttons(draft)
    draft, advance = fp.apply_choice(draft, "fuel", ELECTRIC)
    assert advance is False and fp.is_done(draft, fp.STEP_FUEL)
    draft, _ = fp.apply_choice(draft, "fuel", HYBRID)
    assert draft["fuel_groups"] == "hybrid,electric"
    assert {"✅ Гибрид", "✅ Электро", "▫️ Бензин", "🌐 Все типы"} <= set(_buttons(draft))
    assert "✅ 8 Топливо: Гибрид, Электро" in fp.render_text(draft, fp.STEP_REGION)

    draft, _ = fp.apply_choice(draft, "fuel", HYBRID)  # повторное нажатие снимает
    assert draft["fuel_groups"] == "electric"
    draft, _ = fp.apply_choice(draft, "fuel", "all")
    assert draft["fuel_groups"] is None
    assert fp.filter_fields(draft)["fuel_groups"] is None


def test_fuel_unknown_value_changes_nothing() -> None:
    draft = fp.new_draft()
    assert fp.apply_choice(draft, "fuel", "водород") == (draft, False)


def test_summary_mentions_fuel() -> None:
    draft = {**fp.new_draft(), "fuel_groups": "diesel"}
    assert "дизель" in fp.summary(draft)


def test_known_lalafo_listing_gets_group_from_stored_fuel_type() -> None:
    """Записанное до колонки fuel_group (старым ботом) получает группу при встрече в ленте."""
    from tests.test_rich_cards import _lalafo_item

    session = _sync_session()
    source, target = _lalafo_setup(session)
    item = _lalafo_item(None)
    run_sync(session, _FeedParser([item], {101: item}), source, target, mode="fresh")
    listing = session.scalar(select(Listing))
    listing.fuel_type, listing.fuel_group = "Бензин, Газ", None
    session.commit()

    run_sync(session, _FeedParser([item], {}), source, target, mode="fresh")  # та же цена
    session.refresh(listing)
    assert listing.fuel_group == PETROL_GAS
