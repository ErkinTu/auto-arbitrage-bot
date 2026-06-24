from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.schemas import EstimateResponse, TrendResponse
from app.services.analytics import estimate_price, price_trend

router = APIRouter(prefix="/api/v1", tags=["analytics"])


@router.get("/estimate", response_model=EstimateResponse)
def get_estimate(
    brand: str,
    model: str,
    generation: str | None = None,
    year: int = Query(ge=1900, le=2100),
    mileage_km: int = Query(ge=0),
    customs_status: str | None = None,
    session: Session = Depends(get_session),
):
    try:
        return estimate_price(
            session,
            brand_name=brand,
            model_name=model,
            generation_name=generation,
            year=year,
            mileage_km=mileage_km,
            customs_status=customs_status,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/price-trend", response_model=TrendResponse)
def get_price_trend(
    brand: str,
    model: str,
    generation: str | None = None,
    year: int = Query(ge=1900, le=2100),
    mileage_km: int = Query(ge=0),
    days: int = Query(default=365, ge=1, le=3650),
    session: Session = Depends(get_session),
):
    try:
        return price_trend(
            session,
            brand_name=brand,
            model_name=model,
            generation_name=generation,
            year=year,
            mileage_km=mileage_km,
            days=days,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
