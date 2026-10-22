from __future__ import annotations

from dataclasses import dataclass
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

import httpx
from sqlalchemy import func, select
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
from app.services.images import extract_image_url
from app.services.normalizer import NormalizedListing

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
    # Фото без него в ответе (лента lalafo, упавшая карточка) не затирает
    # известное: пропажа ссылки из ответа — не пропажа фото у объявления.
    if item.image_url:
        listing.image_url = item.image_url
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

    # У конвертированного по курсу USD (usd_converted) колебание — это смена
    # курса НБКР, а не цены продавцом: сравниваем только оригинальную цену.
    usd_is_derived = "usd_converted" in item.quality_flags
    price_changed = (
        listing.current_price_amount != item.price_amount
        or listing.current_price_currency != item.price_currency
        or (not usd_is_derived and listing.current_price_usd != item.price_usd)
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


def deactivate_stale_listings(
    session: Session,
    *,
    stale_days: int,
    source_names: Iterable[str] = ("lalafo.kg",),
    not_seen_since: datetime | None = None,
) -> int:
    """Деактивация «зависших»: у fresh-only источников (lalafo) нет full-scan,
    проданное авто просто исчезает из ленты и осталось бы is_active навсегда.

    Семантика — last_seen_at (НЕ first_seen_at): активно висящее в ленте
    объявление каждый цикл обновляет last_seen_at и продолжает жить. История
    цен не трогается — аналитика прошлых дней сохраняется. Scope по умолчанию —
    только lalafo: у mashina своя деактивация через weekend full-scan.

    `not_seen_since` дополнительно сужает порог: гасим только объявления с
    last_seen_at раньше этой отметки. Worker передаёт сюда границу, до которой
    в этом прогоне дошёл валидатор карточек, — фолбэк тогда добивает лишь то,
    что валидатор уже ПЫТАЛСЯ проверить и не смог, а не всё, до чего у него
    не хватило потолка (см. ValidationReport.reached_last_seen_at).
    Коммит — на вызывающей стороне.
    """
    cutoff = utcnow() - timedelta(days=stale_days)
    if not_seen_since is not None:
        if not_seen_since.tzinfo is None:  # SQLite в тестах теряет tz
            not_seen_since = not_seen_since.replace(tzinfo=timezone.utc)
        cutoff = min(cutoff, not_seen_since)
    stale = session.scalars(
        select(Listing).where(
            Listing.source_id.in_(select(Source.id).where(Source.name.in_(tuple(source_names)))),
            Listing.is_active.is_(True),
            Listing.last_seen_at < cutoff,
        )
    ).all()
    for listing in stale:
        old_status = listing.status
        listing.is_active = False
        listing.status = "inactive"
        listing.deactivated_at = utcnow()
        _append_status_history(
            session, listing, "deactivated", old_status, "inactive", details={"reason": "stale"}
        )
    return len(stale)


def count_unseen_active_listings(
    session: Session,
    *,
    days: int,
    source_names: Iterable[str] = ("lalafo.kg",),
) -> int:
    """Сколько активных объявлений источника не видели дольше `days` дней —
    метрика «валидатор не успевает» для warning-а в worker."""
    cutoff = utcnow() - timedelta(days=days)
    return session.scalar(
        select(func.count())
        .select_from(Listing)
        .where(
            Listing.source_id.in_(select(Source.id).where(Source.name.in_(tuple(source_names)))),
            Listing.is_active.is_(True),
            Listing.last_seen_at < cutoff,
        )
    ) or 0


# status_id карточки lalafo (API feed/details отвечает 200 и на мёртвые):
#   2  — живое объявление (публичная страница 200);
#   8  — снято/архив: публичная страница того же объявления отвечает 410 Gone
#        (проверено 16.09.2026 на 13 карточках; на проде это ~74% «живых» по 200);
#   3  — заблокировано модерацией (спам «внимание…»), страница тоже 410;
#   11 — страница 200, семантика не выяснена (единичные случаи) — считаем живым.
# Незнакомые значения НЕ деактивируем (консервативно), но логируем.
LALAFO_STATUS_ACTIVE = 2
LALAFO_DEAD_STATUS_IDS = frozenset({3, 8})


@dataclass
class ValidationReport:
    """Итог одного прогона валидатора карточек lalafo."""

    checked: int = 0  # карточка ответила (200 или 410) — вердикт вынесен
    deactivated: int = 0  # из checked: 410 Gone или мёртвый status_id
    failed: int = 0  # сеть/прокси/не-410 ошибка — вердикта нет, last_seen не трогали
    candidates: int = 0  # сколько кандидатов взяли в прогон (≤ max_per_run)
    drained: bool = True  # False = кандидатов было больше потолка
    aborted: bool = False  # прервано по серии ошибок (источник/прокси недоступен)
    # last_seen_at последнего взятого кандидата (до продления): всё старше уже
    # ПЫТАЛИСЬ проверить в этом прогоне. Граница для фолбэка «зависших».
    reached_last_seen_at: datetime | None = None

    @property
    def fallback_allowed(self) -> bool:
        """Можно ли после прогона доверять фолбэку «30 дней не видели → мёртвое».
        Нельзя, если прогон прерван или большинство попыток провалилось: тогда
        «не видели» означает «не смогли посмотреть», а не «пропало»."""
        if self.aborted:
            return False
        return self.failed <= self.checked


def validate_stale_lalafo_listings(
    session: Session,
    parser: ParserAdapter,
    source: Source,
    *,
    validate_after_days: int,
    max_per_run: int,
    max_consecutive_failures: int = 5,
    commit_every: int | None = None,
) -> ValidationReport:
    """Точечный «валидатор» давно не виденных lalafo-объявлений через карточку.

    Лента даёт только новейшие 50 на марку, поэтому живое объявление со
    временем навсегда уходит из нашего поля зрения. Карточка отвечает
    HTTP 200 живым (продлеваем last_seen_at — объявление остаётся в статистике
    и не попадёт под 30-дневный фолбэк) и HTTP 410 Gone удалённым/проданным
    (честная деактивация раньше фолбэка). Отдельно мёртвые по `status_id`
    (LALAFO_DEAD_STATUS_IDS): API отдаёт 200, но сайт такое объявление уже не
    показывает — деактивируем с reason='card_inactive'.

    Кандидаты — от самых давних. `max_consecutive_failures` подряд (прокси лёг,
    lalafo недоступен) — прогон прерывается: иначе 5 ретраев × N карточек
    держат worker часами (14.09–16.09.2026 — по ~10 ч за ночь) и блокируют
    остальные его задачи. `commit_every` — периодический commit внутри прогона,
    чтобы транзакция не висела открытой все ~40 минут (прогон в 3000 карточек).
    worker передаёт 1: тогда транзакция не переживает ни одного HTTP-запроса —
    иначе она часами держит строки `listings`, и DDL при выкатке встаёт в
    очередь за ней, а все запросы к таблице — за DDL (21.10.2026).
    """
    cutoff = utcnow() - timedelta(days=validate_after_days)
    candidates = session.scalars(
        select(Listing)
        .where(
            Listing.source_id == source.id,
            Listing.is_active.is_(True),
            Listing.last_seen_at < cutoff,
        )
        .order_by(Listing.last_seen_at.asc())
        .limit(max_per_run)
    ).all()

    if commit_every:
        session.commit()  # выборку прочитали — не держим её транзакцию через HTTP
    report = ValidationReport(candidates=len(candidates), drained=len(candidates) < max_per_run)
    consecutive_failures = 0
    for index, listing in enumerate(candidates, start=1):
        report.reached_last_seen_at = listing.last_seen_at
        try:
            detail = parser.fetch_details(listing.external_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 410:
                logger.warning(
                    "lalafo validator: id=%s unexpected status=%s; keeping active",
                    listing.external_id,
                    exc.response.status_code,
                )
                report.failed += 1
                consecutive_failures += 1
            else:
                report.checked += 1
                consecutive_failures = 0
                _deactivate_by_card(session, listing, reason="card_gone")
                report.deactivated += 1
        except Exception:
            logger.exception("lalafo validator: detail fetch failed for id=%s", listing.external_id)
            report.failed += 1
            consecutive_failures += 1
        else:
            report.checked += 1
            consecutive_failures = 0
            status_id = detail.get("status_id")
            if status_id in LALAFO_DEAD_STATUS_IDS:
                _deactivate_by_card(session, listing, reason="card_inactive", status_id=status_id)
                report.deactivated += 1
            else:
                if status_id is not None and status_id != LALAFO_STATUS_ACTIVE:
                    logger.warning(
                        "lalafo validator: id=%s alive card with unexpected status_id=%s; keeping active",
                        listing.external_id,
                        status_id,
                    )
                listing.last_seen_at = utcnow()
                if listing.image_url is None:
                    listing.image_url = extract_image_url(detail)

        if commit_every and index % commit_every == 0:
            session.commit()
        if consecutive_failures >= max_consecutive_failures:
            logger.error(
                "lalafo validator: %s consecutive failures — source or proxy is down, aborting run "
                "(checked=%s of %s candidates)",
                consecutive_failures,
                report.checked,
                report.candidates,
            )
            report.aborted = True
            break
    return report


def _deactivate_by_card(
    session: Session, listing: Listing, *, reason: str, status_id: int | None = None
) -> None:
    old_status = listing.status
    listing.is_active = False
    listing.status = "inactive"
    listing.deactivated_at = utcnow()
    details: dict = {"reason": reason}
    if status_id is not None:
        details["status_id"] = status_id
    _append_status_history(session, listing, "deactivated", old_status, "inactive", details=details)


def _enrich_or_skip(
    session: Session,
    parser: ParserAdapter,
    source: Source,
    raw: dict,
) -> dict | None:
    """Для источников с неполной лентой (lalafo): карточку дотягиваем только
    для новых объявлений и при смене цены. Уже известное объявление с той же
    ценой не перезаписываем неполными данными ленты — только отмечаем как
    увиденное. None => пропустить элемент."""
    external_id = str(raw.get("id"))
    listing = session.scalar(
        select(Listing).where(Listing.source_id == source.id, Listing.external_id == external_id)
    )
    if listing is not None:
        feed_price = raw.get("price")
        same_price = (
            (feed_price is None and listing.current_price_amount is None)
            or (
                feed_price is not None
                and listing.current_price_amount is not None
                and listing.current_price_amount == feed_price
                and listing.current_price_currency == raw.get("currency")
            )
        )
        if same_price:
            listing.last_seen_at = utcnow()
            # Карточку не тянем, но фото есть и в ленте: так объявления, увиденные
            # до появления колонки image_url, получают его без лишних запросов.
            if listing.image_url is None:
                listing.image_url = extract_image_url(raw)
            return None
    try:
        return parser.fetch_details(raw["id"])
    except Exception:
        logger.exception(
            "detail fetch failed for %s id=%s; falling back to feed data", parser.source_name, raw.get("id")
        )
        return raw


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
    update_target_bookkeeping: bool = True,
    catchup_max_pages: int = 0,
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

    # Catch-up (только fresh): полностью новая страница означает пропуск между
    # циклами (простой) — разрешаем дочитать до catchup_max_pages сверх лимита.
    last_allowed_page = start_page + max_pages - 1
    hard_last_page = last_allowed_page + (catchup_max_pages if mode == "fresh" else 0)

    try:
        page = start_page
        while page <= last_allowed_page:
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

            # Событие самого старого обработанного объявления страницы: если даже
            # оно new — за краем страницы могли остаться невиданные (пропуск).
            # Сравнивать "всю страницу" с page_size нельзя: lalafo добавляет
            # сверху VIP-блок (54 при per-page=50), и VIP почти всегда известны.
            tail_event: str | None = None
            for raw in fetched.items:
                if run.processed_count >= max_items:
                    break
                try:
                    if parser.needs_detail_enrichment:
                        raw = _enrich_or_skip(session, parser, source, raw)
                        if raw is None:
                            tail_event = "known_skip"
                            continue
                    item = parser.normalize(raw, fallback_make=target.brand_filter)
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
                    tail_event = event
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
            page_gap_suspected = len(fetched.items) >= page_size and tail_event == "new"
            if page_gap_suspected and mode == "fresh":
                logger.warning(
                    "run=%s target=%s page=%s: oldest item on a full page is new; "
                    "possible gap between cycles",
                    run.id,
                    target.brand_filter,
                    page,
                )
                if page == last_allowed_page and last_allowed_page < hard_last_page:
                    last_allowed_page += 1
                    max_items += page_size
                    logger.warning(
                        "run=%s target=%s catch-up: extending to page %s", run.id, target.brand_filter, page + 1
                    )
            if run.processed_count >= max_items:
                break
            if len(fetched.items) < page_size:
                complete_scan = True
                break
            page += 1

        # Deactivation is only safe after a full scan that started at page 1:
        # a resumed partial scan (start_page > 1) never sees the whole catalog,
        # so its seen-set would wrongly mark earlier pages as missing.
        if mode == "full" and complete_scan and start_page == 1:
            run.deactivated_count = _deactivate_missing(session, source, target, seen_external_ids)
            if update_target_bookkeeping:
                target.last_full_scan_at = utcnow()

        # Bot-side light runs pass update_target_bookkeeping=False so they do not
        # interfere with the worker's iter_due_targets scheduling.
        if update_target_bookkeeping:
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
