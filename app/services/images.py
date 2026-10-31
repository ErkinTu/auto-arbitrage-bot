"""Главное фото объявления из сырого ответа источника.

Без зависимостей от БД: зовут нормализатор (колонки `listings.image_url` и
`listings.image_urls`), админка и API (`vehicle_summary`).
"""

from __future__ import annotations

from typing import Any


# Ключи ссылки внутри элемента галереи — в порядке предпочтения. mashina отдаёт
# `thumb/medium/big` (WebP; `medium` — 640 px, для карточки в Telegram хватает),
# lalafo — `original_url/thumbnail_url` (JPEG) и их `*_webp_url`-двойники: JPEG
# раньше WebP, оригинал раньше превью (превью lalafo мелкое). Проверено на живых
# лентах 22.10.2026: Telegram принимает по ссылке оба формата.
_IMAGE_URL_KEYS = (
    "url",
    "src",
    "image",
    "large",
    "medium",
    "original_url",
    "big",
    "thumbnail_url",
    "original_webp_url",
    "thumbnail_webp_url",
    "small",
    "thumb",
)


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _gallery_item_url(item: Any) -> str | None:
    if _is_http_url(item):
        return item
    if isinstance(item, dict):
        for url_key in _IMAGE_URL_KEYS:
            if _is_http_url(item.get(url_key)):
                return item[url_key]
    return None


# Потолок длины галереи в БД: у mashina до 12, у lalafo до 10 в ленте и 11+ в
# карточке (замер 31.10.2026). Альбом Telegram — до 10, больше двух альбомов
# одной машины никто не листает.
MAX_GALLERY = 20


def extract_image_urls(payload: dict[str, Any] | None) -> list[str]:
    """Все фото объявления: главное первым, без дублей, не больше MAX_GALLERY.

    Ключ ссылки у каждого фото — тот же, что у главного (`_IMAGE_URL_KEYS`):
    кнопка «📷 Все фото» шлёт альбом того же вида, что и карточка.
    """
    if not payload:
        return []
    for key in ("photos", "images"):
        gallery = payload.get(key)
        if not isinstance(gallery, list) or not gallery:
            continue
        main = [item for item in gallery if isinstance(item, dict) and item.get("is_main")]
        rest = [item for item in gallery if not (isinstance(item, dict) and item.get("is_main"))]
        urls: list[str] = []
        for item in main + rest:
            url = _gallery_item_url(item)
            if url is not None and url not in urls:
                urls.append(url)
            if len(urls) >= MAX_GALLERY:
                break
        if urls:
            return urls
    single = extract_image_url(payload)
    return [single] if single else []


def extract_image_url(payload: dict[str, Any] | None) -> str | None:
    """Главное фото объявления из сырого ответа источника (mashina или lalafo).

    Общая для бота (карточка с фото), админки и API. У lalafo главное фото
    помечено `is_main` и не обязано стоять первым; без пометки — первое.
    """
    if not payload:
        return None
    # Плоские поля — на случай, если источник отдаст одно фото без галереи.
    for key in ("image_url", "photo", "preview_image"):
        if _is_http_url(payload.get(key)):
            return payload[key]
    for key in ("photos", "images"):
        gallery = payload.get(key)
        if not isinstance(gallery, list) or not gallery:
            continue
        main = [item for item in gallery if isinstance(item, dict) and item.get("is_main")]
        for item in main + gallery:
            url = _gallery_item_url(item)
            if url is not None:
                return url
    return None
