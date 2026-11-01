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
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.services.images import extract_image_urls
from app.services.phones import normalize_phone
from app.core.config import get_settings
from app.services.sources import LALAFO, MASHINA

logger = logging.getLogger(__name__)

# Лимит Bot API: в альбоме (sendMediaGroup) от 2 до 10 элементов.
ALBUM_MAX = 10
# Двойной тап не должен слать альбом дважды: на (чат, объявление) — не чаще.
DEBOUNCE_SECONDS = 10.0

_recent: dict[tuple[int, int], float] = {}

# Лимит альбомов на человека (план 01.11.2026, п. Г): защита от выкачивания фото
# через бота. Скользящее окно в памяти процесса, как дебаунс: рестарт обнуляет —
# не страшно. Число — `BOT_PHOTOS_ALBUMS_PER_HOUR`.
ALBUMS_WINDOW_SECONDS = 3600.0
_albums_sent: dict[int, list[float]] = {}


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


def _recent_albums(chat_id: int, moment: float) -> list[float]:
    window = [t for t in _albums_sent.get(chat_id, ()) if moment - t < ALBUMS_WINDOW_SECONDS]
    if window:
        _albums_sent[chat_id] = window
    else:
        _albums_sent.pop(chat_id, None)
    return window


def album_wait_minutes(chat_id: int, *, limit: int, now: float | None = None) -> int:
    """0 — альбом можно; иначе через сколько минут освободится место в окне.

    `limit <= 0` — лимит выключен. Считаются только ОТПРАВЛЕННЫЕ альбомы
    (`record_album`): «снято»/«фото нет» место не съедают.
    """
    if limit <= 0:
        return 0
    moment = time.monotonic() if now is None else now
    window = _recent_albums(chat_id, moment)
    if len(window) < limit:
        return 0
    free_at = window[-limit] + ALBUMS_WINDOW_SECONDS
    return max(1, int((free_at - moment + 59) // 60))


def record_album(chat_id: int, *, now: float | None = None) -> None:
    moment = time.monotonic() if now is None else now
    _albums_sent.setdefault(chat_id, []).append(moment)
    if len(_albums_sent) > 1000:  # память процесса не растёт вечно
        for stale in list(_albums_sent):
            _recent_albums(stale, moment)


def reserve_album(chat_id: int, *, limit: int, now: float | None = None) -> int:
    """Занять место в лимите ДО отправки: 0 — занято, иначе минуты ожидания.

    Раньше альбом засчитывался ПОСЛЕ отправки, и 30 одновременных нажатий все
    видели свободное место (п. К1). Не отправили — `release_album`.
    """
    wait = album_wait_minutes(chat_id, limit=limit, now=now)
    if not wait and limit > 0:
        record_album(chat_id, now=now)
    return wait


def release_album(chat_id: int) -> None:
    """Вернуть место: альбом не ушёл (снято, фото нет, Telegram отверг)."""
    window = _albums_sent.get(chat_id)
    if window:
        window.pop()
        if not window:
            _albums_sent.pop(chat_id, None)


# Повтор того же объявления (п. К2): альбом уже в чате — второй раз не шлём, а
# показываем «фото выше». Окно — SHOWN_SECONDS после отправки.
SHOWN_SECONDS = 600.0
_shown: dict[tuple[int, int], float] = {}


def recently_shown(chat_id: int, listing_id: int, *, now: float | None = None) -> bool:
    moment = time.monotonic() if now is None else now
    at = _shown.get((chat_id, listing_id))
    return at is not None and moment - at < SHOWN_SECONDS


def mark_shown(chat_id: int, listing_id: int, *, now: float | None = None) -> None:
    moment = time.monotonic() if now is None else now
    _shown[(chat_id, listing_id)] = moment
    if len(_shown) > 5000:
        for key in [k for k, t in _shown.items() if moment - t >= SHOWN_SECONDS]:
            _shown.pop(key, None)


# Вердикт живой проверки «не снято» (п. К1): раньше старое объявление
# проверялось на источнике при КАЖДОМ нажатии — от любого человека.
VERIFIED_SECONDS = 1800.0
_verified: dict[int, float] = {}


def recently_verified(listing_id: int, *, now: float | None = None) -> bool:
    moment = time.monotonic() if now is None else now
    at = _verified.get(listing_id)
    return at is not None and moment - at < VERIFIED_SECONDS


def mark_verified(listing_id: int, *, now: float | None = None) -> None:
    moment = time.monotonic() if now is None else now
    _verified[listing_id] = moment
    if len(_verified) > 5000:
        for key in [k for k, t in _verified.items() if moment - t >= VERIFIED_SECONDS]:
            _verified.pop(key, None)


def reset_memory() -> None:
    """Сбросить дебаунс, лимит и кэши «Все фото» (тесты; память процесса)."""
    _recent.clear()
    _albums_sent.clear()
    _shown.clear()
    _verified.clear()


def albums(urls: list[str]) -> list[list[str]]:
    """Режет галерею на альбомы по ALBUM_MAX.

    Хвост из одного фото альбомом не отправить (минимум 2) — тогда он
    уходит последним одиночным фото: вызывающий шлёт `send_photo`.
    """
    return [urls[start : start + ALBUM_MAX] for start in range(0, len(urls), ALBUM_MAX)]


class ListingGone(Exception):
    """Источник ответил, что объявления больше нет.

    `reason`/`status_id` — для `listing_status_history`, как у валидатора lalafo:
    `card_gone` — 404/410/417 или редирект mashina на главную, `card_inactive` —
    lalafo ответил 200, но со `status_id` снятого (8) или заблокированного (3).
    """

    def __init__(self, reason: str = "card_gone", status_id: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_id = status_id


def needs_recheck(
    last_seen_at: datetime | None, *, hours: float, now: datetime | None = None
) -> bool:
    """Давно не виденное объявление перед альбомом проверяем на источнике.

    Фото на CDN переживают объявление, а `is_active` отстаёт от жизни (mashina —
    сверка по выходным, lalafo — валидатор после 3 дней): без живой проверки бот
    прислал бы альбом машины, которой уже нет. Свежие пуши fresh-цикл видит
    каждые 10 минут — им лишняя секунда ни к чему. `hours <= 0` — проверка
    выключена.
    """
    if hours <= 0:
        return False
    if last_seen_at is None:
        return True
    if last_seen_at.tzinfo is None:  # SQLite в тестах отдаёт naive UTC
        last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    return moment - last_seen_at > timedelta(hours=hours)


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


# Телефон на странице mashina: атрибут `phone` в тех же данных Next.js
# (01.11.2026: `{"slug":"phone","name":"Телефон","value":"996…"}` и ещё раз
# с `value_text`/`value_json`). В ленте API номера нет (`user.phones` пуст).
_MASHINA_PHONE_MARKER = '\\"slug\\":\\"phone\\"'
# Значение бывает строкой (`"value":"996…"`) и списком (`"value":["996…"]`,
# `"value_json":["+996 …"]`) — второй вид у большинства страниц (01.11.2026:
# номер не находился ни у одной машины mashina в витрине).
_MASHINA_PHONE_VALUE_RE = re.compile(
    r'\\"value(?:_text|_json)?\\":\[?\\"([+\d][\d ()+-]{8,})\\"'
)


def mashina_phone_from_page(html: str) -> str | None:
    """Телефон продавца со страницы mashina (цифры) или None."""
    start = html.find(_MASHINA_PHONE_MARKER)
    while start != -1:
        match = _MASHINA_PHONE_VALUE_RE.search(html, start, start + 600)
        if match and (phone := normalize_phone(match.group(1))):
            return phone
        start = html.find(_MASHINA_PHONE_MARKER, start + 1)
    return None


@dataclass(slots=True)
class Card:
    """Что нажатие «Все фото» берёт со страницы/карточки источника."""

    urls: list[str]
    phone: str | None = None  # цифры; None — нет или скрыт продавцом
    phone_hidden: bool = False  # продавец скрыл номер (lalafo `hide_phone`)


def _fetch_mashina_page(url: str, timeout: float | None = None) -> str:
    settings = get_settings()
    with httpx.Client(
        timeout=settings.request_timeout_seconds if timeout is None else timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as client:
        response = client.get(url)
    # Снятое объявление mashina не отдаёт 404, а уводит редиректом на главную.
    if response.status_code in _GONE_STATUSES or "/details/" not in str(response.url):
        raise ListingGone
    response.raise_for_status()
    return response.text


def fetch_card(
    source_name: str, external_id: str, url: str, *, timeout: float | None = None
) -> Card:
    """Запасной путь: страница/карточка источника по нажатию. Синхронно (из потока).

    Один запрос даёт и галерею, и телефон продавца. `url` — публичная страница
    объявления (нужна mashina). `ListingGone` — снято. `timeout` — запрос по
    нажатию: короче общего и без повторов (человек не ждёт); None — общие
    настройки (прогрев витрины в фоне).
    """
    if source_name == MASHINA:
        page = _fetch_mashina_page(url, timeout)
        return Card(urls=mashina_gallery_from_page(page), phone=mashina_phone_from_page(page))
    if not can_fetch(source_name):
        return Card(urls=[])
    # Импорт здесь: парсер тянет httpx-клиент с прокси, а модуль импортируется
    # хендлерами при старте бота.
    from app.parsers.lalafo import LalafoParser
    from app.services.sync import LALAFO_DEAD_STATUS_IDS

    parser = (
        LalafoParser() if timeout is None else LalafoParser(timeout=timeout, retry_attempts=1)
    )
    try:
        detail = parser.fetch_details(external_id)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in _GONE_STATUSES:
            raise ListingGone from error
        raise
    finally:
        parser.close()
    detail = detail or {}
    # 200 — ещё не «живое»: снятое lalafo отдаёт со status_id 8 (страница сайта
    # при этом 410), как и разбирает валидатор карточек.
    status_id = detail.get("status_id")
    if status_id in LALAFO_DEAD_STATUS_IDS:
        raise ListingGone("card_inactive", status_id)
    hidden = bool(detail.get("hide_phone"))
    return Card(
        urls=extract_image_urls(detail),
        phone=None if hidden else normalize_phone(detail.get("mobile")),
        phone_hidden=hidden,
    )


def fetch_gallery(source_name: str, external_id: str, url: str) -> list[str]:
    """Только галерея (см. `fetch_card`)."""
    return fetch_card(source_name, external_id, url).urls
