"""Недельные графики: цены по неделям и приток новых объявлений.

Месячная сетка сглаживала ровно то, ради чего на график смотрят, поэтому шаг
стал настраиваемым (`granularity`), а рядом с ценой появился второй ряд —
сколько объявлений появилось за период.

Тесты держат три обещания, которые легко нарушить незаметно:
1. недельный ключ группировки действительно недельный, а месячный не съехал;
2. приток считает ВСЕ объявления, включая снятые с продажи (иначе старые недели
   пустеют задним числом и график рисует несуществующий рост);
3. `sample_count` у цены — среднее за сутки, а не сумма суточных срезов.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Brand, CarModel, DailyMarketStat, Listing, Source
from app.services.vehicle_summary import _intake_points, _trend_points, get_vehicle_summary


def _session() -> tuple[Session, Brand, CarModel]:
    import app.models  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    source = Source(name="mashina.kg")
    brand = Brand(name="Toyota")
    session.add_all([source, brand])
    session.flush()
    model = CarModel(brand_id=brand.id, name="Camry")
    session.add(model)
    session.flush()
    return session, brand, model


def _stat(
    session: Session,
    brand: Brand,
    model: CarModel,
    *,
    stat_date: date,
    avg: int,
    median: int,
    samples: int,
) -> None:
    session.add(
        DailyMarketStat(
            stat_date=stat_date,
            brand_id=brand.id,
            car_model_id=model.id,
            year=2024,
            mileage_bucket="0-50k",
            customs_status="unknown",
            avg_price_usd=Decimal(avg),
            median_price_usd=Decimal(median),
            min_price_usd=Decimal(avg),
            max_price_usd=Decimal(avg),
            sample_count=samples,
        )
    )


def _listing(
    session: Session,
    brand: Brand,
    model: CarModel,
    *,
    external_id: str,
    first_seen_at: datetime,
    is_active: bool = True,
    price_usd: int = 25_000,
) -> None:
    session.add(
        Listing(
            source_id=1,
            external_id=external_id,
            brand_id=brand.id,
            car_model_id=model.id,
            title=f"Toyota Camry {external_id}",
            year=2024,
            current_price_usd=Decimal(price_usd),
            is_active=is_active,
            analytics_eligible=is_active,
            first_seen_at=first_seen_at,
            last_seen_at=first_seen_at,
            payload_hash=f"hash-{external_id}",
        )
    )


def test_trend_groups_by_week_and_averages_daily_snapshots() -> None:
    session, brand, model = _session()
    # Понедельник и среда одной недели плюс понедельник следующей.
    monday = date.today() - timedelta(days=date.today().weekday() + 7)
    _stat(session, brand, model, stat_date=monday, avg=20_000, median=20_000, samples=10)
    _stat(
        session, brand, model, stat_date=monday + timedelta(days=2), avg=22_000, median=21_000, samples=10
    )
    _stat(
        session, brand, model, stat_date=monday + timedelta(days=7), avg=30_000, median=30_000, samples=4
    )
    session.flush()

    points = _trend_points(
        session,
        brand_id=brand.id,
        car_model_id=model.id,
        generation_id=None,
        year=2024,
        granularity="week",
    )

    assert [p["period_start"] for p in points] == [monday, monday + timedelta(days=7)]
    assert points[0]["average_price_usd"] == 21_000.0
    # Медиана недели — медиана дневных медиан, а не их средневзвешенное.
    assert points[0]["median_price_usd"] == 20_500.0
    # Двадцать строк за два дня — это десять машин, а не двадцать.
    assert points[0]["sample_count"] == 10


def test_trend_month_granularity_still_groups_by_month() -> None:
    session, brand, model = _session()
    first = date.today().replace(day=1)
    _stat(session, brand, model, stat_date=first, avg=20_000, median=20_000, samples=5)
    _stat(session, brand, model, stat_date=first + timedelta(days=20), avg=20_000, median=20_000, samples=5)
    session.flush()

    points = _trend_points(
        session,
        brand_id=brand.id,
        car_model_id=model.id,
        generation_id=None,
        year=2024,
        granularity="month",
    )

    assert len(points) == 1
    assert points[0]["period"] == first.strftime("%Y-%m")
    assert points[0]["period_start"] == first


def test_intake_counts_inactive_listings_and_fills_gaps() -> None:
    session, brand, model = _session()
    now = datetime.now(timezone.utc)
    three_weeks_ago = now - timedelta(days=21)
    # Снятое с продажи объявление появилось на рынке ровно так же, как живое:
    # без него старые недели пустели бы задним числом.
    _listing(session, brand, model, external_id="a", first_seen_at=three_weeks_ago, is_active=False)
    _listing(session, brand, model, external_id="b", first_seen_at=three_weeks_ago)
    _listing(session, brand, model, external_id="c", first_seen_at=now)
    session.flush()

    points = _intake_points(
        session,
        brand_id=brand.id,
        car_model_id=model.id,
        generation_id=None,
        year=2024,
        granularity="week",
    )

    assert points[0]["count"] == 2
    assert points[-1]["count"] == 1
    # Между первой и последней неделей — честные нули, а не пропуск периодов.
    assert len(points) == 4
    assert [p["count"] for p in points[1:3]] == [0, 0]


def test_intake_does_not_backfill_zeros_before_first_listing() -> None:
    session, brand, model = _session()
    _listing(
        session,
        brand,
        model,
        external_id="a",
        first_seen_at=datetime.now(timezone.utc),
    )
    session.flush()

    points = _intake_points(
        session,
        brand_id=brand.id,
        car_model_id=model.id,
        generation_id=None,
        year=2024,
        granularity="week",
    )

    # Полугодовое окно, но данных до этой недели нет — рисовать «мёртвый рынок»
    # там, где просто нет наших данных, нельзя.
    assert len(points) == 1


def test_anomalies_return_full_list_not_top_ten() -> None:
    """Панель фильтрует и листает аномалии сама — обрезать их на бэкенде нельзя.

    Пока сервис отдавал `items[:10]`, рядом с честным «Всего: 34» стояла таблица
    из пяти строк, и расхождение читалось как ошибка. Регрессия сторожит именно
    полноту списка: количество и счётчики low/high и раньше считались по всем.
    """
    session, brand, model = _session()
    now = datetime.now(timezone.utc)
    # Плотное ядро: по нему IQR построит узкий коридор.
    for i in range(40):
        _listing(
            session,
            brand,
            model,
            external_id=f"core-{i}",
            first_seen_at=now,
            price_usd=25_000 + i,
        )
    # Дюжина явных выбросов вниз — больше прежнего потолка в 10 строк.
    for i in range(12):
        _listing(
            session,
            brand,
            model,
            external_id=f"low-{i}",
            first_seen_at=now,
            price_usd=5_000 + i,
        )
    session.flush()

    summary = get_vehicle_summary(session, brand_id=brand.id, model_id=model.id, year=2024)

    assert summary["anomalies"]["low_count"] == 12
    assert len(summary["anomalies"]["items"]) == summary["anomalies"]["count"]
    assert len(summary["anomalies"]["items"]) > 10
