"""Планировщик фоновых задач.

Роли после появления telegram-бота (бот сам делает fresh-циклы каждые 20 минут):

1. Полная сверка каталога по выходным: первая половина марок в субботу,
   вторая — в воскресенье, не раньше FULL_SCAN_HOUR (2:00 локального времени).
   Пауза между страницами — REQUEST_DELAY_SECONDS (внутри парсера),
   пауза между марками — FULL_SCAN_BRAND_PAUSE_SECONDS.
2. Пересчет daily_market_stats каждые STATS_REBUILD_INTERVAL_HOURS часов,
   сетка начинается с STATS_REBUILD_START_HOUR (10:00, 18:00, 02:00).

Повторный запуск full scan в тот же день исключается по parser_runs,
поэтому рестарт worker-а безопасен.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal, init_db
from app.models import ParserRun
from app.parsers.mashina import MashinaParser
from app.services.catalog_service import seed_default_catalog
from app.services.lookup import ensure_brand_targets, get_or_create_source
from app.services.schedule import local_now, last_slot_start, split_brands_for_weekend
from app.services.stats import rebuild_daily_market_stats
from app.services.sync import run_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SATURDAY, SUNDAY = 5, 6


def _full_scan_ran_today(session, target_id: int, local_midnight_utc: datetime) -> bool:
    run_id = session.scalar(
        select(ParserRun.id)
        .where(
            ParserRun.target_id == target_id,
            ParserRun.mode == "full",
            ParserRun.started_at >= local_midnight_utc,
            ParserRun.status.in_(("running", "completed")),
        )
        .limit(1)
    )
    return run_id is not None


def run_weekend_full_scans() -> int:
    """Полная сверка дневной половины марок; возвращает число выполненных сканов."""
    settings = get_settings()
    now = local_now(settings.scheduler_timezone)
    if now.weekday() not in (SATURDAY, SUNDAY) or now.hour < settings.full_scan_hour:
        return 0

    saturday_brands, sunday_brands = split_brands_for_weekend(settings.allowed_brand_list)
    brands = saturday_brands if now.weekday() == SATURDAY else sunday_brands
    if not brands:
        return 0

    local_midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc
    )
    completed = 0
    with SessionLocal() as session:
        source = get_or_create_source(session, "mashina.kg", settings.mashina_api_url)
        session.commit()
        targets = ensure_brand_targets(session, source, brands)

        parser = MashinaParser()
        try:
            for target in targets:
                if _full_scan_ran_today(session, target.id, local_midnight_utc):
                    continue
                if completed:
                    time.sleep(settings.full_scan_brand_pause_seconds)
                try:
                    summary = run_sync(
                        session,
                        parser,
                        source,
                        target,
                        mode="full",
                        page_size_override=settings.full_scan_page_size,
                        max_pages_override=settings.full_scan_max_pages,
                        max_items_override=settings.full_scan_max_items,
                    )
                    logger.info("full scan %s: %s", target.brand_filter, summary)
                except Exception:
                    session.rollback()
                    logger.exception("full scan failed for %s", target.brand_filter)
                completed += 1
        finally:
            parser.close()
    return completed


def rebuild_stats_if_due(last_rebuild_at: datetime | None) -> datetime | None:
    """Пересчитывает статистику, если начался новый слот; возвращает время пересчета."""
    settings = get_settings()
    now = local_now(settings.scheduler_timezone)
    slot_start = last_slot_start(
        now, settings.stats_rebuild_start_hour, settings.stats_rebuild_interval_hours
    )
    if last_rebuild_at is not None and last_rebuild_at >= slot_start:
        return None
    with SessionLocal() as session:
        rows = rebuild_daily_market_stats(session)
        session.commit()
    logger.info("daily market stats rebuilt: %s rows (slot %s)", rows, slot_start.isoformat())
    return now


def main() -> None:
    settings = get_settings()
    init_db()
    with SessionLocal() as session:
        get_or_create_source(session, "mashina.kg", settings.mashina_api_url)
        seed_default_catalog(session)
        session.commit()

    logger.info(
        "worker started: weekend full scans at %02d:00, stats rebuild every %sh from %02d:00 (%s)",
        settings.full_scan_hour,
        settings.stats_rebuild_interval_hours,
        settings.stats_rebuild_start_hour,
        settings.scheduler_timezone,
    )
    last_rebuild_at: datetime | None = None
    while True:
        try:
            scans = run_weekend_full_scans()
            if scans:
                logger.info("weekend full scans completed: %s", scans)
        except Exception:
            logger.exception("weekend full scan cycle failed")
        try:
            rebuilt_at = rebuild_stats_if_due(last_rebuild_at)
            if rebuilt_at is not None:
                last_rebuild_at = rebuilt_at
        except Exception:
            logger.exception("stats rebuild failed")
        time.sleep(60)


if __name__ == "__main__":
    main()
