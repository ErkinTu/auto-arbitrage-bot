"""Фото в Telegram файлом, а не ссылкой (18.11.2026).

Проблема: `send_photo(url)` просит Telegram самого скачать картинку с CDN
источника. Первая загрузка свежего файла с CDN lalafo/mashina бывает медленной,
Telegram не дожидается и ЗАПОМИНАЕТ неудачу для этого адреса: дальше он отвечает
`failed to get HTTP URL content` / `WEBPAGE_CURL_FAILED` из кэша, не ходя на CDN.
Так свежие объявления оставались без фото у всех, а кнопка «Все фото» не
помогала (адрес тот же). Проверено 18.11: те же файлы с `?cb=<случайное>`
Telegram брал сразу, а наш сервер скачивал их за доли секунды.

Лесенка отправки (каждая ступень — только если предыдущая отвергнута):
1. `file_id` из кэша (notifier) — мгновенно, без CDN;
2. файл, скачанный самим ботом (`BufferedInputFile`) — Telegram ничего не качает;
3. ссылка как есть (прежнее поведение);
4. ссылка с `?cb=…` — обход запомненной Telegram'ом неудачи.

Выключатель: BOT_PHOTO_UPLOAD_ENABLED=false — ступень 2 пропускается.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from urllib.parse import urlsplit, urlunsplit

import httpx
from aiogram.types import BufferedInputFile

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Потолок размера фото в Bot API — 10 МБ; больше не качаем.
MAX_PHOTO_BYTES = 10 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 8.0
# Не больше стольких одновременных скачиваний (альбом — до 10 фото разом).
_MAX_PARALLEL = 6
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}

_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CarAnalyticsBot/1.0)"},
        )
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_PARALLEL)
    return _semaphore


def upload_enabled() -> bool:
    return bool(getattr(get_settings(), "bot_photo_upload_enabled", True))


async def fetch_photo(url: str) -> BufferedInputFile | None:
    """Скачать фото самим. None — не вышло (вызывающий пойдёт по ссылке).

    Любая ошибка (сеть, не 200, не картинка, слишком большой файл) — это не
    повод ронять отправку: ссылка остаётся запасным путём.
    """
    if not upload_enabled():
        return None
    try:
        async with _get_semaphore():
            response = await _get_client().get(url)
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        logger.warning("photo fetch failed for %s: %s", url, exc.__class__.__name__)
        return None
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if response.status_code != 200 or content_type not in _EXTENSIONS:
        logger.warning(
            "photo fetch rejected for %s: status=%s type=%s", url, response.status_code, content_type
        )
        return None
    data = response.content
    if not data or len(data) > MAX_PHOTO_BYTES:
        logger.warning("photo fetch rejected for %s: size=%s", url, len(data))
        return None
    return BufferedInputFile(data, filename=f"photo.{_EXTENSIONS[content_type]}")


async def fetch_many(urls: list[str]) -> dict[str, BufferedInputFile]:
    """Скачать несколько фото параллельно; в ответе только удавшиеся."""
    results = await asyncio.gather(*(fetch_photo(url) for url in urls))
    return {url: item for url, item in zip(urls, results) if item is not None}


def cache_busted(url: str) -> str:
    """Тот же адрес с добавкой в запросе: Telegram видит новый URL, CDN отдаёт тот же файл."""
    parts = urlsplit(url)
    extra = f"cb={secrets.token_hex(4)}"
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit(parts._replace(query=query))
