"""Фоновый цикл бота: легкий fresh-парс + подбор уведомлений.

run_cycle — полностью синхронный (вызывается через asyncio.to_thread),
polling_loop — asyncio-обертка с расписанием и отправкой. Циклы идут каждые
BOT_FRESH_INTERVAL_MINUTES, но только в окне активности
[BOT_ACTIVE_START_HOUR, BOT_ACTIVE_END_HOUR) локального времени.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from app.bot.db import mark_user_blocked
from app.bot.matching import PendingNotification, match_new_listings
from app.bot.notifier import BotLike, send_notifications
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.parsers.mashina import MashinaParser
from app.services.lookup import ensure_brand_targets, get_or_create_source
from app.services.schedule import local_now, seconds_until_window
from app.services.sync import run_sync, utcnow

logger = logging.getLogger(__name__)


def run_cycle() -> list[PendingNotification]:
    """Один цикл: 1 страница x BOT_FRESH_PAGE_SIZE свежих объявлений на марку,
    затем матчинг. Страница маленькая (20), а циклы частые — нагрузка на сайт
    ниже, чем редкие тяжелые страницы.

    Пауза между запросами марок обеспечивается парсером: он спит
    REQUEST_DELAY_SECONDS после каждого HTTP-запроса.
    """
    settings = get_settings()
    with SessionLocal() as session:
        source = get_or_create_source(session, "mashina.kg", settings.mashina_api_url)
        session.commit()
        targets = ensure_brand_targets(session, source, settings.allowed_brand_list)

        parser = MashinaParser()
        try:
            for target in targets:
                try:
                    summary = run_sync(
                        session,
                        parser,
                        source,
                        target,
                        mode="fresh",
                        period_override=settings.bot_fresh_period,
                        page_size_override=settings.bot_fresh_page_size,
                        max_pages_override=1,
                        max_items_override=settings.bot_fresh_page_size,
                        update_target_bookkeeping=False,
                    )
                    logger.info(
                        "bot fresh sync %s: new=%s processed=%s",
                        target.brand_filter,
                        summary.new,
                        summary.processed,
                    )
                except Exception:
                    session.rollback()
                    logger.exception("bot fresh sync failed for %s", target.brand_filter)
        finally:
            parser.close()

        since = utcnow() - timedelta(minutes=settings.bot_new_lookback_minutes)
        return match_new_listings(
            session,
            since,
            factor=settings.bot_below_avg_factor,
            min_samples=settings.bot_below_avg_min_samples,
        )


async def _on_blocked(chat_id: int) -> None:
    await asyncio.to_thread(mark_user_blocked, chat_id)


async def polling_loop(bot: BotLike) -> None:
    settings = get_settings()
    interval_seconds = settings.bot_fresh_interval_minutes * 60
    logger.info(
        "bot poller started: every %s min within %02d:00-%02d:00 (%s)",
        settings.bot_fresh_interval_minutes,
        settings.bot_active_start_hour,
        settings.bot_active_end_hour,
        settings.scheduler_timezone,
    )
    while True:
        wait_seconds = seconds_until_window(
            local_now(settings.scheduler_timezone),
            settings.bot_active_start_hour,
            settings.bot_active_end_hour,
        )
        if wait_seconds > 0:
            logger.info("outside active window, sleeping %.0f min", wait_seconds / 60)
            await asyncio.sleep(wait_seconds)

        try:
            pending = await asyncio.to_thread(run_cycle)
            if pending:
                sent = await send_notifications(bot, pending, on_blocked=_on_blocked)
                logger.info("notifications: sent %s of %s", sent, len(pending))
            else:
                logger.info("cycle complete, no matching new listings")
        except Exception:
            logger.exception("bot poll cycle failed")
        await asyncio.sleep(interval_seconds)
