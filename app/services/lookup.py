from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Brand, CarModel, Generation, Source, SyncTarget


def ensure_brand_targets(session: Session, source: Source, brands: list[str]) -> list[SyncTarget]:
    """Brand-level sync_targets для перечисленных марок (создает отсутствующие)."""
    settings = get_settings()
    targets: list[SyncTarget] = []
    for brand in brands:
        target = session.scalar(
            select(SyncTarget).where(
                SyncTarget.source_id == source.id,
                SyncTarget.brand_filter == brand,
                SyncTarget.model_filter.is_(None),
                SyncTarget.generation_filter.is_(None),
            )
        )
        if target is None:
            target = SyncTarget(
                source_id=source.id,
                brand_filter=brand,
                fresh_period=settings.bot_fresh_period,
                page_size=settings.default_page_size,
                max_pages=settings.default_max_pages,
                max_items=settings.default_max_items,
                sync_interval_minutes=settings.default_sync_interval_minutes,
            )
            session.add(target)
            session.commit()
        targets.append(target)
    return targets


def get_or_create_source(session: Session, name: str, base_url: str) -> Source:
    source = session.scalar(select(Source).where(Source.name == name))
    if source:
        return source
    source = Source(name=name, base_url=base_url)
    session.add(source)
    session.flush()
    return source


def get_or_create_brand(session: Session, name: str | None) -> Brand | None:
    if not name:
        return None
    brand = session.scalar(select(Brand).where(Brand.name == name))
    if brand:
        return brand
    brand = Brand(name=name, slug=name.lower().replace(" ", "-"))
    session.add(brand)
    session.flush()
    return brand


def get_or_create_model(session: Session, brand: Brand | None, name: str | None) -> CarModel | None:
    if not brand or not name:
        return None
    car_model = session.scalar(
        select(CarModel).where(CarModel.brand_id == brand.id, CarModel.name == name)
    )
    if car_model:
        return car_model
    car_model = CarModel(brand_id=brand.id, name=name, slug=name.lower().replace(" ", "-"))
    session.add(car_model)
    session.flush()
    return car_model


def get_or_create_generation(
    session: Session, car_model: CarModel | None, name: str | None
) -> Generation | None:
    if not car_model or not name:
        return None
    generation = session.scalar(
        select(Generation).where(Generation.car_model_id == car_model.id, Generation.name == name)
    )
    if generation:
        return generation
    generation = Generation(
        car_model_id=car_model.id,
        name=name,
        code=None,
        is_facelift="рестайлинг" in name.lower(),
    )
    session.add(generation)
    session.flush()
    return generation
