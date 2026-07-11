"""Фоновый цикл бота: легкий fresh-парс + подбор уведомлений.

run_cycle — полностью синхронный (вызывается через asyncio.to_thread),
polling_loop — asyncio-обертка с расписанием и отправкой.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app.bot.db import mark_user_blocked
from app.bot.matching import PendingNotification, match_new_listings
from app.bot.notifier import BotLike, send_notifications
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import Source, SyncTarget
from app.parsers.mashina import MashinaParser
from app.services.lookup import get_or_create_source
from app.services.sync import run_sync, utcnow

logger = logging.getLogger(__name__)


def _ensure_bot_targets(session, source: Source) -> list[SyncTarget]:
    """Brand-level sync_targets для разрешенных марок (создает отсутствующие).

    Строки общие с worker-ом; бот запускает их с update_target_bookkeeping=False,
    поэтому расписание worker-а не сбивается.
    """
    settings = get_settings()
    targets: list[SyncTarget] = []
    for brand in settings.allowed_brand_list:
        target = session.scalar(
            select(SyncTarget).where(
                SyncTarget.source_id == source.id,
                SyncTarget.brand_filter == brand,
                SyncTarget.model_filter.is_(None),
                SyncTarget.generation_filter.is_(None),
            )
        )
        if target is None:
            target = SyncTarget(
                source_id=source.id,
                brand_filter=brand,
                fresh_period=settings.bot_fresh_period,
                page_size=settings.default_page_size,
                max_pages=settings.default_max_pages,
                max_items=settings.default_max_items,
                sync_interval_minutes=settings.default_sync_interval_minutes,
            )
            session.add(target)
            session.commit()
        targets.append(target)
    return targets


def run_cycle() -> list[PendingNotification]:
    """Один цикл: 1 страница x 100 свежих объявлений на марку, затем матчинг."""
    settings = get_settings()
    with SessionLocal() as session:
        source = get_or_create_source(session, "mashina.kg", settings.mashina_api_url)
        session.commit()
        targets = _ensure_bot_targets(session, source)

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
                        page_size_override=100,
                        max_pages_override=1,
                        max_items_override=100,
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
    logger.info("bot poller started, interval %s min", settings.bot_fresh_interval_minutes)
    while True:
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
