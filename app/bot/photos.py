"""Кнопка «📷 Все фото» под карточкой (план 31.10.2026, п. 2.4).

Галерея берётся из `listings.image_urls` — её пишут парсеры при встрече
объявления (лента mashina — до 12 фото, карточка lalafo — 11+). У объявлений,
увиденных до появления колонки, её нет: для lalafo тянем карточку по нажатию
(~7–13 КБ через прокси, ~1–2 с) и дописываем в базу; у mashina ручки карточки в
парсере нет — отвечаем ссылкой на объявление, галерея появится при следующей
встрече в ленте.

Здесь — без aiogram: разбивка на альбомы, дебаунс и запасной запрос, чтобы
хендлер оставался тонким, а логика тестировалась без бота.
"""

from __future__ import annotations

import logging
import time

import httpx

from app.services.images import extract_image_urls
from app.services.sources import LALAFO

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


def can_fetch(source_name: str) -> bool:
    return source_name == LALAFO


def fetch_gallery(source_name: str, external_id: str) -> list[str]:
    """Запасной путь: карточка источника по нажатию. Синхронно (зовут из потока).

    Пустой список — фото нет или источник не умеет; `ListingGone` — снято.
    """
    if not can_fetch(source_name):
        return []
    # Импорт здесь: парсер тянет httpx-клиент с прокси, а модуль импортируется
    # хендлерами при старте бота.
    from app.parsers.lalafo import LalafoParser

    parser = LalafoParser()
    try:
        detail = parser.fetch_details(external_id)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (404, 410, 417):  # 417 — так lalafo отвечал на снятое (31.10)
            raise ListingGone from error
        raise
    finally:
        parser.close()
    return extract_image_urls(detail)
