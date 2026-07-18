"""Задача «свежесть данных lalafo»: автодеактивация зависших объявлений
(deactivate_stale_listings), точечный валидатор карточек и фильтр
last_seen_at в дневных агрегатах."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import (
    Brand,
    CarModel,
    DailyMarketStat,
    Listing,
    ListingStatusHistory,
    Source,
)
from app.parsers.base import FetchPage, ParserAdapter
from app.services.stats import rebuild_daily_market_stats
from app.services.sync import deactivate_stale_listings, validate_stale_lalafo_listings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite теряет tz у DateTime(timezone=True) — выравниваем для сравнения."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _session() -> Session:
    import app.models  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    lalafo = Source(name="lalafo.kg")
    mashina = Source(name="mashina.kg")
    brand = Brand(name="Toyota")
    session.add_all([lalafo, mashina, brand])
    session.flush()
    session.add(CarModel(brand_id=brand.id, name="Camry"))
    session.commit()
    return session


def _add_listing(
    session: Session,
    *,
    external_id: str,
    source_name: str = "lalafo.kg",
    last_seen_days_ago: int = 0,
    price_usd: Decimal | None = Decimal("10000"),
) -> Listing:
    source_id = session.scalar(select(Source.id).where(Source.name == source_name))
    brand_id = session.scalar(select(Brand.id))
    model_id = session.scalar(select(CarModel.id))
    listing = Listing(
        source_id=source_id,
        external_id=external_id,
        brand_id=brand_id,
        car_model_id=model_id,
        title="Toyota Camry 2019",
        year=2019,
        mileage_km=90_000,
        current_price_usd=price_usd,
        analytics_eligible=True,
        is_active=True,
        first_seen_at=_now() - timedelta(days=max(last_seen_days_ago, 40)),
        last_seen_at=_now() - timedelta(days=last_seen_days_ago),
        payload_hash="x" * 8,
    )
    session.add(listing)
    session.commit()
    return listing


def test_deactivate_stale_by_last_seen() -> None:
    session = _session()
    stale = _add_listing(session, external_id="1", last_seen_days_ago=31)
    alive = _add_listing(session, external_id="2", last_seen_days_ago=1)

    count = deactivate_stale_listings(session, stale_days=30)
    session.commit()

    assert count == 1
    session.refresh(stale)
    session.refresh(alive)
    assert stale.is_active is False
    assert stale.status == "inactive"
    assert stale.deactivated_at is not None
    # Видимое вчера объявление живет: семантика именно last_seen_at.
    assert alive.is_active is True and alive.deactivated_at is None

    event = session.scalar(
        select(ListingStatusHistory).where(ListingStatusHistory.listing_id == stale.id)
    )
    assert event is not None
    assert event.event_type == "deactivated"
    assert event.old_status == "active" and event.new_status == "inactive"
    assert event.details == {"reason": "stale"}

    # Повторный запуск в тот же день ничего не находит (идемпотентность).
    assert deactivate_stale_listings(session, stale_days=30) == 0


def test_deactivate_stale_scoped_to_lalafo_only() -> None:
    session = _session()
    _add_listing(session, external_id="1", source_name="mashina.kg", last_seen_days_ago=31)

    count = deactivate_stale_listings(session, stale_days=30)
    session.commit()

    # У mashina своя деактивация через weekend full-scan — не трогаем.
    assert count == 0
    listing = session.scalar(select(Listing))
    assert listing.is_active is True


class FakeCardParser(ParserAdapter):
    """Карточки lalafo: 410 Gone для «умерших» id, живым — status_id=2."""

    source_name = "lalafo.kg"
    needs_detail_enrichment = True

    def __init__(self, gone_ids: set[int], status_ids: dict[int, int] | None = None) -> None:
        self.gone_ids = gone_ids
        self.status_ids = status_ids or {}
        self.detail_calls: list[int] = []

    def fetch_listings(self, **_: Any) -> FetchPage:
        raise NotImplementedError

    def normalize(self, raw: dict[str, Any], *, fallback_make: str | None = None):
        raise NotImplementedError

    def fetch_details(self, external_id: str | int) -> dict[str, Any]:
        ad_id = int(external_id)
        self.detail_calls.append(ad_id)
        if ad_id in self.gone_ids:
            request = httpx.Request("GET", f"https://lalafo.kg/api/search/v3/feed/details/{ad_id}")
            response = httpx.Response(410, request=request)
            raise httpx.HTTPStatusError("410 Gone", request=request, response=response)
        return {"id": ad_id, "status_id": self.status_ids.get(ad_id, 2)}

    def close(self) -> None:
        pass


def test_validator_deactivates_gone_and_prolongs_alive() -> None:
    session = _session()
    source = session.scalar(select(Source).where(Source.name == "lalafo.kg"))
    gone = _add_listing(session, external_id="1", last_seen_days_ago=5)
    alive = _add_listing(session, external_id="2", last_seen_days_ago=5)
    recent = _add_listing(session, external_id="3", last_seen_days_ago=1)

    parser = FakeCardParser(gone_ids={1})
    checked, deactivated = validate_stale_lalafo_listings(
        session, parser, source, validate_after_days=3, max_per_run=10
    )
    session.commit()

    # Недавно виденное объявление карточкой не проверялось.
    assert parser.detail_calls == [1, 2]
    assert (checked, deactivated) == (2, 1)

    session.refresh(gone)
    assert gone.is_active is False and gone.status == "inactive"
    event = session.scalar(
        select(ListingStatusHistory).where(ListingStatusHistory.listing_id == gone.id)
    )
    assert event.event_type == "deactivated" and event.details == {"reason": "card_gone"}

    # Живая карточка продлевает last_seen_at -> объявление остаётся в статистике.
    session.refresh(alive)
    assert alive.is_active is True
    assert _aware(alive.last_seen_at) >= _now() - timedelta(minutes=1)
    session.refresh(recent)
    assert _aware(recent.last_seen_at) < _now() - timedelta(hours=12)


def test_validator_keeps_active_on_unknown_status_id_and_respects_cap() -> None:
    session = _session()
    source = session.scalar(select(Source).where(Source.name == "lalafo.kg"))
    weird = _add_listing(session, external_id="1", last_seen_days_ago=6)
    _add_listing(session, external_id="2", last_seen_days_ago=5)

    # Незнакомый status_id: не деактивируем, но и объявление живо (last_seen обновлён).
    parser = FakeCardParser(gone_ids=set(), status_ids={1: 7})
    checked, deactivated = validate_stale_lalafo_listings(
        session, parser, source, validate_after_days=3, max_per_run=1
    )
    session.commit()

    # Кап max_per_run=1: проверен только самый давний кандидат.
    assert parser.detail_calls == [1]
    assert (checked, deactivated) == (1, 0)
    session.refresh(weird)
    assert weird.is_active is True
    assert _aware(weird.last_seen_at) >= _now() - timedelta(minutes=1)


def test_stats_exclude_listings_not_seen_recently() -> None:
    session = _session()
    _add_listing(session, external_id="1", last_seen_days_ago=31, price_usd=Decimal("20000"))
    _add_listing(session, external_id="2", last_seen_days_ago=1, price_usd=Decimal("10000"))

    written = rebuild_daily_market_stats(session, max_last_seen_days=30)

    assert written == 1
    stat = session.scalar(select(DailyMarketStat))
    # Давно не виденное объявление с ценой 20000 не попало в агрегат.
    assert stat.sample_count == 1
    assert stat.avg_price_usd == Decimal("10000")
