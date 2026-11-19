"""Перцептивные хэши фото объявлений — признак «та же машина» (план 18.11 № 27, DU-2).

Замер 19.11.2026 (`docs/data/DUPLICATES_MEASURE_2026-11-19.md`): одно и то же
объявление, выставленное заново или на другой площадке, почти всегда несёт те же
снимки, но другими файлами (mashina ставит водяной знак, размеры разные). dHash
(64 бита) у таких кадров отличается на 0–4 бита, у разных машин — на 16–31.
Хэшируем первые `PHOTO_HASH_FRAMES` кадров: совпадает иногда не обложка, а
второй-третий снимок.

Фоновый поток worker-а (`photo_hash_loop`) идёт по активным объявлениям, новые —
первыми, со скоростью `PHOTO_HASH_REQUESTS_PER_SECOND`. Сами фото не храним.
Новые машины (пробег < 1000 км) не хэшируем: у складов дилеров одинаковые
фото на разные машины — признаком дубля это не будет (замер: 1 из 40).
"""

from __future__ import annotations

import io
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import Listing, ListingPhotoHash
from app.services.images import real_photo

logger = logging.getLogger(__name__)

NEW_CAR_MAX_KM = 1000
RETRY_FAILED_AFTER = timedelta(days=1)
TIMEOUT = 15.0


def _small(url: str) -> str:
    """lalafo: оригинал (~300 КБ) → версия API (~60 КБ); для 9×8 пикселей хватит."""
    if "lalafo.com/i/posters/original/" in url:
        return url.replace("/i/posters/original/", "/i/posters/api/", 1)
    return url


def frame_urls(cover: str | None, gallery: list[str] | None, limit: int) -> list[str]:
    """Первые `limit` настоящих кадров (обложка первой), без повторов и заглушек."""
    out: list[str] = []
    for url in [cover, *(gallery or [])]:
        url = real_photo(url)
        if url:
            url = _small(url)
            if url not in out:
                out.append(url)
        if len(out) >= limit:
            break
    return out


def dhash(data: bytes) -> int | None:
    """dHash 64 бита: серое 9×8, бит = «левый пиксель ярче правого». Как в замере
    19.11 (`scripts/analysis/dups_validate.py`) — порог считан на нём."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data)).convert("L").resize((9, 8))
    except Exception:
        return None
    px = img.tobytes()  # режим L: байт на пиксель, построчно
    return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])


def is_informative(value: int) -> bool:
    """Почти однотонный кадр (белый фон, чёрная заглушка) даёт хэш из одних
    нулей/единиц — у разных машин он одинаковый. Такие кадры не сравниваем."""
    ones = value.bit_count()
    return 6 <= ones <= 58


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def due_query(now: datetime, limit: int):
    """Активные с фото, у которых хэша нет, обложка сменилась, или прошлый раз
    не скачалось ничего (повтор раз в сутки). Новые объявления — первыми."""
    hashed = ListingPhotoHash
    return (
        select(Listing.id, Listing.image_url, Listing.image_urls)
        .outerjoin(hashed, hashed.listing_id == Listing.id)
        .where(
            Listing.is_active.is_(True),
            Listing.image_url.is_not(None),
            or_(Listing.mileage_km.is_(None), Listing.mileage_km >= NEW_CAR_MAX_KM),
            or_(
                hashed.listing_id.is_(None),
                hashed.cover_url.is_distinct_from(Listing.image_url),
                and_(hashed.frames == 0, hashed.hashed_at < now - RETRY_FAILED_AFTER),
            ),
        )
        .order_by(Listing.id.desc())
        .limit(limit)
    )


class _Pacer:
    def __init__(self, per_second: float, clock=time.monotonic, sleep=time.sleep):
        self.gap = 1.0 / per_second if per_second > 0 else 0.0
        self.clock, self.sleep, self.next_at = clock, sleep, 0.0

    def wait(self) -> None:
        now = self.clock()
        if now < self.next_at:
            self.sleep(self.next_at - now)
            now = self.next_at
        self.next_at = now + self.gap


def _fetch_hash(client: httpx.Client, url: str) -> int | None:
    try:
        resp = client.get(url)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200 or not resp.content:
        return None
    return dhash(resp.content)


def hash_pass(
    session: Session,
    client: httpx.Client,
    *,
    limit: int,
    now: datetime | None = None,
    pacer: _Pacer | None = None,
    stop: threading.Event | None = None,
) -> dict[str, int]:
    """Один проход: до `limit` объявлений. Коммит после каждого — прерывание
    (рестарт worker-а) ничего не теряет."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    pacer = pacer or _Pacer(settings.photo_hash_requests_per_second)
    rows = session.execute(due_query(now, limit)).all()
    stats = {"listings": 0, "frames": 0, "failed": 0}
    for listing_id, cover, gallery in rows:
        if stop is not None and stop.is_set():
            break
        hashes: list[int] = []
        for url in frame_urls(cover, gallery, settings.photo_hash_frames):
            pacer.wait()
            value = _fetch_hash(client, url)
            if value is None:
                stats["failed"] += 1
            else:
                hashes.append(value)
        row = session.get(ListingPhotoHash, listing_id) or ListingPhotoHash(listing_id=listing_id)
        row.cover_url = cover
        row.hashes = hashes
        row.frames = len(hashes)
        row.hashed_at = now
        session.add(row)
        session.commit()
        stats["listings"] += 1
        stats["frames"] += len(hashes)
    return stats


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": get_settings().user_agent},
    )


def photo_hash_loop(stop: threading.Event | None = None) -> None:
    """Цель потока worker-а: хэшировать, пока есть что; иначе ждать 5 минут."""
    from app.db.session import SessionLocal

    stop = stop or threading.Event()
    logger.info("photo hash loop started")
    with make_client() as client:
        while not stop.is_set():
            settings = get_settings()
            idle = 300.0
            if settings.photo_hash_enabled:
                try:
                    with SessionLocal() as session:
                        stats = hash_pass(session, client, limit=settings.photo_hash_batch, stop=stop)
                    if stats["listings"]:
                        logger.info("photo hashes: %s", stats)
                    if stats["listings"] >= settings.photo_hash_batch:
                        idle = 0.0
                except Exception:
                    logger.exception("photo hash pass failed")
                    idle = 60.0
            stop.wait(idle)
