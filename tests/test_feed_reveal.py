"""Плавное появление новых в ленте сайта: обход расписывает `feed_at`."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Listing
from app.services.normalizer import normalize_mashina_listing
from app.services.sync import run_sync
from tests.test_rich_cards import _FeedParser, _lalafo_setup, _mashina_raw, _sync_session


class _MashinaFeed(_FeedParser):
    source_name = "mashina.kg"
    needs_detail_enrichment = False

    def normalize(self, raw, *, fallback_make=None):
        return normalize_mashina_listing(raw, fallback_make=fallback_make)


def test_sync_schedules_new_listings_for_gradual_feed() -> None:
    """Обход ставит новым `feed_at` в пределах 5 минут; повторная встреча его не трогает."""
    from datetime import datetime, timedelta, timezone

    session = _sync_session()
    source, target = _lalafo_setup(session)
    source.name = "mashina.kg"
    session.commit()
    before = datetime.now(timezone.utc)
    run_sync(session, _MashinaFeed([_mashina_raw()], {}), source, target, mode="fresh")
    listing = session.scalar(select(Listing))
    planned = listing.feed_at.replace(tzinfo=timezone.utc) if listing.feed_at.tzinfo is None else listing.feed_at
    assert before < planned <= before + timedelta(seconds=305)
    run_sync(session, _MashinaFeed([_mashina_raw(title="Toyota Camry 70 2.5 AT!")], {}), source, target, mode="fresh")
    session.refresh(listing)
    again = listing.feed_at.replace(tzinfo=timezone.utc) if listing.feed_at.tzinfo is None else listing.feed_at
    assert again == planned
