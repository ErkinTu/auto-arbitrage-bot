"""Планировщик фоновых задач.

Роли после появления telegram-бота (бот сам делает fresh-циклы каждые 10 минут):

1. Полная сверка каталога по выходным: первая половина марок в субботу,
   вторая — в воскресенье, не раньше FULL_SCAN_HOUR (2:00 локального времени).
   Пауза между страницами — REQUEST_DELAY_SECONDS (внутри парсера),
   пауза между марками — FULL_SCAN_BRAND_PAUSE_SECONDS.
2. Пересчет daily_market_stats (только за сегодня) каждый час, но лишь в окне
   активности бота [BOT_ACTIVE_START_HOUR, BOT_ACTIVE_END_HOUR): ночью нет
   свежих данных (fresh-парс не идёт) и не работает matching, а market_average
   при отсутствии сегодняшних строк падает на последнюю дату.
3. Раз в локальные сутки — обслуживание lalafo (у него нет full-scan):
   сначала точечный валидатор давно не виденных объявлений через карточку
   (HTTP 410 -> деактивация, 200 -> продление last_seen_at), затем фолбэк —
   деактивация так и не подтверждённых дольше STALE_LISTING_DAYS.

Повторный запуск full scan в тот же день исключается по parser_runs,
поэтому рестарт worker-а безопасен.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal, init_db
from app.bot.messages import trial_paywall_text
from app.models import BotUser, ParserRun, Payment
from app.services.events import EVENT_TRIAL_EXPIRED, log_event
from app.services.telegram import send_message
from app.services.trial import paywall_candidates
from app.parsers.mashina import MashinaParser
from app.services.catalog_service import seed_default_catalog
from app.services.lookup import ensure_brand_targets, get_or_create_source
from app.services.schedule import local_now, last_slot_start, split_brands_for_weekend
from app.services.sources import LALAFO, build_parser, source_base_url
from app.services.stats import rebuild_daily_market_stats
from app.services.sync import (
    deactivate_stale_listings,
    run_sync,
    utcnow,
    validate_stale_lalafo_listings,
)

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


def fail_orphaned_full_runs(session) -> int:
    """Помечает осиротевшие full-сканы failed, чтобы прерванный рестартом скан
    мог перезапуститься в тот же день (_full_scan_ran_today считает running).

    Только mode='full': fresh-запуски принадлежат боту и могут идти прямо
    сейчас. Полные сканы делает только этот worker, а его прошлый процесс
    мёртв — раз мы стартуем, его 'running' заведомо осиротели. Редкий ручной
    `cli sync` full, идущий параллельно, попадёт под раздачу — приемлемо:
    его ParserRun станет failed, но сам процесс продолжит работать.
    """
    orphaned = session.scalars(
        select(ParserRun).where(ParserRun.mode == "full", ParserRun.status == "running")
    ).all()
    for run in orphaned:
        run.status = "failed"
        run.error_message = "orphaned: worker restarted mid-run"
        run.finished_at = utcnow()
    return len(orphaned)


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
    # Только в окно активности бота: ночью новых данных нет и matching не идёт,
    # пересчитывать нечего (market_average берёт последнюю доступную дату).
    if not (settings.bot_active_start_hour <= now.hour < settings.bot_active_end_hour):
        return None
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


def lalafo_maintenance_if_due(last_run_date: date | None) -> date | None:
    """Раз в локальные сутки: валидатор карточек давно не виденных
    lalafo-объявлений, затем фолбэк-деактивация «зависших». Возвращает дату
    запуска. Идемпотентна — рестарт worker-а в тот же день просто не найдет
    новых кандидатов."""
    settings = get_settings()
    today = local_now(settings.scheduler_timezone).date()
    if last_run_date == today:
        return None
    with SessionLocal() as session:
        source = get_or_create_source(session, LALAFO, source_base_url(LALAFO))
        session.commit()
        parser = build_parser(LALAFO, session)
        try:
            checked, gone = validate_stale_lalafo_listings(
                session,
                parser,
                source,
                validate_after_days=settings.lalafo_validate_after_days,
                max_per_run=settings.lalafo_validate_max_per_run,
            )
            session.commit()
        finally:
            parser.close()
        stale = deactivate_stale_listings(session, stale_days=settings.stale_listing_days)
        session.commit()
    logger.info(
        "lalafo maintenance: validated %s cards (deactivated %s gone), stale fallback deactivated %s (>%sd)",
        checked,
        gone,
        stale,
        settings.stale_listing_days,
    )
    return today


def _reminder_text(expires_at: datetime) -> str:
    return (
        f"⏳ Ваша подписка истекает {expires_at.strftime('%d.%m.%Y')}.\n"
        "Продлите её командой /subscribe, чтобы не пропускать выгодные объявления."
    )


def send_renewal_reminders_if_due(last_run_date: date | None) -> date | None:
    """Раз в локальные сутки: напоминает подписчикам об истечении.

    Кандидаты — активные подписчики, чей срок истекает в ближайшие
    SUBSCRIPTION_REMINDER_DAYS_BEFORE дней и кому ещё не напоминали за текущий
    период (renewal_reminded_at сбрасывается в NULL при оплате). Спит, если
    подписка не настроена. Возвращает дату запуска (идемпотентна за день).
    """
    settings = get_settings()
    if not settings.subscription_enabled:
        return None
    today = local_now(settings.scheduler_timezone).date()
    if last_run_date == today:
        return None

    now = utcnow()
    threshold = now + timedelta(days=settings.subscription_reminder_days_before)
    reminded = 0
    with SessionLocal() as session:
        users = session.scalars(
            select(BotUser).where(
                BotUser.access_status == "subscriber",
                BotUser.subscription_expires_at.is_not(None),
                BotUser.subscription_expires_at > now,
                BotUser.subscription_expires_at <= threshold,
                BotUser.renewal_reminded_at.is_(None),
            )
        ).all()
        for user in users:
            if send_message(user.telegram_id, _reminder_text(user.subscription_expires_at)):
                user.renewal_reminded_at = now
                reminded += 1
        session.commit()
    if reminded:
        logger.info("subscription renewal reminders sent: %s", reminded)
    return today


def send_trial_paywall_notices() -> int:
    """Пейвол: разовое сообщение тем, у кого закончился бесплатный период.

    Сами уведомления об объявлениях прекращаются автоматически (matching.py
    выбирает только активный доступ) — здесь только объясняем человеку, что
    произошло, и зовём оплатить. Строго один раз на пользователя:
    `trial_expired_notified_at` выставляется даже при неудачной отправке
    (заблокировал бота) — иначе бот пытался бы каждый цикл.

    Не «раз в сутки», а каждый цикл worker-а: у каждого юзера свой день
    окончания триала, и день ожидания сообщения — это потерянная конверсия.
    Кандидатов почти всегда ноль, запрос дешёвый.
    """
    settings = get_settings()
    if not settings.trial_enabled:
        return 0
    price = int(settings.subscription_price_kgs)
    text = trial_paywall_text(price, settings.subscription_period_days)
    now = utcnow()
    sent = 0
    with SessionLocal() as session:
        for user in paywall_candidates(session, now):
            delivered = send_message(user.telegram_id, text)
            user.trial_expired_notified_at = now
            log_event(session, user.id, EVENT_TRIAL_EXPIRED, {"delivered": delivered})
            if user.access_status == "trial":
                # Держим статус честным для метрик, даже если юзер молчит.
                user.access_status = "revoked"
            sent += 1
        session.commit()
    if sent:
        logger.info("trial paywall notices sent: %s", sent)
    return sent


def expire_stale_payments() -> int:
    """Гасит незавершённые платежи: pending старше PAYMENT_PENDING_TTL_HOURS.

    Нужно потому, что Finik присылает webhook ТОЛЬКО при успешной оплате: про
    «человек передумал / ссылка истекла» мы не узнаём никогда, и без этого
    pending копились бы вечно (и врала бы любая воронка по конверсии).
    """
    settings = get_settings()
    deadline = utcnow() - timedelta(hours=settings.payment_pending_ttl_hours)
    with SessionLocal() as session:
        stale = session.scalars(
            select(Payment).where(Payment.status == "pending", Payment.created_at < deadline)
        ).all()
        for payment in stale:
            payment.status = "expired"
        session.commit()
    if stale:
        logger.info("expired %s stale pending payments", len(stale))
    return len(stale)


def main() -> None:
    settings = get_settings()
    init_db()
    with SessionLocal() as session:
        get_or_create_source(session, "mashina.kg", settings.mashina_api_url)
        seed_default_catalog(session)
        orphaned = fail_orphaned_full_runs(session)
        session.commit()
    if orphaned:
        logger.warning("marked %s orphaned full-scan runs as failed", orphaned)

    logger.info(
        "worker started: weekend full scans at %02d:00, stats rebuild every %sh within %02d:00-%02d:00 (%s)",
        settings.full_scan_hour,
        settings.stats_rebuild_interval_hours,
        settings.bot_active_start_hour,
        settings.bot_active_end_hour,
        settings.scheduler_timezone,
    )
    last_rebuild_at: datetime | None = None
    last_maintenance_date: date | None = None
    last_reminder_date: date | None = None
    while True:
        try:
            scans = run_weekend_full_scans()
            if scans:
                logger.info("weekend full scans completed: %s", scans)
        except Exception:
            logger.exception("weekend full scan cycle failed")
        try:
            # До пересчета статистики, чтобы дневные агрегаты не включали зависшие.
            ran_date = lalafo_maintenance_if_due(last_maintenance_date)
            if ran_date is not None:
                last_maintenance_date = ran_date
        except Exception:
            logger.exception("lalafo maintenance failed")
        try:
            rebuilt_at = rebuild_stats_if_due(last_rebuild_at)
            if rebuilt_at is not None:
                last_rebuild_at = rebuilt_at
        except Exception:
            logger.exception("stats rebuild failed")
        try:
            reminded_date = send_renewal_reminders_if_due(last_reminder_date)
            if reminded_date is not None:
                last_reminder_date = reminded_date
        except Exception:
            logger.exception("subscription renewal reminders failed")
        try:
            send_trial_paywall_notices()
        except Exception:
            logger.exception("trial paywall notices failed")
        try:
            expire_stale_payments()
        except Exception:
            logger.exception("expiring stale payments failed")
        time.sleep(60)


if __name__ == "__main__":
    main()
