from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.schemas import VehicleSummary
from app.services.vehicle_summary import get_vehicle_summary

router = APIRouter(prefix="/api/v1/market", tags=["market"])


@router.get("/vehicle-summary", response_model=VehicleSummary)
def vehicle_summary(
    brand_id: int = Query(ge=1),
    model_id: int = Query(ge=1),
    year: int = Query(ge=1900, le=2100),
    generation_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> dict:
    try:
        return get_vehicle_summary(
            session,
            brand_id=brand_id,
            model_id=model_id,
            year=year,
            generation_id=generation_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
