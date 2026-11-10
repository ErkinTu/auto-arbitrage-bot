from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.cache import cached_call
from app.api.roles import require_subscriber
from app.db.session import get_session
from app.schemas import BrandStat, DashboardSummary, MarketOverview, TopModel
from app.services.dashboard import get_brand_stats, get_dashboard_summary
from app.services.market_overview import market_overview, top_models

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


@router.get("/market", response_model=MarketOverview)
def dashboard_market(
    brand_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> dict:
    """Поступления по дням, распределение цен (рынок или марка), срок до снятия."""
    return cached_call("dashboard.market", market_overview, session, brand_id=brand_id)


@router.get("/top-models", response_model=list[TopModel])
def dashboard_top_models(session: Session = Depends(get_session)) -> list[dict]:
    """30 самых массовых моделей: объём, цены, типичный год/пробег, приток, тренд 30 дн."""
    return cached_call("dashboard.top_models", top_models, session)
