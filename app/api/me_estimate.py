"""Уровни точности «Оценить авто» (решение владельца 19.11.2026).

Уровень 1 — всем, в `/public/estimate` (не меняется). Здесь — дополнительные:
подписчику уровни 1–2, админу 1–3 (уровень 3 — со снятыми с продажи). Ответ
зависит от роли, поэтому ручка под `/me/` (кука сессии, без публичного кэша), а
не под `/public/`. Гейт — на роутере. Расчёт кэшируется общим `cached_call`
(одинаков для всех), обрезка по роли — после кэша.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.roles import require_subscriber
from app.core.cache import cached_call
from app.db.session import get_session
from app.schemas import EstimateLevels
from app.services.estimate_levels import estimate_levels
from app.services.site_auth import Viewer, role_at_least

router = APIRouter(prefix="/api/v1/me", tags=["me"], dependencies=[Depends(require_subscriber)])


@router.get("/estimate-levels", response_model=EstimateLevels)
def get_estimate_levels(
    viewer: Annotated[Viewer, Depends(require_subscriber)],
    brand_id: int = Query(ge=1),
    model_id: int = Query(ge=1),
    year: int = Query(ge=1900, le=2100),
    mileage_km: int | None = Query(default=None, ge=0, le=1_000_000),
    session: Session = Depends(get_session),
) -> dict:
    levels = cached_call(
        "me.estimate_levels",
        estimate_levels,
        session,
        brand_id=brand_id,
        model_id=model_id,
        year=year,
        mileage_km=mileage_km,
    )
    top = 3 if role_at_least(viewer.role, "admin") else 2
    return {"levels": [level for level in levels if level["level"] <= top]}
