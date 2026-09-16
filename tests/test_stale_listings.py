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
from app.services.sync import (
    ValidationReport,
    deactivate_stale_listings,
    validate_stale_lalafo_listings,
)


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
    city: str | None = "Бишкек",
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
        city=city,
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

    def __init__(
        self,
        gone_ids: set[int],
        status_ids: dict[int, int] | None = None,
        failing_ids: set[int] | None = None,
    ) -> None:
        self.gone_ids = gone_ids
        self.status_ids = status_ids or {}
        self.failing_ids = failing_ids or set()
        self.detail_calls: list[int] = []

    def fetch_listings(self, **_: Any) -> FetchPage:
        raise NotImplementedError

    def normalize(self, raw: dict[str, Any], *, fallback_make: str | None = None):
        raise NotImplementedError

    def fetch_details(self, external_id: str | int) -> dict[str, Any]:
        ad_id = int(external_id)
        self.detail_calls.append(ad_id)
        if ad_id in self.failing_ids:
            raise httpx.ProxyError("407 TRAFFIC_EXHAUSTED")
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
    report = validate_stale_lalafo_listings(
        session, parser, source, validate_after_days=3, max_per_run=10
    )
    session.commit()

    # Недавно виденное объявление карточкой не проверялось.
    assert parser.detail_calls == [1, 2]
    assert (report.checked, report.deactivated, report.failed) == (2, 1, 0)
    assert report.drained is True and report.aborted is False
    assert report.fallback_allowed is True

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
    report = validate_stale_lalafo_listings(
        session, parser, source, validate_after_days=3, max_per_run=1
    )
    session.commit()

    # Кап max_per_run=1: проверен только самый давний кандидат.
    assert parser.detail_calls == [1]
    assert (report.checked, report.deactivated) == (1, 0)
    # Очередь не исчерпана: фолбэку разрешено гасить только до границы,
    # до которой валидатор дошёл (last_seen_at кандидата ДО продления).
    assert report.drained is False
    assert _aware(report.reached_last_seen_at) < _now() - timedelta(days=5)
    session.refresh(weird)
    assert weird.is_active is True
    assert _aware(weird.last_seen_at) >= _now() - timedelta(minutes=1)


def test_validator_deactivates_dead_status_ids() -> None:
    """API-карточка отвечает 200 и на снятые объявления: status_id=8 (архив)
    и 3 (заблокировано) — публичная страница у них 410. Это мёртвые, а не
    «живые с незнакомым статусом»: деактивируем и НЕ продлеваем last_seen_at."""
    session = _session()
    source = session.scalar(select(Source).where(Source.name == "lalafo.kg"))
    archived = _add_listing(session, external_id="1", last_seen_days_ago=6)
    blocked = _add_listing(session, external_id="2", last_seen_days_ago=5)
    alive = _add_listing(session, external_id="3", last_seen_days_ago=4)

    parser = FakeCardParser(gone_ids=set(), status_ids={1: 8, 2: 3})
    report = validate_stale_lalafo_listings(
        session, parser, source, validate_after_days=3, max_per_run=10
    )
    session.commit()

    assert (report.checked, report.deactivated, report.failed) == (3, 2, 0)
    for listing, status_id in ((archived, 8), (blocked, 3)):
        session.refresh(listing)
        assert listing.is_active is False and listing.status == "inactive"
        assert _aware(listing.last_seen_at) < _now() - timedelta(days=4)
        event = session.scalar(
            select(ListingStatusHistory).where(ListingStatusHistory.listing_id == listing.id)
        )
        assert event.details == {"reason": "card_inactive", "status_id": status_id}
    session.refresh(alive)
    assert alive.is_active is True
    assert _aware(alive.last_seen_at) >= _now() - timedelta(minutes=1)


def test_validator_aborts_on_consecutive_failures_and_forbids_fallback() -> None:
    """Прокси лёг (407): серия ошибок подряд прерывает прогон, чтобы 5 ретраев ×
    тысячи карточек не держали worker часами. Фолбэку после такого прогона
    верить нельзя — иначе «не смогли посмотреть» превратится в «пропало»."""
    session = _session()
    source = session.scalar(select(Source).where(Source.name == "lalafo.kg"))
    for i in range(1, 8):
        _add_listing(session, external_id=str(i), last_seen_days_ago=10 - i)

    parser = FakeCardParser(gone_ids=set(), failing_ids=set(range(1, 8)))
    report = validate_stale_lalafo_listings(
        session, parser, source, validate_after_days=3, max_per_run=10, max_consecutive_failures=3
    )
    session.commit()

    assert parser.detail_calls == [1, 2, 3]
    assert report.aborted is True
    assert (report.checked, report.failed) == (0, 3)
    assert report.fallback_allowed is False
    # Ошибка вердикта не даёт: last_seen_at не тронут, объявления живы.
    for listing in session.scalars(select(Listing)):
        assert listing.is_active is True
        assert _aware(listing.last_seen_at) < _now() - timedelta(days=2)


def test_validator_failures_reset_on_success_and_majority_failure_blocks_fallback() -> None:
    session = _session()
    source = session.scalar(select(Source).where(Source.name == "lalafo.kg"))
    for i in range(1, 6):
        _add_listing(session, external_id=str(i), last_seen_days_ago=10 - i)

    # Ошибки чередуются с успехами — серия не набирается, прогон не прерван…
    parser = FakeCardParser(gone_ids=set(), failing_ids={1, 3, 5})
    report = validate_stale_lalafo_listings(
        session, parser, source, validate_after_days=3, max_per_run=10, max_consecutive_failures=2
    )
    assert report.aborted is False
    assert (report.checked, report.failed) == (2, 3)
    # …но ошибок больше, чем вердиктов: фолбэк всё равно под запретом.
    assert report.fallback_allowed is False
    assert ValidationReport(checked=3, failed=3).fallback_allowed is True
    assert ValidationReport(checked=3, failed=4).fallback_allowed is False


def test_stale_fallback_respects_validator_reach() -> None:
    """Фолбэк гасит только то, что валидатор уже пытался проверить: объявления
    старше 30 дней, до которых валидатору не хватило потолка, остаются живыми
    (до 16.09.2026 так вслепую уходило ~1600/сутки, треть — живые)."""
    session = _session()
    oldest = _add_listing(session, external_id="1", last_seen_days_ago=40)
    unreached = _add_listing(session, external_id="2", last_seen_days_ago=35)
    reach = _aware(oldest.last_seen_at)
    source = session.scalar(select(Source).where(Source.name == "lalafo.kg"))

    # Потолок 1: валидатор дошёл только до самого давнего (живое — продлено).
    parser = FakeCardParser(gone_ids=set())
    report = validate_stale_lalafo_listings(
        session, parser, source, validate_after_days=3, max_per_run=1
    )
    session.commit()
    assert report.drained is False and report.fallback_allowed is True
    assert _aware(report.reached_last_seen_at) == reach

    count = deactivate_stale_listings(
        session, stale_days=30, not_seen_since=report.reached_last_seen_at
    )
    session.commit()

    # 35-дневное объявление старше порога, но валидатор до него не дошёл — живо.
    assert count == 0
    session.refresh(oldest)
    session.refresh(unreached)
    assert oldest.is_active is True and unreached.is_active is True

    # Без границы (очередь исчерпана) — прежняя семантика: всё старше 30 дней.
    assert deactivate_stale_listings(session, stale_days=30) == 1
    session.commit()
    session.refresh(unreached)
    assert unreached.is_active is False


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


def test_stats_excludes_cohort_price_outlier() -> None:
    session = _session()
    for i in range(6):
        _add_listing(session, external_id=f"ok{i}", last_seen_days_ago=1, price_usd=Decimal("7000"))
    _add_listing(session, external_id="junk", last_seen_days_ago=1, price_usd=Decimal("520000"))

    rebuild_daily_market_stats(session, outlier_min_cohort=5, outlier_price_median_multiple=4.0)

    stat = session.scalar(select(DailyMarketStat))
    # Когорта модель+год из 6 нормальных + 1 выброс: $520k вне [median/4, median*4].
    assert stat.sample_count == 6
    assert stat.avg_price_usd == Decimal("7000")
    assert stat.max_price_usd == Decimal("7000")


def test_stats_keeps_outlier_in_small_cohort() -> None:
    session = _session()
    for i in range(2):
        _add_listing(session, external_id=f"ok{i}", last_seen_days_ago=1, price_usd=Decimal("7000"))
    _add_listing(session, external_id="junk", last_seen_days_ago=1, price_usd=Decimal("520000"))

    rebuild_daily_market_stats(session, outlier_min_cohort=5, outlier_price_median_multiple=4.0)

    stat = session.scalar(select(DailyMarketStat))
    # Когорта < 5 — граница неосмысленна, не трогаем (полагаемся на предохранитель A).
    assert stat.sample_count == 3
    assert stat.max_price_usd == Decimal("520000")


def test_stats_exclude_foreign_listings() -> None:
    """Машины «в Китае» — не местный рынок: ни в средней, ни в границах выбросов.

    Шесть зарубежных по $6000 против пяти местных по $10000: без исключения
    медиана когорты и средняя уехали бы вниз, а местные $10000 выглядели бы
    «дорого». Город без значения (NULL) остаётся местным.
    """
    session = _session()
    for i in range(5):
        _add_listing(session, external_id=f"kg{i}", price_usd=Decimal("10000"))
    for i in range(6):
        _add_listing(session, external_id=f"cn{i}", price_usd=Decimal("6000"), city="Китай")
    _add_listing(session, external_id="nocity", price_usd=Decimal("10000"), city=None)

    written = rebuild_daily_market_stats(
        session, outlier_min_cohort=5, outlier_price_median_multiple=4.0
    )

    assert written == 1
    stat = session.scalar(select(DailyMarketStat))
    assert stat.sample_count == 6
    assert stat.avg_price_usd == Decimal("10000")
    assert stat.min_price_usd == Decimal("10000")


def test_stats_stay_silent_when_only_foreign_listings_remain() -> None:
    """Когорта, державшаяся на зарубежном навесе, статистики не получает вовсе."""
    session = _session()
    for i in range(6):
        _add_listing(session, external_id=f"cn{i}", price_usd=Decimal("6000"), city="Китай")

    assert rebuild_daily_market_stats(session) == 0
    assert session.scalar(select(DailyMarketStat)) is None
