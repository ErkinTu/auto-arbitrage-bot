from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Brand, CarModel, Listing


def list_brands(session: Session) -> list[dict]:
    rows = session.execute(
        select(Brand.id, Brand.name, func.count(Listing.id).label("active_count"))
        .join(Listing, Listing.brand_id == Brand.id)
        .where(
            Listing.is_active.is_(True),
            Listing.analytics_eligible.is_(True),
        )
        .group_by(Brand.id, Brand.name)
        .having(func.count(Listing.id) > 0)
        .order_by(Brand.name.asc())
    ).all()
    return [
        {"id": int(brand_id), "name": name, "active_count": int(count or 0)}
        for brand_id, name, count in rows
    ]


def list_models(session: Session, brand_id: int) -> list[dict]:
    rows = session.execute(
        select(CarModel.id, CarModel.name, func.count(Listing.id).label("active_count"))
        .join(Listing, Listing.car_model_id == CarModel.id)
        .where(
            CarModel.brand_id == brand_id,
            Listing.is_active.is_(True),
            Listing.analytics_eligible.is_(True),
        )
        .group_by(CarModel.id, CarModel.name)
        .having(func.count(Listing.id) > 0)
        .order_by(CarModel.name.asc())
    ).all()
    return [
        {"id": int(model_id), "name": name, "active_count": int(count or 0)}
        for model_id, name, count in rows
    ]


def list_years(session: Session, brand_id: int, model_id: int) -> list[dict]:
    rows = session.execute(
        select(
            Listing.year,
            func.count(Listing.id).label("active_count"),
            func.sum(case((Listing.analytics_eligible.is_(True), 1), else_=0)).label(
                "analytics_eligible_count"
            ),
        )
        .where(
            Listing.brand_id == brand_id,
            Listing.car_model_id == model_id,
            Listing.is_active.is_(True),
            Listing.year.is_not(None),
        )
        .group_by(Listing.year)
        .order_by(Listing.year.desc())
    ).all()
    return [
        {
            "year": int(year),
            "active_count": int(active or 0),
            "analytics_eligible_count": int(eligible or 0),
        }
        for year, active, eligible in rows
    ]
