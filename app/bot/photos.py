"""Кнопка «📷 Все фото» под карточкой (план 31.10.2026, п. 2.4).

Галерея берётся из `listings.image_urls` — её пишут парсеры при встрече
объявления (лента mashina — до 12 фото, карточка lalafo — 11+). У объявлений,
увиденных до появления колонки, её нет — тянем по нажатию и дописываем в базу:
- lalafo — карточка API (~7–13 КБ через прокси, ~1–2 с);
- mashina — у API нет карточки (GET `/ads/<slug>` → 405), поэтому страница
  объявления на сайте (~450 КБ, без прокси): в ней тот же блок `images`, что в
  ленте, экранированный внутри данных Next.js. Проверено 01.11.2026 — блок на
  странице один, и это фото самого объявления (папка = id объявления).

Здесь — без aiogram: разбивка на альбомы, дебаунс и запасной запрос, чтобы
хендлер оставался тонким, а логика тестировалась без бота.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from app.services.images import extract_image_urls
from app.core.config import get_settings
from app.services.sources import LALAFO, MASHINA

logger = logging.getLogger(__name__)

# Лимит Bot API: в альбоме (sendMediaGroup) от 2 до 10 элементов.
ALBUM_MAX = 10
# Двойной тап не должен слать альбом дважды: на (чат, объявление) — не чаще.
DEBOUNCE_SECONDS = 10.0

_recent: dict[tuple[int, int], float] = {}


def debounce(chat_id: int, listing_id: int, *, now: float | None = None) -> bool:
    """True — можно слать; False — то же нажатие пришло меньше DEBOUNCE_SECONDS назад."""
    moment = time.monotonic() if now is None else now
    key = (chat_id, listing_id)
    last = _recent.get(key)
    if last is not None and moment - last < DEBOUNCE_SECONDS:
        return False
    _recent[key] = moment
    # Память процесса не должна расти вечно: чистим протухшие ключи по ходу.
    if len(_recent) > 1000:
        for stale in [k for k, t in _recent.items() if moment - t >= DEBOUNCE_SECONDS]:
            _recent.pop(stale, None)
    return True


def albums(urls: list[str]) -> list[list[str]]:
    """Режет галерею на альбомы по ALBUM_MAX.

    Хвост из одного фото альбомом не отправить (минимум 2) — тогда он
    уходит последним одиночным фото: вызывающий шлёт `send_photo`.
    """
    return [urls[start : start + ALBUM_MAX] for start in range(0, len(urls), ALBUM_MAX)]


class ListingGone(Exception):
    """Источник ответил, что объявления больше нет (lalafo: 410 Gone)."""


# Начало массива фото в странице mashina: данные Next.js лежат JS-строкой, и
# кавычки внутри экранированы.
_MASHINA_IMAGES_MARKER = '\\"images\\":['
_GONE_STATUSES = (404, 410, 417)  # 417 — так lalafo отвечал на снятое (31.10)


def can_fetch(source_name: str) -> bool:
    return source_name in (LALAFO, MASHINA)


def mashina_gallery_from_page(html: str) -> list[str]:
    """Галерея из HTML страницы объявления mashina; [] — блока нет/не разобрался."""
    start = html.find(_MASHINA_IMAGES_MARKER)
    if start == -1:
        return []
    chunk = html[start + len(_MASHINA_IMAGES_MARKER) - 1 :]
    # Снимаем экранирование JS-строки ровно настолько, чтобы массив стал JSON:
    # внутри элементов галереи только id, хэши и ссылки.
    decoded = chunk.replace('\\"', '"').replace("\\\\", "\\")
    try:
        gallery, _ = json.JSONDecoder().raw_decode(decoded)
    except ValueError:
        logger.warning("photos: mashina page has an images block that is not JSON")
        return []
    return extract_image_urls({"images": gallery}) if isinstance(gallery, list) else []


def _fetch_mashina_page(url: str) -> str:
    settings = get_settings()
    with httpx.Client(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as client:
        response = client.get(url)
    # Снятое объявление mashina не отдаёт 404, а уводит редиректом на главную.
    if response.status_code in _GONE_STATUSES or "/details/" not in str(response.url):
        raise ListingGone
    response.raise_for_status()
    return response.text


def fetch_gallery(source_name: str, external_id: str, url: str) -> list[str]:
    """Запасной путь: карточка источника по нажатию. Синхронно (зовут из потока).

    `url` — публичная страница объявления (нужна mashina). Пустой список — фото
    нет или разобрать не вышло; `ListingGone` — снято.
    """
    if source_name == MASHINA:
        return mashina_gallery_from_page(_fetch_mashina_page(url))
    if not can_fetch(source_name):
        return []
    # Импорт здесь: парсер тянет httpx-клиент с прокси, а модуль импортируется
    # хендлерами при старте бота.
    from app.parsers.lalafo import LalafoParser

    parser = LalafoParser()
    try:
        detail = parser.fetch_details(external_id)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in _GONE_STATUSES:
            raise ListingGone from error
        raise
    finally:
        parser.close()
    return extract_image_urls(detail)
