from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.cache import cached_call
from app.api.roles import require_subscriber
from app.db.session import get_session
from app.schemas import EstimateResponse, TrendResponse
from app.services.analytics import estimate_price, price_trend

# Графики и аналитика — подписчикам (роли сайта, PLATFORM_ADR.md). Гейт на
# роутере: новую ручку здесь нельзя завести открытой.
router = APIRouter(
    prefix="/api/v1", tags=["analytics"], dependencies=[Depends(require_subscriber)]
)


@router.get("/estimate", response_model=EstimateResponse)
def get_estimate(
    brand: str = Query(min_length=1, max_length=100),
    model: str = Query(min_length=1, max_length=120),
    generation: str | None = Query(default=None, max_length=160),
    year: int = Query(ge=1900, le=2100),
    mileage_km: int = Query(ge=0, le=1_000_000),
    customs_status: str | None = Query(default=None, max_length=30),
    session: Session = Depends(get_session),
):
    try:
        return cached_call(
            "analytics.estimate",
            estimate_price,
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
    brand: str = Query(min_length=1, max_length=100),
    model: str = Query(min_length=1, max_length=120),
    generation: str | None = Query(default=None, max_length=160),
    year: int = Query(ge=1900, le=2100),
    mileage_km: int = Query(ge=0, le=1_000_000),
    days: int = Query(default=365, ge=1, le=3650),
    session: Session = Depends(get_session),
):
    try:
        return cached_call(
            "analytics.price_trend",
            price_trend,
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
