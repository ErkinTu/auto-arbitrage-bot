"""Оценка «Оценить авто» — открыта гостям (решение владельца 10.11.2026).

Тот же расчёт, что `/market/vehicle-summary` (подписка), но без того, что
относится к аналитике рынка: список аномалий со ссылками на объявления и
«представитель» когорты. Графики оценки (динамика, приток, распределение) —
часть ответа на вопрос «сколько стоит моя машина», поэтому остаются.
Лимит — heavy (`core/ratelimit.py`), ответ кэшируется общим `cached_call`.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.cache import cached_call
from app.db.session import get_session
from app.schemas import VehicleSummary
from app.services.vehicle_summary import Granularity, get_vehicle_summary

router = APIRouter(prefix="/api/v1/public", tags=["public"])

_NO_ANOMALIES = {"count": 0, "low_count": 0, "high_count": 0, "items": []}


@router.get("/estimate", response_model=VehicleSummary)
def public_estimate(
    brand_id: int = Query(ge=1),
    model_id: int = Query(ge=1),
    year: int = Query(ge=1900, le=2100),
    mileage_km: int | None = Query(default=None, ge=0, le=1_000_000),
    granularity: Granularity = Query(default="week"),
    session: Session = Depends(get_session),
) -> dict:
    try:
        # Тот же ключ кэша, что у подписочной ручки: расчёт один и тот же.
        summary = cached_call(
            "market.vehicle_summary",
            get_vehicle_summary,
            session,
            brand_id=brand_id,
            model_id=model_id,
            year=year,
            generation_id=None,
            mileage_km=mileage_km,
            granularity=granularity,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Копия: кэш отдаёт общий объект, портить его нельзя.
    return {**summary, "anomalies": _NO_ANOMALIES, "representative_listing": None}
