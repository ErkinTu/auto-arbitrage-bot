"""Прогрев кэша публичных ручек, которые дёргает главная сайта.

Зачем (замер 11.11.2026): холодные `/public/best` — 1,5–6 с, `feed/count` и
`feed/histograms` — 0,6–1,7 с, `catalog/brands` — 1,7 с. Кэш живёт в памяти
процесса и истекает по TTL, так что кому-то из посетителей раз в минуту
достаётся холодный запрос (и «стадо» после рестарта api). Фоновый поток
пересчитывает ровно те ключи, что запрашивает главная, ДО истечения TTL
(`force=True`: старое значение отдаётся, пока считается новое).

Ключи обязаны совпадать с теми, что строят ручки: фильтр собирается той же
`feed_filter`, параметры — теми же именами (проверяет tests/test_warmup.py).
"""

from __future__ import annotations

import logging
import threading
import time

from app.core.cache import cached_call
from app.core.config import get_settings
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

HOME_LIMIT = 24  # PAGE у сайта (useFeed.ts)
BEST_LIMIT = 12


SLOW_EVERY = 4  # «тяжёлые» ключи с TTL 300 с (best, фасеты, марки) — раз в 4 прохода (~3 мин)
_pass = 0


def warm_once() -> int:
    """Один проход: пересчитывает ключи главной. Возвращает число обновлённых."""
    global _pass
    slow_due = _pass % SLOW_EVERY == 0
    _pass += 1
    from app.api.public.feed import feed_filter
    from app.services.catalog_browse import list_brands
    from app.services.public_feed import (
        best_now,
        feed_count,
        feed_facets,
        feed_histograms,
        feed_page,
    )

    settings = get_settings()
    ttl = settings.public_feed_cache_seconds
    done = 0
    with SessionLocal() as session:
        # Главная сайта: «только с фото», сорт по свежести (useFeed.ts, with_photo по умолчанию).
        flt = feed_filter(with_photo=True, session=session)
        jobs = (
            ("public.feed", feed_page, ttl, dict(flt=flt, sort="fresh", limit=min(HOME_LIMIT, settings.public_feed_max_limit), cursor=None, near=None)),
            ("public.feed_count", feed_count, ttl, dict(flt=flt)),
            ("public.feed_histograms", feed_histograms, ttl, dict(flt=flt)),
            ("public.best", best_now, None, dict(limit=BEST_LIMIT)),
            ("public.feed_facets", feed_facets, None, {}),
            ("catalog.brands", list_brands, None, {}),
        )
        for namespace, fn, job_ttl, params in jobs:
            if job_ttl is None and not slow_due:
                continue  # TTL 300 с: до следующего «тяжёлого» прохода ещё свежо
            try:
                cached_call(namespace, fn, session, ttl=job_ttl, force=True, **params)
                done += 1
            except Exception:  # noqa: BLE001 — прогрев не должен ронять api
                logger.warning("warmup %s failed", namespace, exc_info=True)
                session.rollback()
    return done


def _loop(stop: threading.Event, interval: int) -> None:
    # Первый проход — сразу после старта: после рестарта кэш пуст.
    while not stop.is_set():
        started = time.monotonic()
        try:
            warm_once()
        except Exception:  # noqa: BLE001
            logger.warning("warmup pass failed", exc_info=True)
        stop.wait(max(interval - (time.monotonic() - started), 5))


def start_warmup() -> threading.Event | None:
    """Запускает фоновый поток; None — выключено (`PUBLIC_WARMUP_INTERVAL_SECONDS=0`)."""
    interval = get_settings().public_warmup_interval_seconds
    if interval <= 0:
        return None
    stop = threading.Event()
    threading.Thread(target=_loop, args=(stop, interval), name="cache-warmup", daemon=True).start()
    return stop
