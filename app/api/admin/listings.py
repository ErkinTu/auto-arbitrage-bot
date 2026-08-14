"""База объявлений для панели. Только чтение."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.admin.deps import current_admin
from app.db.session import get_session
from app.schemas import AdminListingDetail, AdminListingPage
from app.services.admin_listings import MAX_LIMIT, get_listing, list_listings

router = APIRouter(
    prefix="/api/v1/admin/listings",
    tags=["admin"],
    dependencies=[Depends(current_admin)],
)


@router.get("", response_model=AdminListingPage)
def listings_page(
    state: str | None = Query(default="active"),
    source_id: int | None = Query(default=None, ge=1),
    brand_id: int | None = Query(default=None, ge=1),
    model_id: int | None = Query(default=None, ge=1),
    year_from: int | None = Query(default=None, ge=1900, le=2100),
    year_to: int | None = Query(default=None, ge=1900, le=2100),
    price_from: int | None = Query(default=None, ge=0),
    price_to: int | None = Query(default=None, ge=0),
    city: str | None = Query(default=None, max_length=80),
    query: str | None = Query(default=None, alias="q", max_length=80),
    sort: str = Query(default="fresh"),
    limit: int = Query(default=20, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> dict:
    return list_listings(
        session,
        state=state,
        source_id=source_id,
        brand_id=brand_id,
        model_id=model_id,
        year_from=year_from,
        year_to=year_to,
        price_from=price_from,
        price_to=price_to,
        city=city,
        query=query,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get("/{listing_id}", response_model=AdminListingDetail)
def listing_detail(listing_id: int, session: Session = Depends(get_session)) -> dict:
    try:
        return get_listing(session, listing_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
