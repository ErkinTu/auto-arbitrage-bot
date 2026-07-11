from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.schemas import BrandItem, ModelItem, YearItem
from app.services.catalog_browse import list_brands, list_models, list_years

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])


@router.get("/brands", response_model=list[BrandItem])
def catalog_brands(session: Session = Depends(get_session)) -> list[dict]:
    return list_brands(session)


@router.get("/models", response_model=list[ModelItem])
def catalog_models(
    brand_id: int = Query(ge=1),
    session: Session = Depends(get_session),
) -> list[dict]:
    return list_models(session, brand_id=brand_id)


@router.get("/years", response_model=list[YearItem])
def catalog_years(
    brand_id: int = Query(ge=1),
    model_id: int = Query(ge=1),
    session: Session = Depends(get_session),
) -> list[dict]:
    return list_years(session, brand_id=brand_id, model_id=model_id)
