"""Главная панели, воронка и прогоны парсеров. Только чтение."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.admin.deps import current_admin
from app.db.session import get_session
from app.schemas import AdminFunnel, AdminOverview, AdminParserRun
from app.services.admin_overview import get_overview, list_parser_runs
from app.services.funnel import funnel_steps

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(current_admin)],
)


@router.get("/overview", response_model=AdminOverview)
def overview(
    funnel_days: int = Query(default=30, ge=1, le=365),
    session: Session = Depends(get_session),
) -> dict:
    return get_overview(session, funnel_days=funnel_days)


@router.get("/stats/funnel", response_model=AdminFunnel)
def funnel(
    days: int = Query(default=30, ge=1, le=365),
    session: Session = Depends(get_session),
) -> dict:
    return {"days": days, "steps": funnel_steps(session, days=days)}


@router.get("/ops/parser-runs", response_model=list[AdminParserRun])
def parser_runs(
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[dict]:
    return list_parser_runs(session, limit=limit)
