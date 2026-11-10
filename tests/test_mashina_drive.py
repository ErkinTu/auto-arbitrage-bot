"""Привод mashina (10.11.2026): в ленте API его нет — разметка по спискам
`drive_type`, как у топлива; обход пустым приводом её не затирает."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Listing
from app.parsers.base import FetchPage
from app.services.drive_labeling import PAGE_SIZE, label_mashina_drive
from app.services.sync import run_sync
from tests.test_fuel_filter import _MashinaFeed, _mashina_listings
from tests.test_rich_cards import _bot_session, _lalafo_setup, _mashina_raw, _sync_session


class _DriveParser:
    def __init__(self, ids: dict[str, list[int]], fail: frozenset[str] = frozenset()) -> None:
        self.ids, self.fail, self.calls = ids, fail, []

    def fetch_attribute_page(self, slug, value, *, page, size):
        assert slug == "drive_type"
        self.calls.append((value, page))
        if value in self.fail:
            raise RuntimeError("timeout")
        chunk = self.ids.get(value, [])[(page - 1) * size : page * size]
        return FetchPage(items=[{"id": i} for i in chunk], total=len(self.ids.get(value, [])))


def test_full_drive_labeling_pages_through_values() -> None:
    session = _bot_session()
    awd = list(range(1, PAGE_SIZE + 3))
    source_id = _mashina_listings(session, [*awd, 500, 600, 700])
    parser = _DriveParser({"полный": awd, "передний": [500], "задний": [600]})

    report = label_mashina_drive(session, parser, source_id, max_pages=None)

    drives = dict(session.execute(select(Listing.external_id, Listing.drive_type)).all())
    assert drives["1"] == drives[str(PAGE_SIZE + 2)] == "Полный"
    assert (drives["500"], drives["600"], drives["700"]) == ("Передний", "Задний", None)
    assert report.updated == len(awd) + 2 and not report.failed
    assert label_mashina_drive(session, parser, source_id, max_pages=None).updated == 0


def test_fresh_drive_labeling_one_page_each_and_error_isolated() -> None:
    session = _bot_session()
    source_id = _mashina_listings(session, [1, 2])
    parser = _DriveParser({"полный": [1], "передний": [2]}, fail=frozenset({"полный"}))
    report = label_mashina_drive(session, parser, source_id, max_pages=1)
    assert len(parser.calls) == 3 and report.failed == ["полный"]
    drives = dict(session.execute(select(Listing.external_id, Listing.drive_type)).all())
    assert drives == {"1": None, "2": "Передний"}


def test_sync_keeps_labeled_mashina_drive() -> None:
    session = _sync_session()
    source, target = _lalafo_setup(session)
    source.name = "mashina.kg"
    session.commit()
    run_sync(session, _MashinaFeed([_mashina_raw()], {}), source, target, mode="fresh")
    listing = session.scalar(select(Listing))
    listing.drive_type = "Полный"
    session.commit()
    run_sync(session, _MashinaFeed([_mashina_raw(title="Toyota Camry 70 2.5 AT!")], {}), source, target, mode="fresh")
    session.refresh(listing)
    assert listing.drive_type == "Полный"
