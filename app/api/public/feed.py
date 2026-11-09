"""Публичные ручки сайта: лента, счётчик фильтра, карточка, избранное, «лучшее».

Всё только GET и только чтение. Граница наружу — префикс `/api/v1/public/`
(Caddy пропускает его в белом списке, лимитер — политика public, счётчик
фильтра — heavy). ПДн в ответах нет по построению: сериализацию делает
`services/public_feed.serialize`, схемы — `schemas.Public*`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.cache import cached_call
from app.core.config import get_settings
from app.db.session import get_session
from app.schemas import PublicFeedCount, PublicFeedPage, PublicListing, PublicListingDetail
from app.services.public_feed import (
    CursorError,
    Deal,
    FeedFilter,
    Sort,
    best_now,
    feed_count,
    feed_page,
    listing_detail,
    listings_by_ids,
)

router = APIRouter(prefix="/api/v1/public", tags=["public"])

_FUELS = {"petrol", "petrol_gas", "hybrid", "electric", "diesel"}


def _int_list(raw: str | None, *, name: str, max_items: int = 20) -> list[int]:
    if not raw:
        return []
    try:
        values = [int(part) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{name}: ожидаются числа") from exc
    if len(values) > max_items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{name}: не больше {max_items}")
    return values


def feed_filter(
    brand_ids: Annotated[str | None, Query(max_length=200, description="через запятую")] = None,
    model_id: Annotated[int | None, Query(ge=1)] = None,
    cities: Annotated[str | None, Query(max_length=300, description="через запятую")] = None,
    source: Annotated[str | None, Query(pattern="^(mashina|lalafo)$")] = None,
    price_from: Annotated[int | None, Query(ge=0, le=10_000_000)] = None,
    price_to: Annotated[int | None, Query(ge=0, le=10_000_000)] = None,
    year_from: Annotated[int | None, Query(ge=1950, le=2100)] = None,
    year_to: Annotated[int | None, Query(ge=1950, le=2100)] = None,
    mileage_from: Annotated[int | None, Query(ge=0, le=2_000_000)] = None,
    mileage_to: Annotated[int | None, Query(ge=0, le=2_000_000)] = None,
    seller_type: Annotated[str | None, Query(pattern="^(private|dealer)$")] = None,
    fuel: Annotated[str | None, Query(max_length=80, description="через запятую")] = None,
    with_photo: bool = False,
    new_24h: bool = False,
    price_dropped: bool = False,
    deal: Deal = "any",
) -> FeedFilter:
    city_list = [c.strip() for c in (cities or "").split(",") if c.strip()][:20]
    fuels = [f.strip() for f in (fuel or "").split(",") if f.strip()]
    if any(f not in _FUELS for f in fuels):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "fuel: неизвестный тип")
    return FeedFilter(
        brand_ids=sorted(set(_int_list(brand_ids, name="brand_ids"))),
        model_id=model_id,
        cities=sorted(set(city_list)),
        source=source,
        price_from=price_from,
        price_to=price_to,
        year_from=year_from,
        year_to=year_to,
        mileage_from=mileage_from,
        mileage_to=mileage_to,
        seller_type=seller_type,
        fuel_groups=sorted(set(fuels)),
        with_photo=with_photo,
        new_24h=new_24h,
        price_dropped=price_dropped,
        deal=deal,
    )


@router.get("/feed", response_model=PublicFeedPage)
def get_feed(
    flt: Annotated[FeedFilter, Depends(feed_filter)],
    sort: Sort = "fresh",
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    session: Session = Depends(get_session),
) -> dict:
    limit = min(limit, get_settings().public_feed_max_limit)
    try:
        return cached_call(
            "public.feed",
            feed_page,
            session,
            ttl=get_settings().public_feed_cache_seconds,
            flt=flt,
            sort=sort,
            limit=limit,
            cursor=cursor,
        )
    except CursorError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный курсор") from exc


@router.get("/feed/count", response_model=PublicFeedCount)
def get_feed_count(
    flt: Annotated[FeedFilter, Depends(feed_filter)],
    session: Session = Depends(get_session),
) -> dict:
    return cached_call(
        "public.feed_count",
        feed_count,
        session,
        ttl=get_settings().public_feed_cache_seconds,
        flt=flt,
    )


@router.get("/best", response_model=list[PublicListing])
def get_best(
    limit: Annotated[int, Query(ge=1, le=24)] = 12,
    session: Session = Depends(get_session),
) -> list[dict]:
    return cached_call("public.best", best_now, session, limit=limit)


@router.get("/listings", response_model=list[PublicListing])
def get_listings(
    ids: Annotated[str, Query(max_length=600, description="id через запятую, до 50")],
    session: Session = Depends(get_session),
) -> list[dict]:
    """Избранное: и активные, и снятые — снятые фронт показывает тусклыми."""
    id_list = _int_list(ids, name="ids", max_items=50)
    return cached_call(
        "public.listings",
        listings_by_ids,
        session,
        ttl=get_settings().public_feed_cache_seconds,
        ids=id_list,
    )


@router.get("/listings/{listing_id}", response_model=PublicListingDetail)
def get_listing(listing_id: int, session: Session = Depends(get_session)) -> dict:
    try:
        return cached_call(
            "public.listing",
            listing_detail,
            session,
            ttl=get_settings().public_feed_cache_seconds,
            listing_id=listing_id,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
