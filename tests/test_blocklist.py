"""Чёрный список: глобальный запрет поверх всех фильтров пользователя.

Главное, что проверяем: запрет сильнее фильтра (подходящее по фильтру
объявление не уходит), не задевает соседей и не срабатывает по неизвестным
полям объявления.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.bot.db as botdb
from app.bot.matching import match_new_listings
from app.db.base import Base
from app.models import (
    BotUser,
    Brand,
    CarModel,
    Listing,
    ListingStatusHistory,
    Source,
    UserBlock,
    UserFilter,
)
from app.services.blocklist import (
    KIND_BRAND,
    KIND_CITY,
    KIND_MODEL,
    BlockSet,
    block_value,
    load_block_sets,
    normalize_city,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session() -> Session:
    import app.models  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Source(name="mashina.kg"))
    session.add_all([Brand(name="Toyota"), Brand(name="Kia")])
    session.flush()
    toyota = session.scalar(select(Brand).where(Brand.name == "Toyota"))
    kia = session.scalar(select(Brand).where(Brand.name == "Kia"))
    session.add_all(
        [
            CarModel(brand_id=toyota.id, name="Camry"),
            CarModel(brand_id=toyota.id, name="Prius"),
            CarModel(brand_id=kia.id, name="K5"),
        ]
    )
    session.commit()
    return session


def _model(session: Session, brand: str, name: str) -> CarModel:
    brand_row = session.scalar(select(Brand).where(Brand.name == brand))
    return session.scalar(
        select(CarModel).where(CarModel.brand_id == brand_row.id, CarModel.name == name)
    )


def _add_listing(session: Session, *, external_id: str, brand: str, model: str,
                 city: str = "Бишкек") -> Listing:
    source_id = session.scalar(select(Source.id))
    model_row = _model(session, brand, model)
    listing = Listing(
        source_id=source_id,
        external_id=external_id,
        brand_id=model_row.brand_id,
        car_model_id=model_row.id,
        title=f"{brand} {model} 2019",
        source_slug=f"{brand}-{model}-{external_id}".lower(),
        year=2019,
        mileage_km=90_000,
        city=city,
        current_price_usd=Decimal("10000"),
        is_active=True,
        source_created_at=_now(),
        payload_hash="x" * 8,
    )
    session.add(listing)
    session.flush()
    session.add(
        ListingStatusHistory(
            listing_id=listing.id, event_type="new", new_status="active", observed_at=_now()
        )
    )
    session.commit()
    return listing


def _add_user(session: Session, telegram_id: int = 111) -> BotUser:
    """Юзер с фильтром «всё подряд»: отсекать должен только чёрный список."""
    user = BotUser(telegram_id=telegram_id, access_status="member")
    session.add(user)
    session.flush()
    session.add(UserFilter(user_id=user.id, brand_id=None, below_market_only=False))
    session.commit()
    return user


def _block(session: Session, user: BotUser, kind: str, value: str, label: str = "x") -> None:
    session.add(UserBlock(user_id=user.id, kind=kind, value=value, label=label))
    session.commit()


def _match(session: Session) -> set[str]:
    pending = match_new_listings(
        session, _now() - timedelta(minutes=60), factor=0.95, min_samples=5
    )
    return {note.title for note in pending}


# --------------------------------------------------------------------------
# Чистая логика
# --------------------------------------------------------------------------


def test_block_value_normalizes_city_and_rejects_empty() -> None:
    assert block_value(KIND_CITY, city="  Бишкек ") == "бишкек"
    assert normalize_city(None) == ""
    for bad in ({}, {"city": "   "}):
        try:
            block_value(KIND_CITY, **bad)
        except ValueError:
            continue
        raise AssertionError("пустой город должен отвергаться")


def test_blockset_ignores_unknown_listing_fields() -> None:
    """NULL-поля объявления под запрет не попадают: запрет надо доказать."""
    listing = Listing(source_id=1, external_id="1", payload_hash="x", brand_id=None,
                      car_model_id=None, city=None)
    assert BlockSet(brands={1}, models={2}, cities={"бишкек"}).blocks(listing) is False


# --------------------------------------------------------------------------
# Применение в матчинге
# --------------------------------------------------------------------------


def test_model_block_hides_only_that_model() -> None:
    session = _session()
    user = _add_user(session)
    camry = _model(session, "Toyota", "Camry")
    _block(session, user, KIND_MODEL, str(camry.id), "Toyota Camry")

    _add_listing(session, external_id="1", brand="Toyota", model="Camry")
    _add_listing(session, external_id="2", brand="Toyota", model="Prius")

    # Фильтр у юзера «всё подряд» — отсекает только чёрный список.
    assert _match(session) == {"Toyota Prius 2019"}


def test_brand_block_hides_whole_brand_but_not_others() -> None:
    session = _session()
    user = _add_user(session)
    toyota = session.scalar(select(Brand).where(Brand.name == "Toyota"))
    _block(session, user, KIND_BRAND, str(toyota.id), "Toyota")

    _add_listing(session, external_id="1", brand="Toyota", model="Camry")
    _add_listing(session, external_id="2", brand="Toyota", model="Prius")
    _add_listing(session, external_id="3", brand="Kia", model="K5")

    assert _match(session) == {"Kia K5 2019"}


def test_city_block_is_case_insensitive() -> None:
    session = _session()
    user = _add_user(session)
    _block(session, user, KIND_CITY, "бишкек", "Бишкек")

    _add_listing(session, external_id="1", brand="Toyota", model="Camry", city="БИШКЕК")
    _add_listing(session, external_id="2", brand="Kia", model="K5", city="Ош")

    assert _match(session) == {"Kia K5 2019"}


def test_block_of_one_user_does_not_affect_another() -> None:
    session = _session()
    first = _add_user(session, telegram_id=111)
    _add_user(session, telegram_id=222)
    toyota = session.scalar(select(Brand).where(Brand.name == "Toyota"))
    _block(session, first, KIND_BRAND, str(toyota.id), "Toyota")

    _add_listing(session, external_id="1", brand="Toyota", model="Camry")
    pending = match_new_listings(
        session, _now() - timedelta(minutes=60), factor=0.95, min_samples=5
    )
    assert [note.chat_id for note in pending] == [222]


def test_load_block_sets_groups_by_user() -> None:
    session = _session()
    first = _add_user(session, telegram_id=111)
    second = _add_user(session, telegram_id=222)
    toyota = session.scalar(select(Brand).where(Brand.name == "Toyota"))
    _block(session, first, KIND_BRAND, str(toyota.id), "Toyota")
    _block(session, first, KIND_CITY, "ош", "Ош")
    _block(session, second, KIND_CITY, "бишкек", "Бишкек")

    loaded = load_block_sets(session, {first.id, second.id})
    assert loaded[first.id].brands == {toyota.id}
    assert loaded[first.id].cities == {"ош"}
    assert loaded[second.id].brands == set()
    assert loaded[second.id].cities == {"бишкек"}
    assert load_block_sets(session, set()) == {}


# --------------------------------------------------------------------------
# Склейка в bot/db.py (кнопка под карточкой)
# --------------------------------------------------------------------------


class _SharedSession:
    """SessionLocal-заглушка: отдаёт одну и ту же сессию и не закрывает её."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def __call__(self) -> "_SharedSession":
        return self

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *exc) -> bool:
        return False


def test_add_block_from_listing_is_idempotent_and_capped(monkeypatch) -> None:
    session = _session()
    user = _add_user(session)
    listing = _add_listing(session, external_id="1", brand="Toyota", model="Camry")
    monkeypatch.setattr(botdb, "SessionLocal", _SharedSession(session))

    first = botdb.add_block_from_listing(user.telegram_id, listing.id, KIND_MODEL)
    assert (first.ok, first.label) == (True, "Toyota Camry")

    again = botdb.add_block_from_listing(user.telegram_id, listing.id, KIND_MODEL)
    assert (again.ok, again.already) == (False, True)

    options = botdb.block_options(listing.id)
    assert (options.brand_name, options.model_name, options.city) == (
        "Toyota",
        "Toyota Camry",
        "Бишкек",
    )

    monkeypatch.setattr(botdb, "MAX_BLOCKS_PER_USER", 1)
    other = _add_listing(session, external_id="2", brand="Kia", model="K5")
    capped = botdb.add_block_from_listing(user.telegram_id, other.id, KIND_MODEL)
    assert capped.limit_reached is True

    assert [item.label for item in botdb.list_blocks(user.telegram_id)] == ["Toyota Camry"]
    block_id = botdb.list_blocks(user.telegram_id)[0].id
    assert botdb.delete_block(user.telegram_id, block_id) is True
    assert botdb.list_blocks(user.telegram_id) == []


def test_block_from_listing_reports_missing_field(monkeypatch) -> None:
    """У объявления без города «скрыть город» невозможно — говорим честно."""
    session = _session()
    user = _add_user(session)
    listing = _add_listing(session, external_id="1", brand="Toyota", model="Camry", city="")
    monkeypatch.setattr(botdb, "SessionLocal", _SharedSession(session))

    result = botdb.add_block_from_listing(user.telegram_id, listing.id, KIND_CITY)
    assert (result.ok, result.unavailable) == (False, True)
    assert botdb.block_options(listing.id).city is None
    # Исчезнувшее объявление — тот же честный отказ, а не исключение.
    assert botdb.add_block_from_listing(user.telegram_id, 9999, KIND_MODEL).unavailable is True
    assert botdb.block_options(9999) is None
