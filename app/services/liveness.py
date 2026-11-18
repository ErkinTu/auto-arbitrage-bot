"""Живая проверка «не снято ли» у источника (решение владельца 12.11.2026).

Зачем: снятое объявление обход замечает поздно — у lalafo валидатор берёт
не виденные 3+ дня, у mashina полный обход раз в неделю. Всё это время снятая
машина висит в «Лучшем сейчас», в избранном и на своей странице как живая.

Что проверяем и как часто (в отдельном потоке worker — `liveness_loop`, не
ждёт часовых обходов; страница авто — синхронно из API):
- «Лучшее сейчас» сайта — 12 позиций раз в `LIVENESS_BEST_MINUTES` (5);
- избранное — id, которые сайт запрашивал за `LIVENESS_WATCHED_HOURS` (24 ч)
  через `/public/listings?ids=` (таблица `watched_listings`), раз в
  `LIVENESS_WATCHED_MINUTES` (30);
- страница авто — при открытии, если не проверяли `LIVENESS_OPEN_MINUTES` (60).
Объявление, которое обход или проверка видели живым позже окна, не трогаем
(`verified_at` / `last_seen_at`).

Как проверяем — одним лёгким запросом:
- mashina: API карточки `…/v1/ads/{slug}/detail` (тот, что дёргает их сайт;
  ~5 КБ сжатого JSON вместо ~450 КБ страницы, проверено 12.11.2026). Живое —
  200 и `status=active`; снятое — 404 (~170 Б). Заодно свежая галерея и
  `video_url` (YouTube) — перезалитые фото и видео без отдельных запросов.
- lalafo: карточка API через прокси (~3 КБ сжатого JSON, 4–9 КБ с туннелем —
  замер 12.11.2026; ~2000 проверок в сутки ≈ $0,02). 404/410/417 или
  `status_id` 3/8 — снято.

Безопасность (заметка владельца 12.11.2026): посетитель не выбирает, куда
пойдёт сервер, — только наши объявления, адрес строим мы (slug/id из базы).
Косвенно запросы к источникам вызывают открытие страницы авто и избранное —
поэтому жёсткие потолки: на открытие — не чаще раза в час на объявление, не
больше `OPEN_PARALLEL` разом и `OPEN_PER_MINUTE` в минуту на процесс (сверх —
без проверки, страница отдаёт что есть); избранное — не больше
`WATCHED_MAX` самых свежих id и `liveness_max_per_pass` проверок за проход,
только существующие активные объявления. Перебор id чужими руками упирается в
эти потолки и лимит частоты API по IP.

Защита от ложного «снято»: только явный ответ источника; сбой/таймаут/прокси —
«не знаю», ничего не меняем. Три таких подряд по источнику — проход по нему
прекращаем (лежащий прокси ≠ снятые машины). Снятое деактивируется как у
валидатора (история статусов), но **данные остаются у нас, номер продавца
тоже** (решение владельца 12.11.2026) — скрываем только от пользователей. Обход
вернёт объявление, если увидит его снова.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Listing, Source, WatchedListing

logger = logging.getLogger(__name__)

TIMEOUT = 8.0  # секунд на запрос: страница авто ждёт ответа
BREAKER = 3  # столько «не знаю» подряд по источнику — проход по нему прекращаем
OPEN_PARALLEL = 3  # одновременных проверок по открытию страницы (процесс API)
OPEN_PER_MINUTE = 30  # и не больше стольких в минуту на процесс — общий потолок
WATCHED_MAX = 1000  # избранное: проверяем не больше стольких самых свежих id
_open_slots = threading.BoundedSemaphore(OPEN_PARALLEL)
_open_times: list[float] = []
_open_lock = threading.Lock()


def _open_budget(moment: float | None = None) -> bool:
    """Скользящая минута: есть ли место ещё на одну проверку по открытию."""
    moment = time.monotonic() if moment is None else moment
    with _open_lock:
        while _open_times and moment - _open_times[0] >= 60:
            _open_times.pop(0)
        if len(_open_times) >= OPEN_PER_MINUTE:
            return False
        _open_times.append(moment)
        return True


def reset_limits() -> None:
    """Для тестов: потолки — память процесса."""
    with _open_lock:
        _open_times.clear()
    with _watch_lock:
        _watch_marks.clear()


class Verdict(str, Enum):
    ALIVE = "alive"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass
class Check:
    verdict: Verdict
    reason: str | None = None
    status_id: int | None = None
    images: list[str] | None = None  # свежая галерея, если источник отдал
    video_id: str | None = None
    has_video: bool = False  # источник сообщает о видео (mashina) — пустое тоже правда


def _aware(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def mashina_detail_url(slug: str) -> str:
    """API карточки mashina: тот же хост и версия, что у ленты (`…/v1/ads/listings`)."""
    base = get_settings().mashina_api_url.rsplit("/ads/", 1)[0]
    return f"{base}/ads/{slug}/detail"


def check_mashina(slug: str | None, client: httpx.Client | None = None) -> Check:
    from app.services.images import extract_image_urls
    from app.services.listing_media import youtube_id

    if not slug:
        return Check(Verdict.UNKNOWN, "no_slug")
    settings = get_settings()
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": settings.user_agent})
    try:
        response = client.get(mashina_detail_url(slug), headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        logger.info("liveness: mashina %s — %r", slug, exc)
        return Check(Verdict.UNKNOWN, "network")
    finally:
        if own:
            client.close()
    if response.status_code in (404, 410):
        return Check(Verdict.GONE, "card_gone")
    if response.status_code != 200:
        return Check(Verdict.UNKNOWN, f"http_{response.status_code}")
    try:
        data = response.json()
    except ValueError:
        return Check(Verdict.UNKNOWN, "not_json")
    if not isinstance(data, dict) or "status" not in data:
        return Check(Verdict.UNKNOWN, "unexpected")
    if data.get("status") != "active":
        return Check(Verdict.GONE, "card_inactive")
    return Check(
        Verdict.ALIVE,
        images=extract_image_urls(data) or None,
        video_id=youtube_id(data.get("video_url")),
        has_video=True,
    )


def check_lalafo(external_id: str) -> Check:
    from app.parsers.lalafo import LalafoParser
    from app.services.images import extract_image_urls
    from app.services.sync import LALAFO_DEAD_STATUS_IDS

    parser = LalafoParser(timeout=TIMEOUT, retry_attempts=1)
    try:
        detail = parser.fetch_details(external_id)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        # 417 — так lalafo отвечал на снятое (bot/photos.py, 31.10.2026).
        if code in (404, 410, 417):
            return Check(Verdict.GONE, "card_gone")
        return Check(Verdict.UNKNOWN, f"http_{code}")
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("liveness: lalafo %s — %r", external_id, exc)
        return Check(Verdict.UNKNOWN, "network")
    finally:
        parser.close()
    status_id = detail.get("status_id")
    if status_id in LALAFO_DEAD_STATUS_IDS:
        return Check(Verdict.GONE, "card_inactive", status_id=status_id)
    return Check(Verdict.ALIVE, images=extract_image_urls(detail) or None)


def check_listing(source_name: str, listing: Listing) -> Check:
    from app.services.sources import LALAFO, MASHINA

    if source_name == MASHINA:
        return check_mashina(listing.source_slug)
    if source_name == LALAFO and get_settings().liveness_lalafo:
        return check_lalafo(listing.external_id)
    return Check(Verdict.UNKNOWN, "unsupported")


def apply(session: Session, listing: Listing, result: Check, now: datetime) -> None:
    """Записать вердикт. Коммит — за вызывающим."""
    from app.services.sync import deactivate_by_card

    if result.verdict is Verdict.UNKNOWN:
        return
    listing.verified_at = now
    if result.verdict is Verdict.GONE:
        if listing.is_active:
            deactivate_by_card(
                session, listing, reason=result.reason or "card_gone", status_id=result.status_id, keep_phone=True
            )
        return
    # Живое: заодно свежие фото (перезалитые) и видео. Пустое известное не затирает.
    if result.images:
        listing.image_urls = result.images
        listing.image_url = result.images[0]
    if result.has_video:
        listing.video_id = result.video_id


def is_fresh(listing: Listing, window: timedelta, now: datetime) -> bool:
    """Обход или проверка видели живым позже окна — проверять незачем."""
    seen = [m for m in (_aware(listing.verified_at), _aware(listing.last_seen_at)) if m is not None]
    return bool(seen) and now - max(seen) < window


def verify_one(session: Session, listing_id: int, *, window: timedelta, now: datetime | None = None) -> bool:
    """Проверка по открытию страницы авто. True — спрашивали источник.

    Не больше `OPEN_PARALLEL` одновременно на процесс: занято — без проверки
    (страница не ждёт очереди к источнику)."""
    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise LookupError("Объявление не найдено")
    if not settings.liveness_enabled or not listing.is_active or is_fresh(listing, window, now):
        return False
    source = session.get(Source, listing.source_id)
    if source is None or not _open_budget():
        return False
    if not _open_slots.acquire(blocking=False):
        return False
    try:
        result = check_listing(source.name, listing)
    finally:
        _open_slots.release()
    if result.verdict is Verdict.UNKNOWN:
        return False
    apply(session, listing, result, now)
    session.commit()
    return True


def verify_many(session: Session, ids: list[int], *, window: timedelta, now: datetime | None = None) -> dict[str, int]:
    """Фоновый проход по списку id. Счётчики вердиктов — для лога."""
    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    counts = {"alive": 0, "gone": 0, "unknown": 0, "skipped": 0}
    if not ids:
        return counts
    rows = session.execute(
        select(Listing, Source.name).join(Source, Source.id == Listing.source_id).where(Listing.id.in_(ids))
    ).all()
    failures: dict[str, int] = {}
    checked = 0
    for listing, source_name in rows:
        if not listing.is_active or is_fresh(listing, window, now) or failures.get(source_name, 0) >= BREAKER:
            counts["skipped"] += 1
            continue
        if checked >= settings.liveness_max_per_pass:
            counts["skipped"] += 1
            continue
        checked += 1
        result = check_listing(source_name, listing)
        counts[result.verdict.value] += 1
        if result.verdict is Verdict.UNKNOWN:
            failures[source_name] = failures.get(source_name, 0) + 1
            if failures[source_name] == BREAKER:
                logger.warning("liveness: %s не отвечает %s раз подряд — проход по нему остановлен", source_name, BREAKER)
            continue
        failures[source_name] = 0
        apply(session, listing, result, now)
        session.commit()  # по одному: долгий проход не держит транзакцию
    return counts


# --- Избранное: какие id сайт запрашивал недавно -----------------------------

WATCH_THROTTLE = 600.0  # один id пишем в базу не чаще раза в 10 мин на процесс
_watch_marks: dict[int, float] = {}
_watch_lock = threading.Lock()


def watch(session: Session, ids: list[int]) -> None:
    """Отметить запрошенные id (избранное). Дёшево: троттлинг в памяти, upsert пачкой."""
    if not ids or not get_settings().liveness_enabled:
        return
    moment = time.monotonic()
    with _watch_lock:
        fresh = [i for i in ids if moment - _watch_marks.get(i, -WATCH_THROTTLE) >= WATCH_THROTTLE]
        for i in fresh:
            _watch_marks[i] = moment
        if len(_watch_marks) > 20_000:
            for key in [k for k, at in _watch_marks.items() if moment - at > WATCH_THROTTLE]:
                _watch_marks.pop(key, None)
    if not fresh:
        return
    now = datetime.now(timezone.utc)
    rows = [{"listing_id": i, "requested_at": now} for i in fresh]
    try:
        if session.get_bind().dialect.name == "postgresql":
            stmt = pg_insert(WatchedListing).values(rows)
            session.execute(stmt.on_conflict_do_update(index_elements=["listing_id"], set_={"requested_at": now}))
        else:  # тесты (SQLite)
            for row in rows:
                session.merge(WatchedListing(**row))
        session.commit()
    except Exception:  # учёт избранного не должен ронять ответ
        session.rollback()
        logger.warning("liveness: не удалось отметить избранное", exc_info=True)


def watched_ids(session: Session, now: datetime) -> list[int]:
    horizon = now - timedelta(hours=get_settings().liveness_watched_hours)
    session.execute(WatchedListing.__table__.delete().where(WatchedListing.requested_at < horizon))
    session.commit()
    return list(
        session.scalars(
            select(WatchedListing.listing_id)
            .where(WatchedListing.requested_at >= horizon)
            .order_by(WatchedListing.requested_at.desc())
            .limit(WATCHED_MAX)
        )
    )


def best_ids(session: Session) -> list[int]:
    from app.services.public_feed import best_now

    return [item["id"] for item in best_now(session, limit=12)]


# --- Фоновый цикл (поток в worker) --------------------------------------------


def run_due(session: Session, last: dict[str, float], *, clock=time.monotonic) -> dict[str, dict[str, int]]:
    """Одна итерация: запускает проходы, у которых подошёл срок."""
    settings = get_settings()
    out: dict[str, dict[str, int]] = {}
    moment = clock()
    jobs = (
        ("best", settings.liveness_best_minutes, best_ids),
        ("watched", settings.liveness_watched_minutes, lambda s: watched_ids(s, datetime.now(timezone.utc))),
    )
    for name, minutes, ids_of in jobs:
        if moment - last.get(name, -1e12) < minutes * 60:
            continue
        last[name] = moment
        window = timedelta(minutes=minutes)
        out[name] = verify_many(session, ids_of(session), window=window)
        if out[name]["gone"] or out[name]["unknown"]:
            logger.info("liveness %s: %s", name, out[name])
    return out


def liveness_loop(stop: threading.Event | None = None) -> None:
    """Цель потока worker: проверки «не снято ли» по своему расписанию."""
    from app.db.session import SessionLocal

    stop = stop or threading.Event()
    last: dict[str, float] = {}
    logger.info("liveness loop started")
    while not stop.is_set():
        if get_settings().liveness_enabled:
            try:
                with SessionLocal() as session:
                    run_due(session, last)
            except Exception:
                logger.exception("liveness pass failed")
        stop.wait(30)
