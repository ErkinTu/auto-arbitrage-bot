"""База объявлений для админ-панели. Только чтение.

Лента отвечает на «что сейчас в базе и что с ней не так», карточка — на
«почему это объявление такое». Отсюда две вещи, которых нет в публичном API:

* фильтры по служебным полям (`analytics_eligible`, `normalization_status`,
  флаги качества) — именно они объясняют, почему объявление не попало в
  статистику;
* цена рядом со средней по рынку: без неё «$32 500» ничего не говорит.

Потолок выборки жёсткий. В базе 60k+ строк, сервер — 2 ГБ на четыре сервиса, и
«выгрузить всё» отсюда невозможно намеренно.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import String, func, or_, select
from sqlalchemy.orm import Session

from app.bot.matching import market_average
from app.core.config import get_settings
from app.models import (
    Brand,
    CarModel,
    Generation,
    Listing,
    ListingPriceHistory,
    ListingStatusHistory,
    RawPayload,
    Source,
)
from app.services.sources import listing_public_url
from app.services.vehicle_summary import _extract_image_url

MAX_LIMIT = 200

# Состояния ленты. Не булевы флаги по отдельности: комбинации вроде «снятое, но
# в аналитике» бессмысленны, а из четырёх осмысленных состояний выбирают одно.
STATES = ("active", "inactive", "eligible", "flagged")

# nullslast обязателен у обеих ценовых сортировок: в Postgres DESC ставит NULL
# ПЕРВЫМИ, и «сначала дорогие» открывалось бы страницей объявлений без цены.
SORTS = {
    "fresh": Listing.last_seen_at.desc(),
    "new": Listing.first_seen_at.desc(),
    "price_asc": Listing.current_price_usd.asc().nullslast(),
    "price_desc": Listing.current_price_usd.desc().nullslast(),
}


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _conditions(
    *,
    state: str | None,
    source_id: int | None,
    brand_id: int | None,
    model_id: int | None,
    year_from: int | None,
    year_to: int | None,
    price_from: int | None,
    price_to: int | None,
    city: str | None,
    query: str | None,
) -> list:
    conditions: list = []

    if state == "active":
        conditions.append(Listing.is_active.is_(True))
    elif state == "inactive":
        conditions.append(Listing.is_active.is_(False))
    elif state == "eligible":
        conditions.extend(
            [Listing.is_active.is_(True), Listing.analytics_eligible.is_(True)]
        )
    elif state == "flagged":
        # Объявления с флагами качества — то, что портит статистику. quality_flags
        # это JSON-массив; пустой сериализуется как «[]», то есть длина 2. Индекса
        # по нему нет и не нужно: срез смотрят руками и редко.
        conditions.append(func.length(func.cast(Listing.quality_flags, String)) > 2)

    if source_id:
        conditions.append(Listing.source_id == source_id)
    if brand_id:
        conditions.append(Listing.brand_id == brand_id)
    if model_id:
        conditions.append(Listing.car_model_id == model_id)
    if year_from:
        conditions.append(Listing.year >= year_from)
    if year_to:
        conditions.append(Listing.year <= year_to)
    if price_from:
        conditions.append(Listing.current_price_usd >= price_from)
    if price_to:
        conditions.append(Listing.current_price_usd <= price_to)
    if city:
        conditions.append(Listing.city.ilike(f"%{city.strip()}%"))
    if query:
        needle = f"%{query.strip()}%"
        conditions.append(
            or_(
                Listing.title.ilike(needle),
                Listing.external_id.ilike(needle),
                Listing.source_model_text.ilike(needle),
            )
        )

    return conditions


def list_listings(
    session: Session,
    *,
    state: str | None = "active",
    source_id: int | None = None,
    brand_id: int | None = None,
    model_id: int | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    price_from: int | None = None,
    price_to: int | None = None,
    city: str | None = None,
    query: str | None = None,
    sort: str = "fresh",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    conditions = _conditions(
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
    )

    total = session.scalar(select(func.count(Listing.id)).where(*conditions)) or 0

    rows = session.execute(
        select(Listing, Brand.name, CarModel.name, Source.name)
        .join(Source, Source.id == Listing.source_id)
        .outerjoin(Brand, Brand.id == Listing.brand_id)
        .outerjoin(CarModel, CarModel.id == Listing.car_model_id)
        .where(*conditions)
        .order_by(SORTS.get(sort, SORTS["fresh"]))
        .limit(limit)
        .offset(offset)
    ).all()

    items = [
        {
            "id": listing.id,
            "title": _title(listing, brand_name, model_name),
            "price_usd": float(listing.current_price_usd) if listing.current_price_usd else None,
            "year": listing.year,
            "mileage_km": listing.mileage_km,
            "city": listing.city,
            "source": source_name,
            "is_active": listing.is_active,
            "analytics_eligible": listing.analytics_eligible,
            "quality_flags": list(listing.quality_flags or []),
            "last_seen_at": _aware(listing.last_seen_at),
            "deactivated_at": _aware(listing.deactivated_at),
        }
        for listing, brand_name, model_name, source_name in rows
    ]

    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _title(listing: Listing, brand_name: str | None, model_name: str | None) -> str:
    """Марка + модель + год, а не сырой заголовок источника.

    Заголовки у источников разные («Toyota Camry IX (XV80) China Market 2.0hyb
    CVT»), и лента из них не сканируется. Если модель не распознана, честно
    показываем исходный текст — по такой строке и видно, что нормализация не
    справилась.
    """
    if brand_name and model_name:
        return f"{brand_name} {model_name}{f' {listing.year}' if listing.year else ''}"
    return listing.title or listing.source_model_text or f"#{listing.external_id}"


def get_listing(session: Session, listing_id: int) -> dict:
    row = session.execute(
        select(Listing, Brand.name, CarModel.name, Generation.name, Source.name)
        .join(Source, Source.id == Listing.source_id)
        .outerjoin(Brand, Brand.id == Listing.brand_id)
        .outerjoin(CarModel, CarModel.id == Listing.car_model_id)
        .outerjoin(Generation, Generation.id == Listing.generation_id)
        .where(Listing.id == listing_id)
    ).first()
    if row is None:
        raise LookupError("Объявление не найдено")

    listing, brand_name, model_name, generation_name, source_name = row

    price_history = session.scalars(
        select(ListingPriceHistory)
        .where(ListingPriceHistory.listing_id == listing.id)
        .order_by(ListingPriceHistory.observed_at.desc())
        .limit(50)
    ).all()
    status_history = session.scalars(
        select(ListingStatusHistory)
        .where(ListingStatusHistory.listing_id == listing.id)
        .order_by(ListingStatusHistory.observed_at.desc())
        .limit(20)
    ).all()

    payload = session.scalar(
        select(RawPayload)
        .where(RawPayload.listing_id == listing.id)
        .order_by(RawPayload.observed_at.desc())
    )

    # Средняя по рынку — тем же кодом, которым бот решает «ниже рынка». Иначе
    # панель показывала бы одну цифру, а пуш пользователю — другую.
    settings = get_settings()
    average = market_average(session, listing, min_samples=settings.bot_below_avg_min_samples)
    price = float(listing.current_price_usd) if listing.current_price_usd else None
    deviation = (
        round((price - average) / average * 100, 1) if average and price else None
    )

    return {
        "id": listing.id,
        "title": _title(listing, brand_name, model_name),
        "raw_title": listing.title,
        "brand": brand_name,
        "model": model_name,
        "generation": generation_name,
        "source": source_name,
        "external_id": listing.external_id,
        "source_url": listing_public_url(listing.source_slug, listing.external_id),
        "image_url": _extract_image_url(payload.payload if payload else None),
        "price_usd": price,
        "price_currency": listing.current_price_currency,
        "market_average_usd": round(average, 2) if average else None,
        "deviation_percent": deviation,
        "year": listing.year,
        "mileage_km": listing.mileage_km,
        "city": listing.city,
        "body_type": listing.body_type,
        "engine_volume": float(listing.engine_volume) if listing.engine_volume else None,
        "fuel_type": listing.fuel_type,
        "gearbox": listing.gearbox,
        "drive_type": listing.drive_type,
        "customs_status": listing.customs_status,
        "seller_type": listing.seller_type,
        "is_active": listing.is_active,
        "status": listing.status,
        "analytics_eligible": listing.analytics_eligible,
        "normalization_status": listing.normalization_status,
        "quality_flags": list(listing.quality_flags or []),
        "source_created_at": _aware(listing.source_created_at),
        "first_seen_at": _aware(listing.first_seen_at),
        "last_seen_at": _aware(listing.last_seen_at),
        "deactivated_at": _aware(listing.deactivated_at),
        "price_history": [
            {
                "observed_at": _aware(item.observed_at),
                "price_usd": float(item.price_usd) if item.price_usd else None,
                "event_type": item.event_type,
            }
            for item in price_history
        ],
        "status_history": [
            {
                "observed_at": _aware(item.observed_at),
                "event_type": item.event_type,
                "old_status": item.old_status,
                "new_status": item.new_status,
                "details": item.details,
            }
            for item in status_history
        ],
    }
