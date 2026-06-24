from __future__ import annotations

from dataclasses import dataclass
import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    Brand,
    Listing,
    ListingPriceHistory,
    ListingStatusHistory,
    ParserRun,
    RawPayload,
    Source,
    SyncTarget,
)
from app.parsers.base import ParserAdapter
from app.services.catalog_service import resolve_normalized_listing
from app.services.normalizer import NormalizedListing, normalize_mashina_listing

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RunSummary:
    run_id: int
    processed: int
    new: int
    updated: int
    price_changed: int
    reactivated: int
    deactivated: int
    skipped: int
    complete_scan: bool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _set_current_fields(listing: Listing, item: NormalizedListing) -> None:
    listing.title = item.title
    listing.source_slug = item.slug
    listing.source_make_text = item.source_make_text
    listing.source_model_text = item.source_model_text
    listing.source_generation_text = item.source_generation_text
    listing.normalization_status = item.normalization_status
    listing.normalization_confidence = item.normalization_confidence
    listing.quality_flags = item.quality_flags
    listing.analytics_eligible = item.analytics_eligible
    listing.year = item.year
    listing.mileage_km = item.mileage_km
    listing.body_type = item.body_type
    listing.engine_volume = item.engine_volume
    listing.fuel_type = item.fuel_type
    listing.gearbox = item.gearbox
    listing.drive_type = item.drive_type
    listing.steering_wheel = item.steering_wheel
    listing.city = item.city
    listing.customs_status = item.customs_status
    listing.seller_type = item.seller_type
    listing.is_verified_seller = item.is_verified_seller
    listing.is_official_dealer = item.is_official_dealer
    listing.current_price_amount = item.price_amount
    listing.current_price_currency = item.price_currency
    listing.current_price_kgs = item.price_kgs
    listing.current_price_usd = item.price_usd
    listing.status = item.status
    listing.source_created_at = item.source_created_at
    listing.source_upped_at = item.source_upped_at
    listing.payload_hash = item.payload_hash
    listing.last_seen_at = utcnow()
    listing.missing_full_scans = 0


def _append_price_history(session: Session, listing: Listing, item: NormalizedListing, event_type: str) -> None:
    session.add(
        ListingPriceHistory(
            listing_id=listing.id,
            event_type=event_type,
            price_amount=item.price_amount,
            price_currency=item.price_currency,
            price_kgs=item.price_kgs,
            price_usd=item.price_usd,
            mileage_km_snapshot=item.mileage_km,
            customs_status_snapshot=item.customs_status,
            observed_at=utcnow(),
        )
    )


def _append_status_history(
    session: Session,
    listing: Listing,
    event_type: str,
    old_status: str | None,
    new_status: str | None,
    details: dict | None = None,
) -> None:
    session.add(
        ListingStatusHistory(
            listing_id=listing.id,
            event_type=event_type,
            old_status=old_status,
            new_status=new_status,
            details=details,
            observed_at=utcnow(),
        )
    )


def _save_raw_payload(
    session: Session, source_id: int, listing: Listing, run_id: int, item: NormalizedListing
) -> None:
    session.add(
        RawPayload(
            source_id=source_id,
            listing_id=listing.id,
            parser_run_id=run_id,
            external_id=item.external_id,
            payload_hash=item.payload_hash,
            payload=item.safe_payload,
            observed_at=utcnow(),
        )
    )


def _apply_listing(
    session: Session,
    source: Source,
    run: ParserRun,
    item: NormalizedListing,
) -> tuple[str, Listing]:
    listing = session.scalar(
        select(Listing).where(Listing.source_id == source.id, Listing.external_id == item.external_id)
    )

    resolution = resolve_normalized_listing(session, item)
    brand = resolution.brand
    car_model = resolution.car_model
    generation = resolution.generation

    if listing is None:
        listing = Listing(
            source_id=source.id,
            external_id=item.external_id,
            brand_id=brand.id if brand else None,
            car_model_id=car_model.id if car_model else None,
            generation_id=generation.id if generation else None,
            is_active=item.status == "active",
            first_seen_at=utcnow(),
            last_seen_at=utcnow(),
            payload_hash=item.payload_hash,
        )
        session.add(listing)
        session.flush()
        _set_current_fields(listing, item)
        _append_price_history(session, listing, item, "initial")
        _append_status_history(session, listing, "new", None, item.status)
        _save_raw_payload(session, source.id, listing, run.id, item)
        return "new", listing

    price_changed = (
        listing.current_price_amount != item.price_amount
        or listing.current_price_currency != item.price_currency
        or listing.current_price_usd != item.price_usd
    )
    was_active = listing.is_active
    changed = listing.payload_hash != item.payload_hash

    listing.brand_id = brand.id if brand else None
    listing.car_model_id = car_model.id if car_model else None
    listing.generation_id = generation.id if generation else None
    old_status = listing.status
    _set_current_fields(listing, item)
    listing.is_active = item.status == "active"
    listing.deactivated_at = None if listing.is_active else listing.deactivated_at

    if price_changed:
        _append_price_history(session, listing, item, "price_changed")
        _append_status_history(session, listing, "price_changed", old_status, item.status)
        _save_raw_payload(session, source.id, listing, run.id, item)
        return "price_changed", listing

    if not was_active and listing.is_active:
        _append_status_history(session, listing, "reactivated", old_status, item.status)
        _save_raw_payload(session, source.id, listing, run.id, item)
        return "reactivated", listing

    if changed:
        _append_status_history(session, listing, "updated", old_status, item.status)
        _save_raw_payload(session, source.id, listing, run.id, item)
        return "updated", listing

    return "unchanged", listing


def _deactivate_missing(
    session: Session,
    source: Source,
    target: SyncTarget,
    seen_external_ids: set[str],
) -> int:
    brand = session.scalar(select(Brand).where(Brand.name == target.brand_filter))
    if brand is None:
        return 0

    query = select(Listing).where(
        Listing.source_id == source.id,
        Listing.brand_id == brand.id,
        Listing.is_active.is_(True),
    )
    # Model-specific full scans should only affect that model.
    if target.model_filter:
        from app.models import CarModel

        car_model = session.scalar(
            select(CarModel).where(CarModel.brand_id == brand.id, CarModel.name == target.model_filter)
        )
        if car_model is not None:
            query = query.where(Listing.car_model_id == car_model.id)

    missing = session.scalars(query).all()
    deactivated = 0
    settings = get_settings()
    for listing in missing:
        if listing.external_id in seen_external_ids:
            continue
        listing.missing_full_scans += 1
        if listing.missing_full_scans >= settings.missing_full_scans_before_deactivation:
            listing.is_active = False
            listing.status = "inactive"
            listing.deactivated_at = utcnow()
            _append_status_history(session, listing, "deactivated", "active", "inactive")
            deactivated += 1
    return deactivated


def run_sync(
    session: Session,
    parser: ParserAdapter,
    source: Source,
    target: SyncTarget,
    *,
    mode: str,
    period_override: str | None = None,
    page_size_override: int | None = None,
    max_pages_override: int | None = None,
    max_items_override: int | None = None,
    start_page_override: int | None = None,
) -> RunSummary:
    if mode not in {"fresh", "full"}:
        raise ValueError("mode must be fresh or full")

    run = ParserRun(source_id=source.id, target_id=target.id, mode=mode, status="running")
    session.add(run)
    session.commit()
    session.refresh(run)

    created_period = period_override or target.fresh_period if mode == "fresh" else None
    page_size = page_size_override or target.page_size
    max_pages = max_pages_override or target.max_pages
    max_items = max_items_override or target.max_items
    start_page = start_page_override or 1
    if start_page < 1:
        raise ValueError("start_page must be greater than or equal to 1")
    seen_external_ids: set[str] = set()
    complete_scan = False

    try:
        for page in range(start_page, start_page + max_pages):
            fetched = parser.fetch_listings(
                brand=target.brand_filter,
                model=target.model_filter,
                created_period=created_period,
                page=page,
                size=page_size,
                sort_by="created_at",
                order="desc",
            )
            logger.info(
                "run=%s target=%s page=%s received=%s api_total=%s",
                run.id,
                target.brand_filter,
                page,
                len(fetched.items),
                fetched.total,
            )
            if not fetched.items:
                complete_scan = True
                break

            for raw in fetched.items:
                if run.processed_count >= max_items:
                    break
                try:
                    item = normalize_mashina_listing(raw, fallback_make=target.brand_filter)
                    if not item.make:
                        run.skipped_count += 1
                        logger.warning(
                            "run=%s skipped listing id=%s: make was not detected; title=%r",
                            run.id,
                            raw.get("id"),
                            raw.get("title"),
                        )
                        continue
                    event, _ = _apply_listing(session, source, run, item)
                    seen_external_ids.add(item.external_id)
                    run.processed_count += 1
                    if event == "new":
                        run.new_count += 1
                    elif event == "updated":
                        run.updated_count += 1
                    elif event == "price_changed":
                        run.price_changed_count += 1
                    elif event == "reactivated":
                        run.reactivated_count += 1
                except Exception:
                    run.error_count += 1
                    logger.exception(
                        "run=%s failed to process listing id=%s title=%r",
                        run.id,
                        raw.get("id"),
                        raw.get("title"),
                    )

            session.commit()
            logger.info(
                "run=%s page=%s progress processed=%s new=%s updated=%s price_changed=%s skipped=%s errors=%s",
                run.id,
                page,
                run.processed_count,
                run.new_count,
                run.updated_count,
                run.price_changed_count,
                run.skipped_count,
                run.error_count,
            )
            if run.processed_count >= max_items:
                break
            if len(fetched.items) < page_size:
                complete_scan = True
                break

        if mode == "full" and complete_scan:
            run.deactivated_count = _deactivate_missing(session, source, target, seen_external_ids)
            target.last_full_scan_at = utcnow()

        target.last_success_at = utcnow()
        run.status = "completed"
        run.finished_at = utcnow()
        session.commit()
    except Exception as exc:
        session.rollback()
        run.status = "failed"
        run.error_message = str(exc)[:1000]
        run.finished_at = utcnow()
        session.add(run)
        session.commit()
        raise

    return RunSummary(
        run_id=run.id,
        processed=run.processed_count,
        new=run.new_count,
        updated=run.updated_count,
        price_changed=run.price_changed_count,
        reactivated=run.reactivated_count,
        deactivated=run.deactivated_count,
        skipped=run.skipped_count,
        complete_scan=complete_scan,
    )


def iter_due_targets(session: Session) -> Iterable[SyncTarget]:
    now = utcnow()
    targets = session.scalars(
        select(SyncTarget).where(SyncTarget.enabled.is_(True)).order_by(SyncTarget.priority.asc())
    ).all()
    for target in targets:
        if target.last_success_at is None:
            yield target
            continue
        age_seconds = (now - target.last_success_at).total_seconds()
        if age_seconds >= target.sync_interval_minutes * 60:
            yield target
