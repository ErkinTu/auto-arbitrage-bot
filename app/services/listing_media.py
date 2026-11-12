"""Фото и видео объявления — сверка со страницей источника по открытию на сайте.

Зачем (заметка владельца 12.11.2026): продавец mashina перезалил фото — старые
файлы CDN удаляет (404), а обход обновляет галерею, только когда снова встречает
объявление: цикл бота видит лишь новые/поднятые, полный обход — по выходным. На
сайте неделю висело бы «Фото не загрузилось». Видео (YouTube) есть только на
странице объявления (`video_url` в данных Next.js) — в ленте API его нет
(проверено 12.11.2026 на 50 объявлениях SsangYong).

Как: страница авто на сайте просит `GET /public/listings/{id}/media`:
- `broken=0` — обычное открытие: страницу источника читаем, если ещё не
  сверяли или сверяли дольше `CHECK_EVERY` назад (видео могли добавить);
- `broken=1` — кадры галереи не грузятся: читаем, если сверяли дольше
  `BROKEN_RECHECK` назад.
Одна загрузка страницы даёт и галерею, и видео (тот же разбор, что у «📷 Все
фото» в боте — `bot/photos.py`). Снято (404/редирект) — объявление
деактивируем, как бот. Только mashina: lalafo ходит через платный прокси, видео
у него нет, а галерею обновляет обход карточки.

Защита источника и своего API: не больше `MAX_PARALLEL` загрузок страниц разом
(страница ~450 КБ, бывает отвечает десятки секунд) — занято → отдаём, что есть,
не отмечая сверку; ответ ручки кэшируется (одновременные открытия одной
машины — один запрос к источнику).
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Listing, Source

logger = logging.getLogger(__name__)

CHECK_EVERY = timedelta(days=7)
BROKEN_RECHECK = timedelta(minutes=30)
PAGE_TIMEOUT = 15  # секунд, как у описания (services/descriptions.py)
MAX_PARALLEL = 3
_slots = threading.BoundedSemaphore(MAX_PARALLEL)

# Значения `video_url` в данных страницы: ссылка продавца в любом виде YouTube.
# Там же встречаются подсказки формы («Вставьте ссылку…») — они не ссылки.
_VIDEO_URL_RE = re.compile(r'\\"video_url\\":\\"([^"\\]*)')
_YOUTUBE_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/|live/|v/))([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])"
)


def youtube_id(url: str | None) -> str | None:
    """Id ролика YouTube из ссылки продавца; не YouTube / мусор — None."""
    if not url:
        return None
    match = _YOUTUBE_ID_RE.search(url)
    return match.group(1) if match else None


def mashina_video_from_page(html: str) -> str | None:
    """Первое настоящее видео на странице объявления mashina (id YouTube) или None."""
    for raw in _VIDEO_URL_RE.findall(html):
        video = youtube_id(raw)
        if video:
            return video
    return None


def _aware(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _due(listing: Listing, *, broken: bool, now: datetime) -> bool:
    checked = _aware(listing.media_checked_at)
    if checked is None:
        return True
    return now - checked >= (BROKEN_RECHECK if broken else CHECK_EVERY)


def refresh_media(session: Session, listing_id: int, *, broken: bool = False, now: datetime | None = None) -> bool:
    """Сверить фото/видео со страницей источника, если пора. True — читали страницу.

    LookupError — объявления нет. Сбой источника не пробрасывается: отдаём, что
    есть, и повторим не раньше следующего окна.
    """
    from app.bot.photos import ListingGone, _fetch_mashina_page, mashina_gallery_from_page
    from app.services.sources import MASHINA, listing_public_url
    from app.services.sync import deactivate_by_card

    now = now or datetime.now(timezone.utc)
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise LookupError("Объявление не найдено")
    if not listing.is_active or not _due(listing, broken=broken, now=now):
        return False
    source = session.get(Source, listing.source_id)
    if source is None or source.name != MASHINA:
        return False
    if not _slots.acquire(blocking=False):
        return False  # источник и так занят нашими запросами — без сверки, повторим
    try:
        try:
            page = _fetch_mashina_page(listing_public_url(listing.source_slug, listing.external_id), timeout=PAGE_TIMEOUT)
        except ListingGone:
            deactivate_by_card(session, listing, reason="card_gone")
            listing.media_checked_at = now
            session.commit()
            return True
        except Exception:  # недоступен / таймаут — не держим страницу авто, повторим позже
            logger.warning("media refresh failed for listing %s", listing_id, exc_info=True)
            listing.media_checked_at = now
            session.commit()
            return True
    finally:
        _slots.release()
    gallery = mashina_gallery_from_page(page)
    if gallery:
        # Пустой разбор (вёрстка поменялась) известное не затирает.
        listing.image_urls = gallery
        listing.image_url = gallery[0]
    listing.video_id = mashina_video_from_page(page)
    listing.media_checked_at = now
    session.commit()
    return True
