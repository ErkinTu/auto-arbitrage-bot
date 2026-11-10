from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.cache import cached_call
from app.db.session import get_session
from app.schemas import BrandStat, DashboardSummary
from app.services.dashboard import get_brand_stats, get_dashboard_summary

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
def dashboard_summary(session: Session = Depends(get_session)) -> dict:
    return cached_call("dashboard.summary", get_dashboard_summary, session)


@router.get("/brand-stats", response_model=list[BrandStat])
def dashboard_brand_stats(session: Session = Depends(get_session)) -> list[dict]:
    return cached_call("dashboard.brand_stats", get_brand_stats, session)
