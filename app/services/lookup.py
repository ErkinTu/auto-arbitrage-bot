from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Brand, CarModel, Generation, Source


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
