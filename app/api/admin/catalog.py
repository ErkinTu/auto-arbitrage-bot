"""Справочники и оценка — те же данные, но за админ-гейтом.

Экран «Рынок» в панели зовёт каталог и `vehicle-summary`. Публичные версии этих
ручек живут вне `/admin`, и наружу их пришлось бы открыть вместе с панелью — то
есть выставить всю аналитику в интернет без авторизации. Вместо этого держим
здесь копии под `Depends(current_admin)`, а Caddy пропускает снаружи **только**
`/api/v1/admin/*`.

Логики тут нет — те же сервисы, что и у публичных роутеров. Если появится
третий потребитель, дублировать роутер снова не нужно: выносить надо будет
общий сервис, а не копировать ручки.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.admin.deps import current_admin
from app.db.session import get_session
from app.schemas import BrandItem, ModelItem, VehicleSummary, YearItem
from app.services.catalog_browse import list_brands, list_models, list_years
from app.services.vehicle_summary import Granularity, get_vehicle_summary

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(current_admin)],
)


@router.get("/catalog/brands", response_model=list[BrandItem])
def catalog_brands(session: Session = Depends(get_session)) -> list[dict]:
    return list_brands(session)


@router.get("/catalog/models", response_model=list[ModelItem])
def catalog_models(
    brand_id: int = Query(ge=1),
    session: Session = Depends(get_session),
) -> list[dict]:
    return list_models(session, brand_id=brand_id)


@router.get("/catalog/years", response_model=list[YearItem])
def catalog_years(
    brand_id: int = Query(ge=1),
    model_id: int = Query(ge=1),
    session: Session = Depends(get_session),
) -> list[dict]:
    return list_years(session, brand_id=brand_id, model_id=model_id)


@router.get("/market/vehicle-summary", response_model=VehicleSummary)
def vehicle_summary(
    brand_id: int = Query(ge=1),
    model_id: int = Query(ge=1),
    year: int = Query(ge=1900, le=2100),
    generation_id: int | None = Query(default=None, ge=1),
    mileage_km: int | None = Query(default=None, ge=0, le=1_000_000),
    granularity: Granularity = Query(default="week"),
    session: Session = Depends(get_session),
) -> dict:
    try:
        return get_vehicle_summary(
            session,
            brand_id=brand_id,
            model_id=model_id,
            year=year,
            generation_id=generation_id,
            mileage_km=mileage_km,
            granularity=granularity,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
