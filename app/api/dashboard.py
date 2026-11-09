from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.cache import cached_call
from app.api.roles import require_subscriber
from app.db.session import get_session
from app.schemas import BrandStat, DashboardSummary
from app.services.dashboard import get_brand_stats, get_dashboard_summary

# Графики и аналитика — подписчикам (роли сайта, PLATFORM_ADR.md). Гейт на
# роутере: новую ручку здесь нельзя завести открытой.
router = APIRouter(
    prefix="/api/v1/dashboard", tags=["dashboard"], dependencies=[Depends(require_subscriber)]
)


@router.get("/summary", response_model=DashboardSummary)
def dashboard_summary(session: Session = Depends(get_session)) -> dict:
    return cached_call("dashboard.summary", get_dashboard_summary, session)


@router.get("/brand-stats", response_model=list[BrandStat])
def dashboard_brand_stats(session: Session = Depends(get_session)) -> list[dict]:
    return cached_call("dashboard.brand_stats", get_brand_stats, session)
