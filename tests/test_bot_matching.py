from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.bot.matching import (
    filter_matches,
    find_new_listings,
    market_average,
    match_new_listings,
    preview_for_filter,
)
from app.db.base import Base
from app.models import (
    AccessBlock,
    BotUser,
    Brand,
    CarModel,
    DailyMarketStat,
    Listing,
    ListingStatusHistory,
    SentNotification,
    Source,
    UserFilter,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Сентинель "публикация прямо сейчас" для _add_listing: None — это валидное
# значение (дата публикации на источнике неизвестна).
_PUBLISHED_NOW = object()


def _session() -> Session:
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
    session.commit()
    return session


def _ids(session: Session) -> tuple[int, int, int]:
    source_id = session.scalar(select(Source.id))
    brand_id = session.scalar(select(Brand.id))
    model_id = session.scalar(select(CarModel.id))
    return source_id, brand_id, model_id


def _add_listing(
    session: Session,
    *,
    external_id: str,
    event_type: str = "new",
    observed_at: datetime | None = None,
    is_active: bool = True,
    price_usd: Decimal | None = Decimal("10000"),
    year: int | None = 2019,
    mileage_km: int | None = 90_000,
    customs_status: str = "unknown",
    with_model: bool = True,
    source_created_at: datetime | None | object = _PUBLISHED_NOW,
    city: str | None = "Бишкек",
) -> Listing:
    source_id, brand_id, model_id = _ids(session)
    listing = Listing(
        source_id=source_id,
        external_id=external_id,
        brand_id=brand_id,
        car_model_id=model_id if with_model else None,
        title=f"Toyota Camry {year}",
        source_slug=f"toyota-camry-{external_id}",
        year=year,
        mileage_km=mileage_km,
        city=city,
        customs_status=customs_status,
        current_price_usd=price_usd,
        is_active=is_active,
        source_created_at=_now() if source_created_at is _PUBLISHED_NOW else source_created_at,
        payload_hash="x" * 8,
    )
    session.add(listing)
    session.flush()
    session.add(
        ListingStatusHistory(
            listing_id=listing.id,
            event_type=event_type,
            old_status=None,
            new_status="active",
            observed_at=observed_at or _now(),
        )
    )
    session.commit()
    return listing


def _add_user_with_filter(
    session: Session, *, telegram_id: int = 111, **filter_kwargs
) -> UserFilter:
    _, brand_id, _ = _ids(session)
    user = BotUser(telegram_id=telegram_id)
    session.add(user)
    session.flush()
    user_filter = UserFilter(user_id=user.id, brand_id=brand_id, **filter_kwargs)
    session.add(user_filter)
    session.commit()
    return user_filter


def _add_paid_user_with_filter(
    session: Session,
    *,
    telegram_id: int,
    tier: str,
    days: int = 30,
    filter_age_hours: int = 3,
    **filter_kwargs,
) -> UserFilter:
    """Платный подписчик с блоком доступа известной скорости.

    Фильтр намеренно «старый»: точка отсчёта фильтра отсекает всё, что случилось
    до его создания, и без этого тесты про выдержку проверяли бы не выдержку.
    """
    now = _now()
    _, brand_id, _ = _ids(session)
    user = BotUser(
        telegram_id=telegram_id,
        access_status="subscriber",
        subscription_expires_at=now + timedelta(days=days),
    )
    session.add(user)
    session.flush()
    session.add(
        AccessBlock(
            user_id=user.id,
            tier=tier,
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=days),
            origin="payment",
        )
    )
    user_filter = UserFilter(
        user_id=user.id,
        brand_id=brand_id,
        created_at=now - timedelta(hours=filter_age_hours),
        **filter_kwargs,
    )
    session.add(user_filter)
    session.commit()
    return user_filter


def _add_stat(
    session: Session,
    *,
    avg: str,
    samples: int,
    stat_date: date | None = None,
    generation_id: int | None = None,
    mileage_bucket: str = "50-100k",
    customs_status: str = "cleared",
    year: int = 2019,
) -> None:
    _, brand_id, model_id = _ids(session)
    session.add(
        DailyMarketStat(
            stat_date=stat_date or date.today(),
            brand_id=brand_id,
            car_model_id=model_id,
            generation_id=generation_id,
            year=year,
            mileage_bucket=mileage_bucket,
            customs_status=customs_status,
            avg_price_usd=Decimal(avg),
            median_price_usd=Decimal(avg),
            min_price_usd=Decimal(avg),
            max_price_usd=Decimal(avg),
            sample_count=samples,
        )
    )
    session.commit()


def test_find_new_listings_window() -> None:
    session = _session()
    since = _now() - timedelta(minutes=60)

    fresh = _add_listing(session, external_id="1")
    # Подешевевшее в окне — тоже кандидат (ловит «цена упала после показа»).
    dropped = _add_listing(session, external_id="2", event_type="price_changed")
    # Событие вне окна since — не кандидат (и для new, и для price_changed).
    _add_listing(session, external_id="3", observed_at=_now() - timedelta(hours=3))
    _add_listing(
        session,
        external_id="6",
        event_type="price_changed",
        observed_at=_now() - timedelta(hours=3),
    )
    _add_listing(session, external_id="4", is_active=False)
    _add_listing(session, external_id="5", price_usd=None)

    found = find_new_listings(session, since)
    assert {listing.id for listing in found} == {fresh.id, dropped.id}


def test_find_new_listings_price_changed_ignores_publication_age() -> None:
    """price_changed берётся по времени изменения цены, а не дате публикации:
    старое объявление, только что подешевевшее, — легитимный кандидат. У new,
    наоборот, старая публикация (VIP-поднятие) отсекается."""
    session = _session()
    since = _now() - timedelta(minutes=60)
    old_publication = _now() - timedelta(days=30)

    # Старая публикация + свежее падение цены -> кандидат.
    dropped = _add_listing(
        session,
        external_id="1",
        event_type="price_changed",
        source_created_at=old_publication,
    )
    # Старая публикация + событие new (VIP-поднятие) -> НЕ кандидат.
    _add_listing(session, external_id="2", event_type="new", source_created_at=old_publication)

    found = find_new_listings(session, since, max_age_hours=12)
    assert [listing.id for listing in found] == [dropped.id]


def test_find_new_listings_publication_freshness() -> None:
    """VIP-поднятое старое объявление впервые видно боту (событие new),
    но публикация на источнике старая -> пуша нет."""
    session = _session()
    since = _now() - timedelta(minutes=60)

    fresh = _add_listing(session, external_id="1", source_created_at=_now() - timedelta(hours=11))
    _add_listing(session, external_id="2", source_created_at=_now() - timedelta(hours=13))
    # Дата публикации неизвестна -> не уведомлять (принцип filter_matches).
    _add_listing(session, external_id="3", source_created_at=None)

    found = find_new_listings(session, since, max_age_hours=12)
    assert [listing.id for listing in found] == [fresh.id]


def test_filter_matches_matrix() -> None:
    session = _session()
    _, brand_id, model_id = _ids(session)
    listing = Listing(
        brand_id=brand_id,
        car_model_id=model_id,
        year=2018,
        mileage_km=120_000,
        current_price_usd=Decimal("15000"),
    )

    def f(**kwargs) -> UserFilter:
        return UserFilter(brand_id=brand_id, **kwargs)

    assert filter_matches(listing, f())
    assert filter_matches(listing, f(car_model_id=model_id))
    assert not filter_matches(listing, f(car_model_id=model_id + 999))
    assert not filter_matches(Listing(brand_id=brand_id + 999), f())
    # Годы: границы включительно.
    assert filter_matches(listing, f(year_from=2018, year_to=2018))
    assert not filter_matches(listing, f(year_from=2019))
    assert not filter_matches(listing, f(year_to=2017))
    # Пробег и цена: границы включительно.
    assert filter_matches(listing, f(mileage_max_km=120_000))
    assert not filter_matches(listing, f(mileage_max_km=119_999))
    assert filter_matches(listing, f(price_max_usd=Decimal("15000")))
    assert not filter_matches(listing, f(price_max_usd=Decimal("14999")))
    # Граница задана, а у объявления поле неизвестно -> не матч.
    unknown = Listing(brand_id=brand_id, year=None, mileage_km=None, current_price_usd=None)
    assert filter_matches(unknown, f())
    assert not filter_matches(unknown, f(year_from=2010))
    assert not filter_matches(unknown, f(mileage_max_km=300_000))
    assert not filter_matches(unknown, f(price_max_usd=Decimal("99999")))
    # brand_id=None = любая марка; остальные ограничения продолжают работать.
    any_brand = UserFilter(brand_id=None)
    assert filter_matches(listing, any_brand)
    assert filter_matches(Listing(brand_id=brand_id + 999), any_brand)
    assert not filter_matches(listing, UserFilter(brand_id=None, year_from=2019))
    # skip_foreign: отсекает только страны из списка; локальные и неизвестный
    # город проходят (в т.ч. село Люксембург — не путать со страной).
    foreign = Listing(brand_id=brand_id, city="Китай")
    assert filter_matches(foreign, f())
    assert not filter_matches(foreign, f(skip_foreign=True))
    assert filter_matches(Listing(brand_id=brand_id, city="Бишкек"), f(skip_foreign=True))
    assert filter_matches(Listing(brand_id=brand_id, city="с. Люксембург"), f(skip_foreign=True))
    assert filter_matches(Listing(brand_id=brand_id, city=None), f(skip_foreign=True))
    # region: NULL = «Все авто» (без ограничения); иначе город должен попасть
    # в выбранный регион, неизвестный/спорный город = не матч.
    bishkek = Listing(brand_id=brand_id, city="Бишкек")
    osh = Listing(brand_id=brand_id, city="Ош")
    china = Listing(brand_id=brand_id, city="Китай")
    assert filter_matches(bishkek, f())  # region=None
    assert filter_matches(bishkek, f(region="bishkek"))
    assert not filter_matches(bishkek, f(region="chuy"))
    assert filter_matches(Listing(brand_id=brand_id, city="Кант"), f(region="chuy"))
    assert filter_matches(osh, f(region="osh_city"))
    assert not filter_matches(osh, f(region="osh_region"))  # город Ош ≠ Ошская обл.
    assert filter_matches(Listing(brand_id=brand_id, city="Ноокат"), f(region="osh_region"))
    assert filter_matches(china, f(region="foreign"))
    assert not filter_matches(bishkek, f(region="foreign"))
    # Спорный/неизвестный город под конкретный регион не матчится, но «Все авто» — да.
    disputed = Listing(brand_id=brand_id, city="Кызыл-Туу")
    assert filter_matches(disputed, f())
    assert not filter_matches(disputed, f(region="issykkul"))
    # «Местный рынок» (local) — любой не-зарубежный, включая спорные/неизвестные.
    assert filter_matches(bishkek, f(region="local"))
    assert filter_matches(disputed, f(region="local"))
    assert not filter_matches(china, f(region="local"))


def test_market_average() -> None:
    session = _session()
    _add_stat(session, avg="12000", samples=10)

    listing = _add_listing(session, external_id="1", price_usd=Decimal("10000"))
    assert market_average(session, listing, min_samples=5) == 12000.0


def test_market_average_insufficient_data() -> None:
    session = _session()
    listing = _add_listing(session, external_id="1", price_usd=Decimal("10000"))

    # Нет статистики вообще.
    assert market_average(session, listing, min_samples=5) is None
    # Мало образцов.
    _add_stat(session, avg="12000", samples=3)
    assert market_average(session, listing, min_samples=5) is None
    # Нет года у объявления.
    no_year = _add_listing(session, external_id="2", year=None)
    assert market_average(session, no_year, min_samples=5) is None


def test_market_average_is_none_for_foreign_listing() -> None:
    """Машину «в Китае» не с чем сравнивать: агрегаты — только местный рынок.

    Без этого почти любое зарубежное объявление выглядело бы «на 30% ниже
    рынка» просто потому, что цена без растаможки и доставки — другой товар.
    """
    session = _session()
    _add_stat(session, avg="12000", samples=10)

    foreign = _add_listing(session, external_id="1", price_usd=Decimal("8000"), city="Китай")
    assert market_average(session, foreign, min_samples=5) is None
    # Та же машина в Бишкеке — статистики хватает.
    local = _add_listing(session, external_id="2", price_usd=Decimal("8000"))
    assert market_average(session, local, min_samples=5) == 12000.0


def test_foreign_listing_is_not_pushed_as_below_market() -> None:
    session = _session()
    since = _now() - timedelta(minutes=60)
    _add_stat(session, avg="12000", samples=10)
    _add_user_with_filter(session, telegram_id=42, below_market_only=True)

    _add_listing(session, external_id="cn", price_usd=Decimal("8000"), city="Китай")
    local = _add_listing(session, external_id="kg", price_usd=Decimal("8000"))

    pending = match_new_listings(session, since, factor=0.95, min_samples=5)
    assert [note.listing_id for note in pending] == [local.id]


def test_market_average_weighted_and_latest_date() -> None:
    session = _session()
    # Старая дата должна игнорироваться.
    _add_stat(session, avg="20000", samples=50, stat_date=date.today() - timedelta(days=7))
    # Последняя дата: два ряда (например, разные группы растаможки при unknown у объявления):
    # (10000*8 + 16000*2) / 10 = 11200.
    _add_stat(session, avg="10000", samples=8, customs_status="cleared")
    _add_stat(session, avg="16000", samples=2, customs_status="not_cleared")

    listing = _add_listing(session, external_id="1", price_usd=Decimal("10000"))
    assert market_average(session, listing, min_samples=5) == 11200.0


def test_market_average_null_mileage_aggregates_all_buckets() -> None:
    session = _session()
    # Разные бакеты пробега одной модель+год; поодиночке ни один не даёт 5 образцов.
    _add_stat(session, avg="10000", samples=3, mileage_bucket="0-50k", customs_status="cleared")
    _add_stat(session, avg="8000", samples=3, mileage_bucket="150-200k", customs_status="cleared")

    # NULL-пробег: сравниваем со всей годовой когортой -> (10000*3 + 8000*3)/6 = 9000.
    null_mileage = _add_listing(
        session, external_id="1", mileage_km=None, price_usd=Decimal("7000")
    )
    assert market_average(session, null_mileage, min_samples=5) == 9000.0

    # Известный пробег: только свой бакет (0-50k, 3 образца) -> порог 5 не выполнен.
    known = _add_listing(session, external_id="2", mileage_km=30_000, price_usd=Decimal("7000"))
    assert market_average(session, known, min_samples=5) is None


def test_market_average_ignores_customs_status() -> None:
    """Растаможка не делит когорту: lalafo-'cleared' сравнивается со всем парком.

    Иначе объявления lalafo с заполненной растаможкой сравнивались бы лишь друг с
    другом (mashina это поле не отдаёт) и не набирали min_samples.
    """
    session = _session()
    _add_stat(session, avg="10000", samples=8, customs_status="cleared")
    _add_stat(session, avg="16000", samples=8, customs_status="not_cleared")

    # (10000*8 + 16000*8) / 16 = 13000 — независимо от растаможки самого объявления.
    for external_id, customs in (("1", "not_cleared"), ("2", "cleared"), ("3", "unknown")):
        listing = _add_listing(
            session, external_id=external_id, price_usd=Decimal("15000"), customs_status=customs
        )
        assert market_average(session, listing, min_samples=5) == 13000.0


def test_market_average_customs_only_cohort_reaches_min_samples() -> None:
    """Разбор 2026-07-25: когорта из разных групп растаможки складывается и берёт порог."""
    session = _session()
    # Поодиночке ни одна группа не даёт 5 образцов, вместе — 6.
    _add_stat(session, avg="6000", samples=1, customs_status="cleared")
    _add_stat(session, avg="7000", samples=5, customs_status="unknown")

    listing = _add_listing(
        session, external_id="1", price_usd=Decimal("3000"), customs_status="cleared"
    )
    # (6000*1 + 7000*5) / 6 ≈ 6833.33 — раньше здесь была бы None (1 образец).
    assert market_average(session, listing, min_samples=5) == pytest.approx(6833.33, abs=0.01)


def test_match_new_listings_dedup() -> None:
    session = _session()
    since = _now() - timedelta(minutes=60)
    _add_user_with_filter(session, telegram_id=42)
    listing = _add_listing(session, external_id="1")

    pending = match_new_listings(session, since, factor=0.95, min_samples=5)
    assert len(pending) == 1
    note = pending[0]
    assert note.chat_id == 42
    assert note.listing_id == listing.id
    assert note.url == "https://www.mashina.kg/details/toyota-camry-1/"
    assert note.price_usd == 10000.0
    assert note.below_market_percent is None  # статистики нет

    assert session.scalar(select(SentNotification.listing_id)) == listing.id
    # Повторный вызов -> дубля нет.
    assert match_new_listings(session, since, factor=0.95, min_samples=5) == []


def test_match_below_market_only_filter() -> None:
    session = _session()
    since = _now() - timedelta(minutes=60)
    _add_user_with_filter(session, telegram_id=42, below_market_only=True)

    # Без статистики тумблер трактует None как "не матч" -> тишина.
    first = _add_listing(session, external_id="1", price_usd=Decimal("10000"))
    assert match_new_listings(session, since, factor=0.95, min_samples=5) == []

    # Со статистикой дешевое авто проходит, и процент подсчитан. Первое объявление
    # не было отправлено и еще в окне lookback -> теперь тоже легитимно проходит.
    _add_stat(session, avg="12000", samples=10)
    cheap = _add_listing(session, external_id="2", price_usd=Decimal("10200"))
    pending = match_new_listings(session, since, factor=0.95, min_samples=5)
    assert {note.listing_id for note in pending} == {first.id, cheap.id}
    by_listing = {note.listing_id: note for note in pending}
    assert by_listing[cheap.id].below_market_percent == 15.0
    assert by_listing[cheap.id].market_avg_usd == 12000.0

    # Дорогое авто не проходит.
    _add_listing(session, external_id="3", price_usd=Decimal("11900"))
    assert match_new_listings(session, since, factor=0.95, min_samples=5) == []


def test_match_below_market_custom_percent() -> None:
    session = _session()
    since = _now() - timedelta(minutes=60)
    _add_user_with_filter(session, telegram_id=42, below_market_only=True, below_market_percent=15)
    _add_stat(session, avg="12000", samples=10)

    # 10% ниже средней — порог фильтра 15% не пройден, хотя глобальный factor 0.95 прошел бы.
    _add_listing(session, external_id="1", price_usd=Decimal("10800"))
    assert match_new_listings(session, since, factor=0.95, min_samples=5) == []

    # Ровно 15% ниже — граница включительно.
    cheap = _add_listing(session, external_id="2", price_usd=Decimal("10200"))
    pending = match_new_listings(session, since, factor=0.95, min_samples=5)
    assert [note.listing_id for note in pending] == [cheap.id]
    assert pending[0].below_market_percent == 15.0
    assert pending[0].market_avg_usd == 12000.0


def test_match_all_brands_filter() -> None:
    session = _session()
    since = _now() - timedelta(minutes=60)
    source_id, _, _ = _ids(session)

    user = BotUser(telegram_id=77)
    session.add(user)
    session.flush()
    session.add(UserFilter(user_id=user.id, brand_id=None))  # все марки
    other_brand = Brand(name="Honda")
    session.add(other_brand)
    session.flush()
    session.commit()

    toyota = _add_listing(session, external_id="1")
    honda = Listing(
        source_id=source_id,
        external_id="2",
        brand_id=other_brand.id,
        title="Honda Fit",
        source_slug="honda-fit-2",
        year=2020,
        mileage_km=50_000,
        current_price_usd=Decimal("9000"),
        is_active=True,
        source_created_at=_now(),
        payload_hash="y" * 8,
    )
    session.add(honda)
    session.flush()
    session.add(
        ListingStatusHistory(
            listing_id=honda.id, event_type="new", new_status="active", observed_at=_now()
        )
    )
    session.commit()

    pending = match_new_listings(session, since, factor=0.95, min_samples=5)
    assert {note.listing_id for note in pending} == {toyota.id, honda.id}


def test_match_caps_notifications_per_user_per_cycle() -> None:
    session = _session()
    since = _now() - timedelta(minutes=60)
    _add_user_with_filter(session, telegram_id=42)  # все Toyota

    for i in range(5):
        _add_listing(session, external_id=str(i))

    # Потолок 2: за цикл фиксируем и шлём не больше двух, остальное — не в
    # sent_notifications (всплывёт в следующем цикле).
    pending = match_new_listings(session, since, factor=0.95, min_samples=5, max_per_user_full=2)
    assert len(pending) == 2
    assert session.scalar(select(func.count()).select_from(SentNotification)) == 2

    # Следующий цикл (в пределах lookback) добирает ещё двух — дублей нет.
    pending2 = match_new_listings(session, since, factor=0.95, min_samples=5, max_per_user_full=2)
    assert len(pending2) == 2
    sent_ids = {note.listing_id for note in pending} | {note.listing_id for note in pending2}
    assert len(sent_ids) == 4

    # max_per_user_full=0 -> без ограничения: последнее пятое объявление уходит.
    pending3 = match_new_listings(session, since, factor=0.95, min_samples=5, max_per_user_full=0)
    assert len(pending3) == 1


def test_cap_is_shared_between_waves_of_one_cycle() -> None:
    """Волны цикла (mashina, затем lalafo) делят ОДИН потолок (решение 31.10.2026).

    Вторая волна получает только остаток; выбравший потолок юзер ждёт
    следующего цикла, остальные — нет.
    """
    session = _session()
    since = _now() - timedelta(minutes=60)
    _add_user_with_filter(session, telegram_id=42)
    _add_user_with_filter(session, telegram_id=43)
    for i in range(2):
        _add_listing(session, external_id=f"wave1-{i}")

    counts: dict[int, int] = {}
    wave1 = match_new_listings(
        session, since, factor=0.95, min_samples=5, max_per_user_full=3, cycle_counts=counts
    )
    assert len(wave1) == 4  # по 2 каждому
    assert sorted(counts.values()) == [2, 2]

    for i in range(3):
        _add_listing(session, external_id=f"wave2-{i}")
    wave2 = match_new_listings(
        session, since, factor=0.95, min_samples=5, max_per_user_full=3, cycle_counts=counts
    )
    assert len(wave2) == 2  # остаток потолка — по 1 каждому, а не по 3
    assert sorted(counts.values()) == [3, 3]

    # Третья волна того же цикла — потолок выбран, ничего не уходит и не фиксируется.
    wave3 = match_new_listings(
        session, since, factor=0.95, min_samples=5, max_per_user_full=3, cycle_counts=counts
    )
    assert wave3 == []
    assert session.scalar(select(func.count()).select_from(SentNotification)) == 6

    # Новый цикл — новый счётчик: недоставленное всплывает.
    next_cycle = match_new_listings(
        session, since, factor=0.95, min_samples=5, max_per_user_full=3, cycle_counts={}
    )
    assert len(next_cycle) == 4


def test_delayed_tier_has_its_own_cap() -> None:
    """Потолок на цикл — по тарифу: медленному свой, независимо от выдержки."""
    session = _session()
    since = _now() - timedelta(minutes=180)
    _add_paid_user_with_filter(session, telegram_id=1, tier="full")
    _add_paid_user_with_filter(session, telegram_id=2, tier="delayed")
    for i in range(4):
        _add_listing(session, external_id=str(i), observed_at=_now() - timedelta(minutes=10))

    # Выдержка выключена (0), чтобы проверить только потолок.
    pending = match_new_listings(
        session,
        since,
        factor=0.95,
        min_samples=5,
        max_per_user_full=3,
        max_per_user_delayed=1,
        delayed_lag_minutes=0,
    )
    by_chat: dict[int, int] = {}
    for note in pending:
        by_chat[note.chat_id] = by_chat.get(note.chat_id, 0) + 1
    assert by_chat == {1: 3, 2: 1}


# --- приоритеты фильтров при нехватке слотов ---------------------------------
# Плоский потолок + жадный проход давали на проде юзеру с фильтрами «все авто»
# и «ниже рынка» ноль отправок по второму: «все авто» матчит каждое объявление и
# забивает слоты раньше, чем очередь дойдёт до дешёвой машины.


def _add_user(session: Session, telegram_id: int) -> BotUser:
    user = BotUser(telegram_id=telegram_id)
    session.add(user)
    session.flush()
    return user


def _add_filter(session: Session, user: BotUser, **kwargs) -> UserFilter:
    user_filter = UserFilter(user_id=user.id, **kwargs)
    session.add(user_filter)
    session.commit()
    return user_filter


def test_below_market_filter_outranks_all_cars_within_the_cap() -> None:
    session = _session()
    since = _now() - timedelta(minutes=60)
    _add_stat(session, avg="10000", samples=10)
    user = _add_user(session, 42)
    # «Все авто» заведён РАНЬШЕ (меньший id): при старой сортировке по id он
    # забирал бы дешёвые машины себе и ставил их в самый слабый уровень.
    all_cars = _add_filter(session, user, brand_id=None)
    cheap = _add_filter(
        session, user, brand_id=None, below_market_only=True, below_market_percent=10
    )

    # Обычные машины приходят первыми, дешёвые — последними.
    regular = [_add_listing(session, external_id=f"r{i}") for i in range(3)]
    bargains = [
        _add_listing(session, external_id=f"b{i}", price_usd=Decimal("8000")) for i in range(2)
    ]

    pending = match_new_listings(session, since, factor=0.95, min_samples=5, max_per_user_full=3)
    sent = {note.listing_id: note for note in pending}
    # Обе дешёвые прошли, несмотря на порядок появления, и через фильтр «ниже рынка».
    assert all(listing.id in sent for listing in bargains)
    assert {sent[listing.id].filter_id for listing in bargains} == {cheap.id}
    # Оставшийся слот достался «все авто».
    leftovers = [listing for listing in regular if listing.id in sent]
    assert len(leftovers) == 1
    assert sent[leftovers[0].id].filter_id == all_cars.id
    # Отброшенные не зафиксированы — придут следующим циклом.
    assert session.scalar(select(func.count()).select_from(SentNotification)) == 3


def test_all_cars_candidates_are_ranked_by_below_market_percent() -> None:
    session = _session()
    since = _now() - timedelta(minutes=60)
    _add_stat(session, avg="10000", samples=10)
    user = _add_user(session, 42)
    _add_filter(session, user, brand_id=None)

    _add_listing(session, external_id="p2", price_usd=Decimal("9800"))  # -2%
    best = _add_listing(session, external_id="p10", price_usd=Decimal("9000"))  # -10%
    _add_listing(session, external_id="p0", price_usd=Decimal("10000"))  # не ниже рынка
    second = _add_listing(session, external_id="p5", price_usd=Decimal("9500"))  # -5%

    pending = match_new_listings(session, since, factor=0.95, min_samples=5, max_per_user_full=2)
    # В лимит попали самые выгодные, а не первые по порядку появления.
    assert [note.listing_id for note in pending] == [best.id, second.id]


def test_specific_brand_filter_ranks_between_below_market_and_all_cars() -> None:
    session = _session()
    since = _now() - timedelta(minutes=60)
    source_id, brand_id, _ = _ids(session)
    _add_stat(session, avg="10000", samples=10)
    user = _add_user(session, 42)
    all_cars = _add_filter(session, user, brand_id=None)
    toyota_any = _add_filter(session, user, brand_id=brand_id)
    toyota_cheap = _add_filter(session, user, brand_id=brand_id, below_market_only=True)

    toyota_regular = _add_listing(session, external_id="t", price_usd=Decimal("10000"))
    toyota_bargain = _add_listing(session, external_id="tb", price_usd=Decimal("8000"))
    # Honda — после объявлений Toyota: `_ids` берёт первую марку по индексу
    # имени, и с Honda в базе «тойоты» из `_add_listing` стали бы хондами.
    honda = Brand(name="Honda")
    session.add(honda)
    session.flush()
    honda_listing = Listing(
        source_id=source_id,
        external_id="h",
        brand_id=honda.id,
        title="Honda Fit",
        source_slug="honda-fit",
        year=2020,
        mileage_km=50_000,
        current_price_usd=Decimal("9000"),
        is_active=True,
        source_created_at=_now(),
        payload_hash="y" * 8,
    )
    session.add(honda_listing)
    session.flush()
    session.add(
        ListingStatusHistory(
            listing_id=honda_listing.id, event_type="new", new_status="active", observed_at=_now()
        )
    )
    session.commit()

    pending = match_new_listings(session, since, factor=0.95, min_samples=5, max_per_user_full=2)
    # Порядок: «ниже рынка» → конкретная марка → «все авто» (Honda не влезла).
    assert [(note.listing_id, note.filter_id) for note in pending] == [
        (toyota_bargain.id, toyota_cheap.id),
        (toyota_regular.id, toyota_any.id),
    ]
    assert honda_listing.id not in {note.listing_id for note in pending}
    assert all_cars.id not in {note.filter_id for note in pending}

    # Без потолка Honda доезжает через «все авто» — приоритет не отсекает, а упорядочивает.
    pending2 = match_new_listings(session, since, factor=0.95, min_samples=5)
    assert [(note.listing_id, note.filter_id) for note in pending2] == [
        (honda_listing.id, all_cars.id)
    ]


def test_match_skips_blocked_and_revoked_users() -> None:
    session = _session()
    since = _now() - timedelta(minutes=60)
    _, brand_id, _ = _ids(session)

    blocked = BotUser(telegram_id=1, is_active=False)
    revoked = BotUser(telegram_id=2, access_status="revoked")
    session.add_all([blocked, revoked])
    session.flush()
    session.add_all(
        [
            UserFilter(user_id=blocked.id, brand_id=brand_id),
            UserFilter(user_id=revoked.id, brand_id=brand_id),
        ]
    )
    session.commit()

    _add_listing(session, external_id="1")
    assert match_new_listings(session, since, factor=0.95, min_samples=5) == []


def test_paused_user_gets_nothing_and_backlog_does_not_pile_up() -> None:
    """/pause: пушей нет и в sent_notifications ничего не копится; после /play —
    то, что ещё в окне (решение 31.10.2026: пропущенное не накапливаем)."""
    session = _session()
    since = _now() - timedelta(minutes=60)
    _, brand_id, _ = _ids(session)
    paused = BotUser(telegram_id=1, paused_at=_now() - timedelta(hours=1))
    session.add(paused)
    session.flush()
    session.add(UserFilter(user_id=paused.id, brand_id=brand_id))
    session.commit()

    _add_listing(session, external_id="1")
    assert match_new_listings(session, since, factor=0.95, min_samples=5) == []
    assert session.scalar(select(func.count()).select_from(SentNotification)) == 0

    paused.paused_at = None  # /play
    session.commit()
    assert len(match_new_listings(session, since, factor=0.95, min_samples=5)) == 1


def test_trial_user_receives_until_trial_expires() -> None:
    """Пейвол на 4-й день: истёк trial_expires_at — юзер просто выпадает из выборки."""
    session = _session()
    since = _now() - timedelta(minutes=60)
    _, brand_id, _ = _ids(session)

    active = BotUser(
        telegram_id=1,
        access_status="trial",
        trial_started_at=_now() - timedelta(days=1),
        trial_expires_at=_now() + timedelta(days=2),
    )
    expired = BotUser(
        telegram_id=2,
        access_status="trial",
        trial_started_at=_now() - timedelta(days=4),
        trial_expires_at=_now() - timedelta(hours=1),
    )
    session.add_all([active, expired])
    session.flush()
    session.add_all(
        [
            UserFilter(user_id=active.id, brand_id=brand_id),
            UserFilter(user_id=expired.id, brand_id=brand_id),
        ]
    )
    session.commit()

    _add_listing(session, external_id="1")
    pending = match_new_listings(session, since, factor=0.95, min_samples=5)
    assert [note.chat_id for note in pending] == [1]


# --- точка отсчёта фильтра и превью -----------------------------------------


def test_filter_does_not_backfill_listings_older_than_itself() -> None:
    """Фильтр создан после появления объявления — это не новинка, а backfill.

    Раньше новичок забирал всё окно lookback разом: на проде это ~500
    объявлений, из которых он получал потолок за цикл и «капель» ещё часами.
    """
    session = _session()
    since = _now() - timedelta(minutes=60)
    _add_listing(session, external_id="old", observed_at=_now() - timedelta(minutes=30))
    _add_user_with_filter(session, telegram_id=42)  # фильтр появился ПОСЛЕ объявления

    assert match_new_listings(session, since, factor=0.95, min_samples=5) == []


def test_listing_after_filter_creation_still_arrives() -> None:
    """Обратная сторона: то, что появилось после фильтра, приходить обязано."""
    session = _session()
    since = _now() - timedelta(minutes=60)
    _add_user_with_filter(session, telegram_id=42)
    _add_listing(session, external_id="fresh")

    pending = match_new_listings(session, since, factor=0.95, min_samples=5)
    assert len(pending) == 1


def test_preview_returns_recent_matches_and_marks_them_sent() -> None:
    """Превью показывает, что уже есть на рынке, и гасит повтор рассылкой."""
    session = _session()
    user_filter = _add_user_with_filter(session, telegram_id=42)
    for index in range(3):
        _add_listing(session, external_id=f"p{index}")

    preview = preview_for_filter(session, user_filter.id, limit=10, min_samples=5)
    assert len(preview) == 3
    assert {note.chat_id for note in preview} == {42}

    # Те же машины обычной рассылкой уже не придут.
    since = _now() - timedelta(minutes=60)
    assert match_new_listings(session, since, factor=0.95, min_samples=5) == []


def test_preview_respects_the_limit() -> None:
    session = _session()
    user_filter = _add_user_with_filter(session, telegram_id=42)
    for index in range(14):
        _add_listing(session, external_id=f"many{index}")

    preview = preview_for_filter(session, user_filter.id, limit=10, min_samples=5)
    assert len(preview) == 10


# --- тарифы: отставание дешёвых планов ---------------------------------------
# Отставание — единственное отличие тарифов, то есть буквально товар. Если оно
# не работает, полный тариф не за что продавать; если работает слишком строго,
# отстающий не получает ничего и уходит.


def test_delayed_tier_waits_for_the_lag() -> None:
    session = _session()
    since = _now() - timedelta(minutes=180)
    _add_paid_user_with_filter(session, telegram_id=42, tier="delayed")
    _add_listing(session, external_id="1", observed_at=_now() - timedelta(minutes=10))

    # 10 минут — рано: объявление дозреет и придёт следующими циклами.
    assert (
        match_new_listings(session, since, factor=0.95, min_samples=5, delayed_lag_minutes=60) == []
    )
    # То же объявление с событием 70 минут назад — уже можно.
    session.query(ListingStatusHistory).update(
        {ListingStatusHistory.observed_at: _now() - timedelta(minutes=70)}
    )
    session.commit()
    pending = match_new_listings(session, since, factor=0.95, min_samples=5, delayed_lag_minutes=60)
    assert [note.chat_id for note in pending] == [42]


def test_full_tier_member_and_trial_get_it_immediately() -> None:
    """Полный тариф, участник группы и триал ждать не должны — это и есть разница."""
    session = _session()
    since = _now() - timedelta(minutes=180)
    _, brand_id, _ = _ids(session)
    now = _now()

    _add_paid_user_with_filter(session, telegram_id=1, tier="full")
    member = BotUser(telegram_id=2, access_status="member")
    trial = BotUser(
        telegram_id=3,
        access_status="trial",
        trial_started_at=now - timedelta(days=1),
        trial_expires_at=now + timedelta(days=2),
    )
    session.add_all([member, trial])
    session.flush()
    session.add_all(
        [
            UserFilter(user_id=member.id, brand_id=brand_id, created_at=now - timedelta(hours=3)),
            UserFilter(user_id=trial.id, brand_id=brand_id, created_at=now - timedelta(hours=3)),
        ]
    )
    session.commit()

    _add_listing(session, external_id="1", observed_at=now - timedelta(minutes=10))
    pending = match_new_listings(session, since, factor=0.95, min_samples=5, delayed_lag_minutes=60)
    assert sorted(note.chat_id for note in pending) == [1, 2, 3]


def test_paid_user_without_a_block_falls_back_to_delayed() -> None:
    """Fail-safe: нет блока — медленный, но рабочий доступ, а не бесплатный полный."""
    session = _session()
    since = _now() - timedelta(minutes=180)
    now = _now()
    _, brand_id, _ = _ids(session)
    user = BotUser(
        telegram_id=42,
        access_status="subscriber",
        subscription_expires_at=now + timedelta(days=10),
    )
    session.add(user)
    session.flush()
    session.add(UserFilter(user_id=user.id, brand_id=brand_id, created_at=now - timedelta(hours=3)))
    session.commit()
    _add_listing(session, external_id="1", observed_at=now - timedelta(minutes=10))

    assert (
        match_new_listings(session, since, factor=0.95, min_samples=5, delayed_lag_minutes=60) == []
    )


def test_zero_lag_switches_the_delay_off_entirely() -> None:
    """Аварийный выключатель DELAYED_TIER_LAG_MINUTES=0: все платные едут полными."""
    session = _session()
    since = _now() - timedelta(minutes=180)
    _add_paid_user_with_filter(session, telegram_id=42, tier="delayed")
    _add_listing(session, external_id="1", observed_at=_now() - timedelta(minutes=1))

    pending = match_new_listings(session, since, factor=0.95, min_samples=5, delayed_lag_minutes=0)
    assert [note.chat_id for note in pending] == [42]


def test_delayed_tier_is_flushed_before_the_night_pause() -> None:
    """Не дозревающее до последнего цикла отдаём сразу.

    Иначе уведомление уедет утром — а за ночь объявление успевают снять, и
    ссылка на удалённую страницу читается как сломанный бот, а не как дешёвый
    тариф (решение владельца 18.08.2026).
    """
    session = _session()
    since = _now() - timedelta(minutes=180)
    _add_paid_user_with_filter(session, telegram_id=42, tier="delayed")
    _add_listing(session, external_id="1", observed_at=_now() - timedelta(minutes=10))

    # Успевает дозреть сегодня (дедлайн через 2 часа) — держим.
    assert (
        match_new_listings(
            session,
            since,
            factor=0.95,
            min_samples=5,
            delayed_lag_minutes=60,
            flush_deadline=_now() + timedelta(hours=2),
        )
        == []
    )
    # Последний цикл дня уже через 30 минут, дозреет только к 00:10 — отдаём.
    pending = match_new_listings(
        session,
        since,
        factor=0.95,
        min_samples=5,
        delayed_lag_minutes=60,
        flush_deadline=_now() + timedelta(minutes=30),
    )
    assert [note.chat_id for note in pending] == [42]


def test_preview_for_delayed_tier_has_an_upper_freshness_bound() -> None:
    """Иначе обход тарифа — «удалить фильтр и создать заново»: превью отдаёт свежак."""
    session = _session()
    user_filter = _add_paid_user_with_filter(session, telegram_id=42, tier="delayed")
    fresh = _add_listing(
        session, external_id="fresh", source_created_at=_now() - timedelta(minutes=10)
    )
    old = _add_listing(session, external_id="old", source_created_at=_now() - timedelta(hours=3))

    preview = preview_for_filter(
        session, user_filter.id, limit=10, min_samples=5, delayed_lag_minutes=60
    )
    assert [note.listing_id for note in preview] == [old.id]
    assert fresh.id not in {note.listing_id for note in preview}


def test_preview_for_full_tier_shows_everything() -> None:
    session = _session()
    user_filter = _add_paid_user_with_filter(session, telegram_id=42, tier="full")
    _add_listing(session, external_id="fresh", source_created_at=_now() - timedelta(minutes=10))
    _add_listing(session, external_id="old", source_created_at=_now() - timedelta(hours=3))

    preview = preview_for_filter(
        session, user_filter.id, limit=10, min_samples=5, delayed_lag_minutes=60
    )
    assert len(preview) == 2
