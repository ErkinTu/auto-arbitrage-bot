from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import CarModel, Listing, NormalizationIssue, Source
from app.services.catalog_service import (
    reprocess_catalog,
    resolve_normalized_listing,
    seed_default_catalog,
)
from app.services.normalizer import normalize_mashina_listing


def _session() -> Session:
    # Import every ORM class before metadata creation.
    import app.models  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    seed_default_catalog(session)
    session.commit()
    return session


def test_normalize_audi_q5() -> None:
    raw = {
        "id": 10063047,
        "title": "Продажа Audi Q5 II (FY) 2019",
        "slug": "audi-q5-example",
        "prices": [
            {"currency": "USD", "amount": 21000, "is_original": False},
            {"currency": "KGS", "amount": 1836450, "is_original": True},
        ],
        "status": "active",
        "created_at": "2026-06-16T18:18:29.933087+06:00",
        "attributes": [
            {"slug": "year", "value_number": 2019},
            {"slug": "mileage", "value_number": 119000},
            {"slug": "engine_volume", "value_number": 2.0},
            {"slug": "gearbox", "value_text": "робот"},
        ],
        "user": {"name": "Private", "phones": ["secret"], "is_verified_seller": False},
    }

    result = normalize_mashina_listing(raw)

    assert result.external_id == "10063047"
    assert result.make == "Audi"
    assert result.model == "Q5"
    assert result.generation == "II (FY)"
    assert result.source_model_text == "Q5"
    assert result.source_generation_text == "II (FY)"
    assert result.year == 2019
    assert result.mileage_km == 119000
    assert result.price_usd == 21000
    assert "user" not in result.safe_payload
    assert "phones" not in str(result.safe_payload)


def test_normalizer_uses_filtered_brand_when_title_is_nonstandard() -> None:
    item = normalize_mashina_listing(
        {
            "id": 42,
            "title": "Продаю X5 G05 2020",
            "prices": [{"is_original": True, "currency": "USD", "amount": 50000}],
            "status": "active",
            "attributes": [{"slug": "year", "value_number": 2020}],
        },
        fallback_make="BMW",
    )

    assert item.make == "BMW"
    assert item.year == 2020
    assert item.model == "X5"
    assert item.source_model_text == "X5"
    assert item.source_generation_text == "G05"


def test_bmw_plural_series_alias_resolves_to_canonical_model() -> None:
    session = _session()
    item = normalize_mashina_listing(
        {
            "id": 43,
            "title": "BMW 5 серии VII (G30/G31) Рестайлинг 2022",
            "prices": [{"is_original": True, "currency": "USD", "amount": 30000}],
            "attributes": [{"slug": "year", "value_number": 2022}],
        },
        fallback_make="BMW",
    )

    resolution = resolve_normalized_listing(session, item)

    assert item.source_model_text == "5 серии"
    assert item.model == "5 Series"
    assert item.generation == "VII (G30/G31) Рестайлинг"
    assert resolution.status == "resolved"
    assert item.analytics_eligible is True


def test_toyota_mark_2_with_numeric_generation_is_canonicalized() -> None:
    session = _session()
    item = normalize_mashina_listing(
        {
            "id": 44,
            "title": "Toyota Mark 2 4 поколения 1996",
            "prices": [{"is_original": True, "currency": "USD", "amount": 5000}],
            "attributes": [{"slug": "year", "value_number": 1996}],
        },
        fallback_make="Toyota",
    )

    resolution = resolve_normalized_listing(session, item)

    assert item.source_model_text == "Mark 2"
    assert item.source_generation_text == "4 поколения"
    assert item.model == "Mark II"
    assert item.generation == "IV"
    assert resolution.status == "resolved"


def test_model_without_generation_is_valid_for_analytics() -> None:
    session = _session()
    item = normalize_mashina_listing(
        {
            "id": 45,
            "title": "Toyota Aygo X 2024",
            "prices": [{"is_original": True, "currency": "USD", "amount": 16000}],
            "attributes": [{"slug": "year", "value_number": 2024}],
        },
        fallback_make="Toyota",
    )

    resolution = resolve_normalized_listing(session, item)

    assert item.model == "Aygo X"
    assert item.generation is None
    assert resolution.status == "resolved_model_only"
    assert item.analytics_eligible is True


def test_unknown_model_is_saved_as_issue_without_polluting_catalog() -> None:
    session = _session()
    item = normalize_mashina_listing(
        {
            "id": 46,
            "title": "Toyota Fictional Wagon II 2020",
            "prices": [{"is_original": True, "currency": "USD", "amount": 12000}],
            "attributes": [{"slug": "year", "value_number": 2020}],
        },
        fallback_make="Toyota",
    )

    resolution = resolve_normalized_listing(session, item)
    session.flush()

    assert item.source_model_text == "Fictional Wagon"
    assert item.model is None
    assert resolution.status == "unknown_model"
    assert item.analytics_eligible is False
    assert session.scalar(select(CarModel).where(CarModel.name == "Fictional Wagon")) is None
    issue = session.scalar(select(NormalizationIssue).where(NormalizationIssue.source_model_text == "Fictional Wagon"))
    assert issue is not None
    assert issue.status == "open"


def test_implausible_mileage_is_kept_but_not_analytics_eligible() -> None:
    session = _session()
    item = normalize_mashina_listing(
        {
            "id": 47,
            "title": "Toyota Yaris I 2001",
            "prices": [{"is_original": True, "currency": "USD", "amount": 3500}],
            "attributes": [
                {"slug": "year", "value_number": 2001},
                {"slug": "mileage", "value_number": 4_320_000},
            ],
        },
        fallback_make="Toyota",
    )

    resolve_normalized_listing(session, item)

    assert "mileage_implausibly_high" in item.quality_flags
    assert item.model == "Yaris"
    assert item.analytics_eligible is False


def test_implausibly_high_price_is_flagged_and_not_analytics_eligible() -> None:
    session = _session()
    item = normalize_mashina_listing(
        {
            "id": 48,
            "title": "Toyota Camry 2015",
            "prices": [{"is_original": True, "currency": "USD", "amount": 999_999_999}],
            "attributes": [{"slug": "year", "value_number": 2015}],
        },
        fallback_make="Toyota",
    )

    resolve_normalized_listing(session, item)

    # Camry распознаётся, значит analytics_eligible=False — именно из-за цены-плейсхолдера.
    assert item.model == "Camry"
    assert "price_implausibly_high" in item.quality_flags
    assert item.analytics_eligible is False


def test_legit_expensive_price_is_not_flagged() -> None:
    item = normalize_mashina_listing(
        {
            "id": 49,
            "title": "Toyota Camry 2024",
            "prices": [{"is_original": True, "currency": "USD", "amount": 500_000}],
            "attributes": [{"slug": "year", "value_number": 2024}],
        },
        fallback_make="Toyota",
    )

    # Реальный верх рынка (~$500k) не должен ловиться предохранителем.
    assert item.price_usd == 500_000
    assert "price_implausibly_high" not in item.quality_flags


def test_mashina_low_price_is_flagged_and_not_analytics_eligible() -> None:
    session = _session()
    item = normalize_mashina_listing(
        {
            "id": 50,
            "title": "Toyota Camry 2015",
            # Взнос рассрочки / «обмен» с символической ценой — не рыночная цена авто.
            "prices": [{"is_original": True, "currency": "USD", "amount": 300}],
            "attributes": [{"slug": "year", "value_number": 2015}],
        },
        fallback_make="Toyota",
    )

    resolve_normalized_listing(session, item)

    assert item.model == "Camry"
    assert "price_implausibly_low" in item.quality_flags
    assert item.analytics_eligible is False


def _mk_listing(session: Session, source: Source, *, ext: str, price, flags: list[str]) -> Listing:
    listing = Listing(
        source_id=source.id,
        external_id=ext,
        title="Toyota Camry 2015",
        source_make_text="Toyota",
        source_model_text="Camry",
        year=2015,
        current_price_usd=price,
        quality_flags=flags,
        is_active=True,
        payload_hash="x" * 8,
    )
    session.add(listing)
    session.flush()
    return listing


def test_reprocess_backfills_price_flags_from_current_price() -> None:
    session = _session()
    mashina = Source(name="mashina.kg")
    lalafo = Source(name="lalafo.kg")
    session.add_all([mashina, lalafo])
    session.flush()

    # mashina >$2M и <$500 — reprocess перепарсит title без цен, флаг берём из current_price_usd.
    high = _mk_listing(session, mashina, ext="h", price=Decimal("3000000"), flags=[])
    low = _mk_listing(session, mashina, ext="l", price=Decimal("300"), flags=[])
    # lalafo: устаревший флаг при нормальной цене снимается; настоящий >$2M проставляется.
    stale = _mk_listing(session, lalafo, ext="s", price=Decimal("15000"), flags=["price_implausibly_high"])
    lalafo_high = _mk_listing(session, lalafo, ext="lh", price=Decimal("11000000"), flags=[])
    session.commit()

    reprocess_catalog(session)
    session.commit()

    for obj in (high, low, stale, lalafo_high):
        session.refresh(obj)
    assert "price_implausibly_high" in high.quality_flags and high.analytics_eligible is False
    assert "price_implausibly_low" in low.quality_flags and low.analytics_eligible is False
    assert "price_implausibly_high" not in (stale.quality_flags or [])
    assert stale.analytics_eligible is True
    assert "price_implausibly_high" in lalafo_high.quality_flags
    assert lalafo_high.analytics_eligible is False


def test_generation_period_is_preserved_and_not_treated_as_listing_year() -> None:
    item = normalize_mashina_listing(
        {
            "id": 48,
            "title": "Toyota Aygo X 2016 - 2026",
            "prices": [{"is_original": True, "currency": "USD", "amount": 16000}],
            "attributes": [{"slug": "year", "value_number": 2024}],
        },
        fallback_make="Toyota",
    )

    assert item.source_model_text == "Aygo X"
    assert item.source_generation_text == "2016 - 2026"
    assert item.generation == "2016-2026"
    assert item.year == 2024
    assert "year_title_conflict" not in item.quality_flags


def test_generation_current_period_and_final_listing_year_are_parsed_separately() -> None:
    item = normalize_mashina_listing(
        {
            "id": 49,
            "title": "Toyota Aygo X (2016-н.в.) 2020",
            "prices": [{"is_original": True, "currency": "USD", "amount": 12000}],
            "attributes": [{"slug": "mileage", "value_number": 40000}],
        },
        fallback_make="Toyota",
    )

    assert item.source_generation_text == "(2016-н.в.)"
    assert item.generation == "2016-н.в."
    assert item.year == 2020


def test_generation_parenthesized_roman_and_restyling_typo_are_canonicalized() -> None:
    session = _session()
    item = normalize_mashina_listing(
        {
            "id": 50,
            "title": "Toyota Camry Рейстайлинг (V) 2004",
            "prices": [{"is_original": True, "currency": "USD", "amount": 6500}],
            "attributes": [{"slug": "year", "value_number": 2004}],
        },
        fallback_make="Toyota",
    )

    resolution = resolve_normalized_listing(session, item)

    assert item.source_generation_text == "Рейстайлинг (V)"
    assert item.generation == "V Рестайлинг"
    assert resolution.status == "resolved"


def test_generation_restyling_prefix_without_other_descriptor_is_kept() -> None:
    item = normalize_mashina_listing(
        {
            "id": 51,
            "title": "Toyota Camry Рестайлинг 2011",
            "prices": [{"is_original": True, "currency": "USD", "amount": 10000}],
            "attributes": [{"slug": "year", "value_number": 2011}],
        },
        fallback_make="Toyota",
    )

    assert item.source_generation_text == "Рестайлинг"
    assert item.generation == "Рестайлинг"


def test_unknown_model_splits_parenthesized_generation() -> None:
    item = normalize_mashina_listing(
        {
            "id": 52,
            "title": "Toyota Fictional Wagon (V) Рестайлинг 2020",
            "prices": [{"is_original": True, "currency": "USD", "amount": 12000}],
            "attributes": [{"slug": "year", "value_number": 2020}],
        },
        fallback_make="Toyota",
    )

    assert item.source_model_text == "Fictional Wagon"
    assert item.source_generation_text == "(V) Рестайлинг"
    assert item.generation == "V Рестайлинг"
