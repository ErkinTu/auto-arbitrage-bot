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
from app.core.logging_setup import setup_logging
from app.db.session import SessionLocal
from app.bot.keyboards import base_filter_kb, play_kb, trial_kb
from app.bot.messages import (
    nudge_filter_text,
    nudge_trial_text,
    pause_reminder_text,
    renewal_reminder_text,
    trial_paywall_text,
    trial_summary_market_text,
    trial_summary_text,
)
from app.bot.showcase import SHOWCASE_PAGE, compute_showcase
from app.models import BotUser, ParserRun, Payment
from app.services.engagement import (
    EVENT_TRIAL_SUMMARY,
    NUDGE_FILTER,
    NUDGE_TRIAL,
    filter_nudge_candidates,
    nudge_event,
    trial_nudge_candidates,
    trial_stats,
    trial_summary_candidates,
)
from app.services.events import EVENT_PAUSE_REMINDER, EVENT_TRIAL_EXPIRED, log_event
from app.services.telegram import SendOutcome, send_message
from app.services.access_schedule import tail_tier
from app.services.plans import TIER_FULL
from app.services.trial import paywall_candidates
from app.parsers.mashina import MashinaParser
from app.services.lookup import ensure_brand_targets, get_or_create_source
from app.services.fuel_labeling import label_mashina_fuel
from app.services.schedule import local_now, last_slot_start, split_brands_for_weekend
from app.services.sources import LALAFO, build_parser, source_base_url
from app.services.stats import rebuild_daily_market_stats
from app.services.sync import (
    count_unseen_active_listings,
    deactivate_stale_listings,
    run_sync,
    utcnow,
    validate_stale_lalafo_listings,
)

setup_logging("%(asctime)s %(levelname)s %(message)s")
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


def mashina_fuel_labeling_if_due(last_run_date: date | None) -> date | None:
    """Раз в локальные сутки, ночью: полная разметка топлива mashina.

    Только между `full_scan_hour` и началом окна бота: прогон ~20 минут, и
    рестарт worker-а днём не должен на столько задерживать напоминания.
    Возвращает дату запуска (или None — не время / уже было сегодня).
    """
    settings = get_settings()
    if not settings.mashina_fuel_full_enabled:
        return None
    now = local_now(settings.scheduler_timezone)
    today = now.date()
    if last_run_date == today:
        return None
    if not settings.full_scan_hour <= now.hour < settings.bot_active_start_hour:
        return None
    run_mashina_fuel_labeling()
    return today


def run_mashina_fuel_labeling():
    """Полная разметка топлива mashina (worker раз в сутки, `cli.py fuel-backfill`).

    Возвращает отчёт: CLI печатает его сам - логгер CLI строку с цифрами не выводит.
    """
    settings = get_settings()
    with SessionLocal() as session:
        source = get_or_create_source(session, "mashina.kg", settings.mashina_api_url)
        session.commit()
        parser = MashinaParser()
        try:
            report = label_mashina_fuel(session, parser, source.id, max_pages=None)
        finally:
            parser.close()
    level = logging.WARNING if report.failed else logging.INFO
    logger.log(
        level,
        "mashina fuel labeling (full): requests=%s seen=%s updated=%s failed=%s",
        report.requests,
        report.seen,
        report.updated,
        report.failed,
    )
    return report


def lalafo_maintenance_if_due(last_run_date: date | None) -> date | None:
    """Раз в локальные сутки: валидатор карточек давно не виденных
    lalafo-объявлений, затем фолбэк-деактивация «зависших». Возвращает дату
    запуска. Идемпотентна — рестарт worker-а в тот же день просто не найдет
    новых кандидатов."""
    settings = get_settings()
    today = local_now(settings.scheduler_timezone).date()
    if last_run_date == today:
        return None
    run_lalafo_maintenance()
    return today


def run_lalafo_maintenance() -> None:
    """Один прогон обслуживания lalafo (worker раз в сутки, CLI — вручную).

    Порядок и предохранители:
    1. Валидатор карточек — от самых давних, не больше `lalafo_validate_max_per_run`.
       Серия ошибок подряд прерывает прогон (прокси/источник лежит).
    2. Фолбэк «не видели 30 дней → неактивно» запускается только если
       валидатору можно верить (`report.fallback_allowed`): при лежащем прокси
       «не видели» значит «не смогли посмотреть». И только до границы, до
       которой валидатор дошёл: непроверенное из-за потолка не гасим вслепую —
       до 16.09.2026 так ежедневно уходило ~1600 объявлений, треть живых.
    3. Если потолка не хватило и за 30-дневным порогом копятся непроверенные —
       warning: поднимать LALAFO_VALIDATE_MAX_PER_RUN.
    """
    settings = get_settings()
    with SessionLocal() as session:
        source = get_or_create_source(session, LALAFO, source_base_url(LALAFO))
        session.commit()
        parser = build_parser(LALAFO, session)
        try:
            report = validate_stale_lalafo_listings(
                session,
                parser,
                source,
                validate_after_days=settings.lalafo_validate_after_days,
                max_per_run=settings.lalafo_validate_max_per_run,
                commit_every=1,
            )
            session.commit()
        finally:
            parser.close()

        stale = 0
        if report.fallback_allowed:
            stale = deactivate_stale_listings(
                session,
                stale_days=settings.stale_listing_days,
                not_seen_since=None if report.drained else report.reached_last_seen_at,
            )
            session.commit()
        else:
            logger.warning(
                "lalafo maintenance: stale fallback skipped — validator unreliable "
                "(aborted=%s, failed=%s, checked=%s)",
                report.aborted,
                report.failed,
                report.checked,
            )
        unseen_past_threshold = count_unseen_active_listings(
            session, days=settings.stale_listing_days
        )
    logger.info(
        "lalafo maintenance: validated %s cards (deactivated %s: gone/inactive, failed %s, "
        "candidates %s, drained=%s, aborted=%s), stale fallback deactivated %s (>%sd)",
        report.checked,
        report.deactivated,
        report.failed,
        report.candidates,
        report.drained,
        report.aborted,
        stale,
        settings.stale_listing_days,
    )
    if unseen_past_threshold:
        logger.warning(
            "lalafo maintenance: %s active listings unseen >%sd remain unchecked — "
            "raise LALAFO_VALIDATE_MAX_PER_RUN (now %s) or the validator will never catch up",
            unseen_past_threshold,
            settings.stale_listing_days,
            settings.lalafo_validate_max_per_run,
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
            # Тариф истекающего хвоста: продлевают конкретную скорость, и человек
            # должен видеть, что именно кончается (и что дешевле есть).
            tier = tail_tier(session, user.id, now) or TIER_FULL
            outcome = send_message(
                user.telegram_id,
                renewal_reminder_text(user.subscription_expires_at, tier),
            )
            # UNDELIVERABLE тоже закрываем флагом: адресата больше нет, и без
            # этого мы бы дёргали заблокировавшего бота каждый день до конца
            # его подписки. Повторяем только то, что реально стоит повторить.
            if outcome is not SendOutcome.FAILED:
                user.renewal_reminded_at = now
            if outcome is SendOutcome.DELIVERED:
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
    `trial_expired_notified_at` выставляется и когда доставить не удалось —
    но только если адресата больше нет (заблокировал, удалился), иначе бот
    пытался бы каждый цикл. А вот на 429/сеть флаг НЕ ставим: сообщение одно
    на всю жизнь юзера, и сжечь его на временной ошибке — значит навсегда
    оставить человека без объяснения, почему пуши прекратились.

    Не «раз в сутки», а каждый цикл worker-а: у каждого юзера свой день
    окончания триала, и день ожидания сообщения — это потерянная конверсия.
    Кандидатов почти всегда ноль, запрос дешёвый.
    """
    settings = get_settings()
    if not settings.trial_enabled:
        return 0
    # Пока оплата закрыта или обкатывается на тестировщиках, зовём не в
    # /subscribe (там «скоро»), а к ручному продлению — см. trial_paywall_text.
    payments_open = settings.subscription_enabled and not settings.subscription_testing
    text = trial_paywall_text(payments_open)
    now = utcnow()
    sent = 0
    deferred = 0
    with SessionLocal() as session:
        for user in paywall_candidates(session, now):
            outcome = send_message(user.telegram_id, text)
            if outcome is SendOutcome.FAILED:
                # Ничего не трогаем: юзер остаётся кандидатом и получит своё
                # сообщение следующим циклом (worker крутится раз в минуту).
                deferred += 1
                continue
            user.trial_expired_notified_at = now
            log_event(
                session,
                user.id,
                EVENT_TRIAL_EXPIRED,
                {"delivered": outcome is SendOutcome.DELIVERED},
            )
            if user.access_status == "trial":
                # Держим статус честным для метрик, даже если юзер молчит.
                user.access_status = "revoked"
            if outcome is SendOutcome.DELIVERED:
                sent += 1
        session.commit()
    if sent or deferred:
        logger.info("trial paywall notices: %s sent, %s deferred", sent, deferred)
    return sent


def _within_bot_window() -> bool:
    """Касания (напоминания, итоги) — только днём, в окне активности бота.

    Ночное сообщение от бота, который ночью и так молчит, будит человека ради
    маркетинга; дождётся утра — ничего не теряет.
    """
    settings = get_settings()
    hour = local_now(settings.scheduler_timezone).hour
    return settings.bot_active_start_hour <= hour < settings.bot_active_end_hour


def _record_touch(session, user: BotUser, event: str, outcome: SendOutcome) -> bool:
    """Журналит касание; True — доставлено.

    FAILED (429/сеть) не журналим — человек останется кандидатом и получит
    сообщение следующим циклом. UNDELIVERABLE (заблокировал бота) журналим,
    иначе дёргали бы его каждую минуту до конца окна.
    """
    if outcome is SendOutcome.FAILED:
        return False
    log_event(session, user.id, event, {"delivered": outcome is SendOutcome.DELIVERED})
    return outcome is SendOutcome.DELIVERED


def send_nudges() -> int:
    """Напоминания «включите триал» и «поставьте фильтр»: по два, через 12 мин и сутки.

    Кто нажал /start и не включил триал — кнопка «триал + базовый фильтр».
    Кто на триале без фильтра — кнопка «поставить базовый фильтр». Кандидаты и
    окна — `services/engagement.py`. Каждый цикл worker-а, как и пейвол: у
    каждого своё время, а запрос почти всегда пустой.
    """
    settings = get_settings()
    if not settings.trial_enabled or not _within_bot_window():
        return 0
    now = utcnow()
    sent = 0
    trial_markup = trial_kb(settings.trial_days).model_dump(exclude_none=True)
    filter_markup = base_filter_kb().model_dump(exclude_none=True)
    with SessionLocal() as session:
        for step in (1, 2):
            for user in trial_nudge_candidates(session, step, now):
                outcome = send_message(
                    user.telegram_id,
                    nudge_trial_text(settings.trial_days, step),
                    reply_markup=trial_markup,
                )
                sent += _record_touch(session, user, nudge_event(NUDGE_TRIAL, step), outcome)
            for user in filter_nudge_candidates(session, step, now):
                outcome = send_message(
                    user.telegram_id, nudge_filter_text(step), reply_markup=filter_markup
                )
                sent += _record_touch(session, user, nudge_event(NUDGE_FILTER, step), outcome)
            session.commit()
    if sent:
        logger.info("nudges sent: %s", sent)
    return sent


def send_trial_summaries() -> int:
    """Итоги за ~12 ч до конца триала: сколько авто пришло, лучшая скидка.

    Нулевой итог не показываем — он продаёт против нас: вместо «я прислал вам
    0 машин» человек видит, что есть на рынке прямо сейчас (витрина). Пустая
    и витрина — молчим, но событие пишем, чтобы не пересчитывать каждую минуту.
    """
    settings = get_settings()
    if not settings.trial_enabled or not _within_bot_window():
        return 0
    now = utcnow()
    sent = 0
    with SessionLocal() as session:
        candidates = trial_summary_candidates(session, now)
        if not candidates:
            return 0
        showcase = None
        for user in candidates:
            stats = trial_stats(session, user)
            if stats.sent:
                text = trial_summary_text(
                    sent=stats.sent,
                    below_market=stats.below_market,
                    best_percent=stats.best_percent,
                )
            else:
                if showcase is None:  # одна витрина на прогон, а не на юзера
                    showcase = compute_showcase(
                        session,
                        min_samples=settings.bot_below_avg_min_samples,
                        max_below_percent=settings.bot_max_below_market_percent,
                    )[:SHOWCASE_PAGE]
                if not showcase:
                    log_event(session, user.id, EVENT_TRIAL_SUMMARY, {"skipped": "empty"})
                    continue
                text = trial_summary_market_text(showcase)
            outcome = send_message(user.telegram_id, text, html=True)
            sent += _record_touch(session, user, EVENT_TRIAL_SUMMARY, outcome)
        session.commit()
    if sent:
        logger.info("trial summaries sent: %s", sent)
    return sent


def send_pause_reminders() -> int:
    """Раз в день, начиная с обеда: «вы на паузе — включить?» (решение 31.10.2026).

    Каждый день, пока человек сам не нажмёт /play: автоснятия паузы нет, а без
    напоминания поставивший паузу «на вечер» молча теряет продукт. Не в день
    постановки (напоминать через час после /pause — назойливо) и не тем, у кого
    доступа нет (на паузе или нет — уведомлений им и так не будет).

    Каждый цикл worker-а, а не «раз в сутки»: идемпотентность — по
    `pause_reminded_on` (локальная дата), рестарт второго сообщения не даст.
    """
    settings = get_settings()
    local = local_now(settings.scheduler_timezone)
    if local.hour < settings.bot_pause_reminder_hour or not _within_bot_window():
        return 0
    today = local.date()
    today_start_utc = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc
    )
    markup = play_kb().model_dump(exclude_none=True)
    sent = 0
    with SessionLocal() as session:
        users = session.scalars(
            select(BotUser).where(
                BotUser.is_active.is_(True),
                BotUser.paused_at.is_not(None),
                BotUser.paused_at < today_start_utc,
                BotUser.access_status != "revoked",
                (BotUser.pause_reminded_on.is_(None)) | (BotUser.pause_reminded_on < today),
            )
        ).all()
        for user in users:
            outcome = send_message(
                user.telegram_id, pause_reminder_text(user.paused_at), reply_markup=markup
            )
            if outcome is SendOutcome.FAILED:
                continue  # 429/сеть — попробуем следующим циклом
            user.pause_reminded_on = today
            sent += _record_touch(session, user, EVENT_PAUSE_REMINDER, outcome)
        session.commit()
    if sent:
        logger.info("pause reminders sent: %s", sent)
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
    # Схема, источники и каталог — забота сервиса `migrate` (init-db + seed),
    # worker стартует после него (depends_on: service_completed_successfully).
    with SessionLocal() as session:
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
    last_fuel_label_date: date | None = None
    last_reminder_date: date | None = None
    while True:
        try:
            scans = run_weekend_full_scans()
            if scans:
                logger.info("weekend full scans completed: %s", scans)
        except Exception:
            logger.exception("weekend full scan cycle failed")
        try:
            fuel_date = mashina_fuel_labeling_if_due(last_fuel_label_date)
            if fuel_date is not None:
                last_fuel_label_date = fuel_date
        except Exception:
            logger.exception("mashina fuel labeling failed")
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
            send_trial_summaries()
        except Exception:
            logger.exception("trial summaries failed")
        try:
            send_nudges()
        except Exception:
            logger.exception("nudges failed")
        try:
            send_pause_reminders()
        except Exception:
            logger.exception("pause reminders failed")
        try:
            expire_stale_payments()
        except Exception:
            logger.exception("expiring stale payments failed")
        time.sleep(60)


if __name__ == "__main__":
    main()
