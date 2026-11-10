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

from app.api.roles import current_viewer
from app.core.cache import cached_call
from app.core.config import get_settings
from app.db.session import get_session
from app.schemas import PublicFeedCount, PublicFeedFacets, PublicFeedHistograms, PublicFeedPage, PublicListing, PublicListingDetail
from app.services.regions import REGION_LABELS
from app.services.specs import SPEC_GROUPS, raw_values_for
from app.services.site_auth import Viewer, role_at_least
from app.services.public_feed import (
    CursorError,
    Deal,
    FeedFilter,
    Sort,
    best_now,
    feed_count,
    cities_for_regions,
    feed_cities,
    feed_spec_values,
    feed_facets,
    feed_histograms,
    feed_page,
    listing_detail,
    listings_by_ids,
    similar_listings,
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


def _str_list(raw: str | None, *, max_items: int = 20) -> list[str]:
    """«Седан,Хэтчбек» → отсортированный список без дублей (ключ кэша стабилен)."""
    return sorted({part.strip() for part in (raw or "").split(",") if part.strip()})[:max_items]


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
    body_type: Annotated[str | None, Query(max_length=200, description="коды групп через запятую: sedan, suv, … (/feed/facets)")] = None,
    gearbox: Annotated[str | None, Query(max_length=100, description="automatic, manual, cvt, robot")] = None,
    drive: Annotated[str | None, Query(max_length=60, description="front, rear, awd")] = None,
    steering: Annotated[str | None, Query(max_length=30, description="left, right")] = None,
    customs: Annotated[str | None, Query(pattern="^(cleared|not_cleared)$")] = None,
    region: Annotated[str | None, Query(max_length=200, description="коды регионов через запятую: bishkek, chuy, …, foreign")] = None,
    with_photo: bool = False,
    new_24h: bool = False,
    price_dropped: bool = False,
    deal: Deal = "any",
    session: Session = Depends(get_session),
) -> FeedFilter:
    fuels = [f.strip() for f in (fuel or "").split(",") if f.strip()]
    if any(f not in _FUELS for f in fuels):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "fuel: неизвестный тип")
    regions = _str_list(region)
    if any(r not in REGION_LABELS or r == "local" for r in regions):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "region: неизвестный регион")
    specs: dict[str, list[str] | None] = {}
    for field_name, raw in (("body_type", body_type), ("gearbox", gearbox), ("drive_type", drive), ("steering_wheel", steering)):
        codes = _str_list(raw)
        allowed = {code for code, _, _ in SPEC_GROUPS[field_name]}
        if any(c not in allowed for c in codes):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field_name}: неизвестное значение")
        specs[field_name] = (
            raw_values_for(
                field_name,
                cached_call("public.feed_spec_values", feed_spec_values, session, field_name=field_name),
                codes,
            )
            if codes
            else None
        )
    region_cities = None
    if regions:
        cities_map = cached_call("public.feed_cities", feed_cities, session)
        region_cities = cities_for_regions(cities_map, regions)
    return FeedFilter(
        brand_ids=sorted(set(_int_list(brand_ids, name="brand_ids"))),
        model_id=model_id,
        cities=_str_list(cities),
        source=source,
        price_from=price_from,
        price_to=price_to,
        year_from=year_from,
        year_to=year_to,
        mileage_from=mileage_from,
        mileage_to=mileage_to,
        seller_type=seller_type,
        fuel_groups=sorted(set(fuels)),
        body_types=specs["body_type"],
        gearboxes=specs["gearbox"],
        drive_types=specs["drive_type"],
        steering=specs["steering_wheel"],
        customs=customs,
        regions=regions,
        region_cities=region_cities,
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
    cities_for_regions,
    feed_cities,
    feed_facets,
    feed_histograms,
        session,
        ttl=get_settings().public_feed_cache_seconds,
        flt=flt,
    )


@router.get("/feed/histograms", response_model=PublicFeedHistograms)
def get_feed_histograms(
    flt: Annotated[FeedFilter, Depends(feed_filter)],
    session: Session = Depends(get_session),
) -> dict:
    """Гистограммы цены/года/пробега под ползунки — с учётом остальных фильтров
    (свой диапазон каждой не применяется). Политика лимита heavy, как у count."""
    return cached_call(
        "public.feed_histograms",
        feed_histograms,
        session,
        ttl=get_settings().public_feed_cache_seconds,
        flt=flt,
    )


@router.get("/feed/facets", response_model=PublicFeedFacets)
def get_feed_facets(session: Session = Depends(get_session)) -> dict:
    """Значения для чипов фильтра: кузов, КПП, привод, руль, города, растаможка.
    Меняются медленно — общий TTL справочников (`API_CACHE_TTL_SECONDS`)."""
    return cached_call("public.feed_facets", feed_facets, session)


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
def get_listing(
    listing_id: int,
    session: Session = Depends(get_session),
    viewer: Viewer = Depends(current_viewer),
) -> dict:
    """Карточка. Оценка рынка — всем (решение владельца); график истории цены —
    с подпиской: гостю приходит пустой список и `price_history_locked=true`."""
    try:
        item = cached_call(
            "public.listing",
            listing_detail,
            session,
            ttl=get_settings().public_feed_cache_seconds,
            listing_id=listing_id,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    # Копия: объект из кэша общий для всех ролей — его не трогаем.
    locked = not role_at_least(viewer.role, "subscriber")
    return {**item, "price_history": [] if locked else item["price_history"], "price_history_locked": locked}


@router.get("/listings/{listing_id}/similar", response_model=list[PublicListing])
def get_similar(
    listing_id: int,
    limit: Annotated[int, Query(ge=1, le=12)] = 8,
    session: Session = Depends(get_session),
) -> list[dict]:
    """Похожие предложения: та же модель, год ±2, активные с фото."""
    try:
        return cached_call(
            "public.similar",
            similar_listings,
            session,
            ttl=get_settings().public_feed_cache_seconds,
            listing_id=listing_id,
            limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
