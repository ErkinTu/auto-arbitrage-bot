"""Фоновый цикл бота: легкий fresh-парс + подбор уведомлений.

Цикл идёт каждые BOT_FRESH_INTERVAL_MINUTES в окне активности
[BOT_ACTIVE_START_HOUR, BOT_ACTIVE_END_HOUR) локального времени и состоит из
двух НЕЗАВИСИМЫХ частей:

- `sync_source` — обход одного источника, по потоку на источник (у каждого своя
  сессия: объект Session потокобезопасным не является). Источники параллельны,
  потому что они ничем друг с другом не связаны, а последовательно цикл стоил
  сумму, а не максимум;
- `collect_matches` — подбор уведомлений. Это ЗАПРОС К БАЗЕ по окну
  `BOT_NEW_LOOKBACK_MINUTES` (на проде 180 мин при интервале 10), а не разбор
  «что этот цикл только что скачал». Отсюда правило: **застрявший источник
  рассылку не держит**. Что он докоммитит с опозданием, подберёт следующий цикл
  — окно в 18 раз шире интервала, а `sent_notifications` не даст дубля.

Рассылка идёт **волнами**: как только доехал очередной источник, подбираем и
шлём то, что уже в базе (план 31.10.2026, `docs/planning/PUSH_PIPELINE_PLAN.md`).
Иначе находка mashina (обход ~2.5 мин) ждала бы lalafo (~6 мин) просто так.
Потолок пушей на человека при этом общий на цикл (`cycle_counts`): вторая
волна получает только остаток.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

from app.bot.db import mark_user_blocked, refresh_showcase, tag_digest_rows
from app.bot.digest import batch_id_for
from app.bot.matching import PendingNotification, match_new_listings
from app.bot.notifier import BotLike, send_notifications
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.lookup import ensure_brand_targets, get_or_create_source
from app.services.schedule import local_now, seconds_until_window, window_end_utc
from app.services.sources import LALAFO, SOURCE_NAMES, build_parser, source_base_url
from app.services.sync import run_sync, utcnow

logger = logging.getLogger(__name__)

# Минимальный зазор между циклами, даже если цикл сам съел весь интервал.
MIN_PAUSE_SECONDS = 60


def sync_source(source_name: str) -> None:
    """Обход ОДНОГО источника: 1 страница x page_size свежих объявлений на марку.

    Страница маленькая (20), а циклы частые — нагрузка на сайт ниже, чем редкие
    тяжелые страницы. Пауза между запросами марок обеспечивается парсером: он
    спит REQUEST_DELAY_SECONDS после каждого HTTP-запроса.

    Своя сессия на вызов — обязательна: функция крутится в отдельном потоке на
    источник, а Session у SQLAlchemy не потокобезопасна.
    """
    settings = get_settings()
    with SessionLocal() as session:
        try:
            source = get_or_create_source(session, source_name, source_base_url(source_name))
            session.commit()
            targets = ensure_brand_targets(session, source, settings.allowed_brand_list)
            # Для lalafo здесь же подтягивается курс НБКР (лениво, раз в день).
            parser = build_parser(source_name, session)
        except Exception:
            session.rollback()
            logger.exception("bot fresh cycle: source %s is unavailable, skipping", source_name)
            return
        page_size = (
            settings.lalafo_fresh_page_size if source_name == LALAFO else settings.bot_fresh_page_size
        )
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
                        page_size_override=page_size,
                        max_pages_override=1,
                        max_items_override=page_size,
                        update_target_bookkeeping=False,
                        catchup_max_pages=settings.bot_catchup_max_pages,
                    )
                    logger.info(
                        "bot fresh sync %s %s: new=%s processed=%s",
                        source_name,
                        target.brand_filter,
                        summary.new,
                        summary.processed,
                    )
                except Exception:
                    session.rollback()
                    logger.exception(
                        "bot fresh sync failed for %s %s", source_name, target.brand_filter
                    )
        finally:
            parser.close()


def collect_matches(cycle_counts: dict[int, int] | None = None) -> list[PendingNotification]:
    """Подбор уведомлений по тому, что УЖЕ лежит в базе.

    Намеренно не принимает результат синка: окно `BOT_NEW_LOOKBACK_MINUTES`
    шире интервала во много раз, поэтому отставший источник ничего не теряет —
    его находки подберёт следующий цикл.

    `cycle_counts` — расход потолка пушей прошлыми волнами этого цикла
    (см. `match_new_listings`); дополняется на месте.
    """
    settings = get_settings()
    with SessionLocal() as session:
        now = utcnow()
        since = now - timedelta(minutes=settings.bot_new_lookback_minutes)
        # Перед ночной паузой выдержка отстающих тарифов сходит к нулю: вычитаем
        # один интервал, потому что последний цикл дня идёт около 23:50, и
        # объявление с дозреванием в 23:55 «формально успевает», но забрать его
        # уже некому — а утреннее уведомление ведёт на снятое объявление.
        flush_deadline = window_end_utc(
            now, settings.bot_active_end_hour, settings.scheduler_timezone
        ) - timedelta(minutes=settings.bot_fresh_interval_minutes)
        return match_new_listings(
            session,
            since,
            factor=settings.bot_below_avg_factor,
            min_samples=settings.bot_below_avg_min_samples,
            max_age_hours=settings.bot_max_listing_age_hours,
            max_per_user_full=settings.bot_max_notifications_per_user_per_cycle_full,
            max_per_user_delayed=settings.bot_max_notifications_per_user_per_cycle_delayed,
            delayed_lag_minutes=settings.delayed_tier_lag_minutes,
            flush_deadline=flush_deadline,
            require_complete=settings.bot_require_complete_listings,
            max_below_percent=settings.bot_max_below_market_percent,
            cycle_counts=cycle_counts,
        )


async def _on_blocked(chat_id: int) -> None:
    await asyncio.to_thread(mark_user_blocked, chat_id)


async def _on_digest(batch_id: int, groups: list[tuple[int, list[int]]]) -> bool:
    return await asyncio.to_thread(tag_digest_rows, batch_id, groups)


class _BatchClock:
    """Номера пачек дайджеста: по минуте отправки, но строго возрастающие.

    Две волны могут уложиться в одну минуту, а кнопка «Показать с фото» берёт
    строки по (юзер, batch_id) — с общим номером вторая волна подмешалась бы в
    список первой.
    """

    def __init__(self) -> None:
        self.last: int | None = None

    def next(self) -> int:
        batch_id = batch_id_for(utcnow())
        if self.last is not None and batch_id <= self.last:
            batch_id = self.last + 1
        self.last = batch_id
        return batch_id


async def _send_wave(
    bot: BotLike,
    sources: list[str],
    *,
    cycle_counts: dict[int, int],
    batches: _BatchClock,
) -> None:
    """Одна волна: подбор по базе + рассылка. Падение волны не роняет цикл.

    `sources` — что доехало перед волной (только для лога); пусто — финальная
    (единственная) волна: первый цикл дня, дедлайн или нет свежих обходов.
    """
    settings = get_settings()
    label = "+".join(sources) or "all"
    try:
        pending = await asyncio.to_thread(collect_matches, cycle_counts)
        if not pending:
            logger.info("wave %s: no matching new listings", label)
            return
        sent = await send_notifications(
            bot,
            pending,
            on_blocked=_on_blocked,
            group_min=settings.bot_group_min_listings,
            rich_cards=settings.bot_rich_cards_per_cycle,
            batch_id=batches.next(),
            on_digest=_on_digest,
        )
        logger.info("wave %s: %s message(s) for %s listing(s)", label, sent, len(pending))
    except Exception:
        logger.exception("notification wave %s failed", label)


def _log_task_error(task: asyncio.Task) -> None:
    """Забирает исключение у завершённой задачи обхода.

    `asyncio.wait` исключения не поднимает: не спросить — значит потерять
    причину, по которой источник молчал весь цикл.
    """
    if task.cancelled() or not task.done():
        return
    error = task.exception()
    if error is not None:
        logger.error("source sync task %s failed: %r", task.get_name(), error)


async def _run_sources(
    inflight: dict[str, asyncio.Task],
    *,
    deadline_seconds: float,
    on_progress: Callable[[list[str]], Awaitable[None]] | None = None,
) -> bool:
    """Запускает обход всех источников параллельно и ждёт их — но не бесконечно.

    `on_progress(names)` зовётся сразу, как доехал очередной источник (или
    несколько разом), — это волна рассылки. Волны идут строго по очереди: пока
    шлётся первая, второй источник продолжает обход в своём потоке, а его
    завершение подхватит следующий `asyncio.wait`. Время волны входит в дедлайн.

    Возвращает True, если волна по завершению источника не прошла ни разу или
    кто-то не уложился в дедлайн: тогда вызывающему нужна финальная волна «по
    тому, что уже в базе» — как было до волн.

    Два правила, оба выведены из замеров на проде:

    1. **Один синк на источник одновременно.** Затор lalafo (таймауты прокси с
       бэкоффом 10/20/30/40 с) растягивает её обход до 22 минут. Без этой
       проверки к третьему циклу на уже отваливающийся прокси пришлось бы три
       параллельных обхода — то есть мы бы добивали источник ровно тогда, когда
       ему плохо.
    2. **Застрявший источник не держит рассылку.** По истечении дедлайна цикл
       идёт дальше, а отставший поток продолжает работать в фоне и коммитит в
       свою сессию. Ничего не теряется: `collect_matches` смотрит в базу по
       окну `BOT_NEW_LOOKBACK_MINUTES`, которое много шире интервала.
    """
    started: list[str] = []
    for source_name in SOURCE_NAMES:
        task = inflight.get(source_name)
        if task is not None and not task.done():
            logger.warning(
                "source %s is still syncing since the previous cycle, not starting another one",
                source_name,
            )
            continue
        if task is not None:
            # Задача доехала уже после прошлого дедлайна — её результат никто не
            # смотрел. Забираем исключение здесь, иначе asyncio напишет в лог
            # «Task exception was never retrieved» без привязки к источнику.
            _log_task_error(task)
        inflight[source_name] = asyncio.create_task(
            asyncio.to_thread(sync_source, source_name), name=f"sync:{source_name}"
        )
        started.append(source_name)

    # Задачи, начатые прошлым циклом и ещё живые, сюда не входят: их завершение
    # волну не даёт (их находки подберёт финальная волна или следующий цикл).
    running = {inflight[name] for name in started}
    waves = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_seconds
    while running:
        timeout = deadline - loop.time()
        if timeout <= 0:
            break
        done, running = await asyncio.wait(
            running, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            _log_task_error(task)
        if done and on_progress is not None:
            await on_progress(sorted(_source_of(task) for task in done))
            waves += 1
    if running:
        logger.warning(
            "still syncing after %.0f s: %s; sending what is already in the database",
            deadline_seconds,
            ", ".join(sorted(task.get_name() for task in running)),
        )
    return waves == 0 or bool(running)


def _source_of(task: asyncio.Task) -> str:
    return task.get_name().removeprefix("sync:")


async def polling_loop(bot: BotLike) -> None:
    settings = get_settings()
    interval_seconds = settings.bot_fresh_interval_minutes * 60
    # Живые (или только что завершившиеся) задачи обхода, по одной на источник.
    inflight: dict[str, asyncio.Task] = {}
    batches = _BatchClock()
    # Первый цикл после ночной паузы (и после старта бота — простой даёт тот же
    # залп) идёт ОДНОЙ рассылкой, а не волнами: за ночь у человека в среднем 28
    # находок (максимум 55), и волнами он получил бы два списка и 6 фото подряд.
    # Ночным объявлениям лишние ~3 мин ожидания lalafo ничего не стоят, а лучшие
    # фото так выбираются по выгоде из обоих источников, а не «кто первый».
    single_wave = True
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
            single_wave = True

        started = time.monotonic()
        # Расход потолка пушей по юзерам за этот цикл — общий для всех волн.
        cycle_counts: dict[int, int] = {}

        async def wave(sources: list[str]) -> None:
            await _send_wave(bot, sources, cycle_counts=cycle_counts, batches=batches)

        try:
            needs_final_wave = await _run_sources(
                inflight,
                deadline_seconds=interval_seconds,
                on_progress=None if single_wave else wave,
            )
            if needs_final_wave:
                await wave([])
        except Exception:
            logger.exception("bot poll cycle failed")
        single_wave = False
        try:
            # Прогрев витрины («лучшие сейчас», демо в /start) после свежего
            # парса: на проде пересчёт — несколько секунд, и пусть их ждёт цикл,
            # а не новичок, нажавший /start. TTL витрины чуть длиннее цикла,
            # так что между прогревами кэш не пустеет.
            await asyncio.to_thread(refresh_showcase)
        except Exception:
            logger.exception("showcase warm-up failed")

        # Пауза — ОСТАТОК интервала, а не полный интервал: иначе период равен
        # `interval + длительность цикла`, и «каждые 10 минут» на деле выходили
        # 14.5 (замер за 7 дней, `docs/CHANGELOG.md § 2026-08-29`). Считаем по
        # монотонным часам: перевод системного времени не должен ни удваивать
        # паузу, ни съедать её.
        elapsed = time.monotonic() - started
        pause = max(interval_seconds - elapsed, MIN_PAUSE_SECONDS)
        if elapsed >= interval_seconds:
            # Овер — почти всегда таймауты прокси lalafo с бэкоффом 10/20/30/40 с.
            # Вплотную такие циклы пускать нельзя: источник уже не отвечает, и
            # долбить его без паузы — худшее, что можно сделать. Отсюда пол
            # `MIN_PAUSE_SECONDS`.
            logger.warning(
                "cycle took %.0f s, longer than the %s min interval; pausing only %.0f s",
                elapsed,
                settings.bot_fresh_interval_minutes,
                pause,
            )
        else:
            logger.info("cycle took %.0f s, sleeping %.0f s", elapsed, pause)
        await asyncio.sleep(pause)
