from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.schemas import VehicleSummary
from app.services.vehicle_summary import Granularity, get_vehicle_summary

router = APIRouter(prefix="/api/v1/market", tags=["market"])


@router.get("/vehicle-summary", response_model=VehicleSummary)
def vehicle_summary(
    brand_id: int = Query(ge=1),
    model_id: int = Query(ge=1),
    year: int = Query(ge=1900, le=2100),
    generation_id: int | None = Query(default=None, ge=1),
    # Пробег необязателен: у lalafo он не указан примерно у 43% объявлений,
    # и «не знаю» — обычный ответ пользователя, а не отказ заполнить форму.
    mileage_km: int | None = Query(default=None, ge=0, le=1_000_000),
    # Шаг графиков. По умолчанию неделя: месячная точка на полугодовом окне
    # сглаживает ровно то, ради чего на график и смотрят.
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
        raise HTTPException(status_code=404, detail=str(exc)) from exc
