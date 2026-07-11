from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Brand, CarModel, Generation, GenerationAlias, Listing, ModelAlias, NormalizationIssue
from app.services.catalog import DEFAULT_CATALOG, is_bootstrap_model, normalize_alias_text
from app.services.lookup import get_or_create_brand, get_or_create_generation, get_or_create_model
from app.services.normalizer import NormalizedListing, normalize_mashina_listing


CRITICAL_QUALITY_FLAGS = {"year_title_conflict", "mileage_implausibly_high"}


@dataclass(slots=True)
class CatalogResolution:
    brand: Brand | None
    car_model: CarModel | None
    generation: Generation | None
    status: str
    confidence: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _upsert_model_alias(
    session: Session,
    *,
    brand: Brand,
    car_model: CarModel,
    alias: str,
    source: str,
) -> ModelAlias:
    normalized = normalize_alias_text(alias)
    existing = session.scalar(
        select(ModelAlias).where(
            ModelAlias.brand_id == brand.id,
            ModelAlias.normalized_alias == normalized,
        )
    )
    if existing:
        return existing
    created = ModelAlias(
        brand_id=brand.id,
        car_model_id=car_model.id,
        alias=alias,
        normalized_alias=normalized,
        source=source,
    )
    session.add(created)
    session.flush()
    return created


def _upsert_generation_alias(
    session: Session,
    *,
    car_model: CarModel,
    generation: Generation,
    alias: str,
    source: str,
) -> GenerationAlias:
    normalized = normalize_alias_text(alias)
    existing = session.scalar(
        select(GenerationAlias).where(
            GenerationAlias.car_model_id == car_model.id,
            GenerationAlias.normalized_alias == normalized,
        )
    )
    if existing:
        return existing
    created = GenerationAlias(
        car_model_id=car_model.id,
        generation_id=generation.id,
        alias=alias,
        normalized_alias=normalized,
        source=source,
    )
    session.add(created)
    session.flush()
    return created


def seed_default_catalog(session: Session) -> None:
    """Seed canonical models and aliases. Safe to call on every startup."""
    for brand_name, model_seeds in DEFAULT_CATALOG.items():
        brand = get_or_create_brand(session, brand_name)
        assert brand is not None
        for seed in model_seeds:
            model = get_or_create_model(session, brand, seed.canonical_name)
            assert model is not None
            _upsert_model_alias(
                session,
                brand=brand,
                car_model=model,
                alias=seed.canonical_name,
                source="bootstrap",
            )
            for alias in seed.aliases:
                _upsert_model_alias(
                    session,
                    brand=brand,
                    car_model=model,
                    alias=alias,
                    source="bootstrap",
                )


def add_model_alias(
    session: Session,
    *,
    brand_name: str,
    canonical_model_name: str,
    alias: str,
    source: str = "manual",
) -> ModelAlias:
    brand = get_or_create_brand(session, brand_name)
    assert brand is not None
    model = get_or_create_model(session, brand, canonical_model_name)
    assert model is not None
    return _upsert_model_alias(session, brand=brand, car_model=model, alias=alias, source=source)


def add_generation_alias(
    session: Session,
    *,
    brand_name: str,
    canonical_model_name: str,
    canonical_generation_name: str,
    alias: str,
    source: str = "manual",
) -> GenerationAlias:
    brand = get_or_create_brand(session, brand_name)
    assert brand is not None
    model = get_or_create_model(session, brand, canonical_model_name)
    assert model is not None
    generation = get_or_create_generation(session, model, canonical_generation_name)
    assert generation is not None
    return _upsert_generation_alias(
        session,
        car_model=model,
        generation=generation,
        alias=alias,
        source=source,
    )


def _record_unknown_model(session: Session, brand: Brand, source_model_text: str) -> None:
    normalized = normalize_alias_text(source_model_text)
    issue = session.scalar(
        select(NormalizationIssue).where(
            NormalizationIssue.brand_id == brand.id,
            NormalizationIssue.normalized_source_model == normalized,
        )
    )
    if issue is None:
        # autoflush is disabled, so the SELECT above cannot see issues added
        # earlier in this same batch. Reuse a pending one to avoid inserting a
        # duplicate (brand_id, normalized_source_model) in a single flush.
        for pending in session.new:
            if (
                isinstance(pending, NormalizationIssue)
                and pending.brand_id == brand.id
                and pending.normalized_source_model == normalized
            ):
                pending.occurrence_count += 1
                pending.last_seen_at = utcnow()
                return
    if issue is None:
        issue = NormalizationIssue(
            brand_id=brand.id,
            source_model_text=source_model_text,
            normalized_source_model=normalized,
            status="open",
            occurrence_count=1,
            first_seen_at=utcnow(),
            last_seen_at=utcnow(),
        )
        session.add(issue)
        return
    issue.occurrence_count += 1
    issue.last_seen_at = utcnow()


def _mark_issue_resolved(session: Session, brand: Brand, source_model_text: str, car_model: CarModel) -> None:
    issue = session.scalar(
        select(NormalizationIssue).where(
            NormalizationIssue.brand_id == brand.id,
            NormalizationIssue.normalized_source_model == normalize_alias_text(source_model_text),
        )
    )
    if issue is None:
        return
    issue.status = "resolved"
    issue.resolved_model_id = car_model.id
    issue.last_seen_at = utcnow()


def resolve_catalog_values(
    session: Session,
    *,
    make: str | None,
    source_model_text: str | None,
    source_generation_text: str | None,
    suggested_model: str | None = None,
    suggested_generation: str | None = None,
) -> CatalogResolution:
    if not make:
        return CatalogResolution(None, None, None, "missing_make", "none")

    brand = get_or_create_brand(session, make)
    assert brand is not None
    if not source_model_text:
        return CatalogResolution(brand, None, None, "missing_model", "none")

    model_alias = session.scalar(
        select(ModelAlias).where(
            ModelAlias.brand_id == brand.id,
            ModelAlias.normalized_alias == normalize_alias_text(source_model_text),
        )
    )
    confidence = "exact_alias"
    if model_alias:
        car_model = session.get(CarModel, model_alias.car_model_id)
    elif suggested_model and is_bootstrap_model(make, suggested_model):
        # A known bootstrap model may be used while the process is being upgraded.
        # Unknown source text never reaches get_or_create_model.
        car_model = get_or_create_model(session, brand, suggested_model)
        assert car_model is not None
        _upsert_model_alias(
            session,
            brand=brand,
            car_model=car_model,
            alias=source_model_text,
            source="bootstrap",
        )
        confidence = "bootstrap_catalog"
    else:
        _record_unknown_model(session, brand, source_model_text)
        return CatalogResolution(brand, None, None, "unknown_model", "unknown")

    assert car_model is not None
    _mark_issue_resolved(session, brand, source_model_text, car_model)

    if not source_generation_text:
        return CatalogResolution(brand, car_model, None, "resolved_model_only", confidence)

    generation_alias = session.scalar(
        select(GenerationAlias).where(
            GenerationAlias.car_model_id == car_model.id,
            GenerationAlias.normalized_alias == normalize_alias_text(source_generation_text),
        )
    )
    if generation_alias:
        generation = session.get(Generation, generation_alias.generation_id)
        return CatalogResolution(brand, car_model, generation, "resolved", "exact_generation_alias")

    canonical_generation = suggested_generation or source_generation_text
    generation = get_or_create_generation(session, car_model, canonical_generation)
    assert generation is not None
    _upsert_generation_alias(
        session,
        car_model=car_model,
        generation=generation,
        alias=source_generation_text,
        source="source_normalized",
    )
    return CatalogResolution(brand, car_model, generation, "resolved", confidence)


def resolve_normalized_listing(session: Session, item: NormalizedListing) -> CatalogResolution:
    resolution = resolve_catalog_values(
        session,
        make=item.make,
        source_model_text=item.source_model_text,
        source_generation_text=item.source_generation_text,
        suggested_model=item.model,
        suggested_generation=item.generation,
    )
    item.model = resolution.car_model.name if resolution.car_model else None
    item.generation = resolution.generation.name if resolution.generation else None
    item.normalization_status = resolution.status
    item.normalization_confidence = resolution.confidence
    item.analytics_eligible = (
        resolution.status in {"resolved", "resolved_model_only"}
        and not bool(set(item.quality_flags) & CRITICAL_QUALITY_FLAGS)
    )
    return resolution


def reprocess_catalog(session: Session) -> dict[str, int]:
    """Reparse every stored title and apply the current catalog rules without re-downloading data."""
    counters = {"checked": 0, "reparsed": 0, "resolved": 0, "unknown": 0, "changed": 0}
    listings = session.scalars(select(Listing).order_by(Listing.id)).all()
    for listing in listings:
        counters["checked"] += 1
        fallback_make = listing.source_make_text
        if not fallback_make and listing.brand_id:
            existing_brand = session.get(Brand, listing.brand_id)
            fallback_make = existing_brand.name if existing_brand else None

        suggested_model = None
        suggested_generation = None
        if listing.title:
            parsed = normalize_mashina_listing(
                {
                    "id": listing.external_id,
                    "title": listing.title,
                    "status": listing.status,
                    "prices": [],
                    "attributes": [
                        {"slug": "year", "value_number": listing.year},
                        {"slug": "mileage", "value_number": listing.mileage_km},
                    ],
                },
                fallback_make=fallback_make,
            )
            old_source_values = (
                listing.source_make_text,
                listing.source_model_text,
                listing.source_generation_text,
                tuple(listing.quality_flags or []),
            )
            listing.source_make_text = parsed.source_make_text
            listing.source_model_text = parsed.source_model_text
            listing.source_generation_text = parsed.source_generation_text
            listing.quality_flags = parsed.quality_flags
            fallback_make = parsed.make
            suggested_model = parsed.model
            suggested_generation = parsed.generation
            if old_source_values != (
                listing.source_make_text,
                listing.source_model_text,
                listing.source_generation_text,
                tuple(listing.quality_flags or []),
            ):
                counters["reparsed"] += 1

        resolution = resolve_catalog_values(
            session,
            make=fallback_make,
            source_model_text=listing.source_model_text,
            source_generation_text=listing.source_generation_text,
            suggested_model=suggested_model,
            suggested_generation=suggested_generation,
        )
        status = resolution.status
        analytics_eligible = (
            status in {"resolved", "resolved_model_only"}
            and not bool(set(listing.quality_flags or []) & CRITICAL_QUALITY_FLAGS)
        )
        old_values = (
            listing.brand_id,
            listing.car_model_id,
            listing.generation_id,
            listing.normalization_status,
            listing.normalization_confidence,
            listing.analytics_eligible,
        )
        listing.brand_id = resolution.brand.id if resolution.brand else None
        listing.car_model_id = resolution.car_model.id if resolution.car_model else None
        listing.generation_id = resolution.generation.id if resolution.generation else None
        listing.normalization_status = status
        listing.normalization_confidence = resolution.confidence
        listing.analytics_eligible = analytics_eligible
        new_values = (
            listing.brand_id,
            listing.car_model_id,
            listing.generation_id,
            listing.normalization_status,
            listing.normalization_confidence,
            listing.analytics_eligible,
        )
        if old_values != new_values:
            counters["changed"] += 1
        if status in {"resolved", "resolved_model_only"}:
            counters["resolved"] += 1
        else:
            counters["unknown"] += 1
    return counters
