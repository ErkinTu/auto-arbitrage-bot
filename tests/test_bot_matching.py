from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.bot.matching import (
    filter_matches,
    find_new_listings,
    market_average,
    match_new_listings,
)
from app.db.base import Base
from app.models import (
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
        city="Бишкек",
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


def _add_user_with_filter(session: Session, *, telegram_id: int = 111, **filter_kwargs) -> UserFilter:
    _, brand_id, _ = _ids(session)
    user = BotUser(telegram_id=telegram_id)
    session.add(user)
    session.flush()
    user_filter = UserFilter(user_id=user.id, brand_id=brand_id, **filter_kwargs)
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
    _add_listing(session, external_id="2", event_type="price_changed")
    _add_listing(session, external_id="3", observed_at=_now() - timedelta(hours=3))
    _add_listing(session, external_id="4", is_active=False)
    _add_listing(session, external_id="5", price_usd=None)

    found = find_new_listings(session, since)
    assert [listing.id for listing in found] == [fresh.id]


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
    null_mileage = _add_listing(session, external_id="1", mileage_km=None, price_usd=Decimal("7000"))
    assert market_average(session, null_mileage, min_samples=5) == 9000.0

    # Известный пробег: только свой бакет (0-50k, 3 образца) -> порог 5 не выполнен.
    known = _add_listing(session, external_id="2", mileage_km=30_000, price_usd=Decimal("7000"))
    assert market_average(session, known, min_samples=5) is None


def test_market_average_respects_customs_status() -> None:
    session = _session()
    _add_stat(session, avg="10000", samples=8, customs_status="cleared")
    _add_stat(session, avg="16000", samples=8, customs_status="not_cleared")

    listing = _add_listing(session, external_id="1", price_usd=Decimal("15000"), customs_status="not_cleared")
    assert market_average(session, listing, min_samples=5) == 16000.0


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
    _add_user_with_filter(
        session, telegram_id=42, below_market_only=True, below_market_percent=15
    )
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
