"""Фото и видео объявления по открытию страницы авто на сайте.

Продавец перезалил фото — старые файлы CDN удаляет (404), а обход обновит
галерею, только когда снова встретит объявление. Видео (YouTube) в ленте API
mashina нет. С 12.11.2026 и то и другое приходит с живой проверкой «не снято ли»
(services/liveness.py): API карточки mashina отдаёт и статус, и галерею, и
`video_url` (~5 КБ вместо ~450 КБ страницы). Страница авто зовёт
`GET /public/listings/{id}/media`: обычное открытие — проверка, если не
проверяли `LIVENESS_OPEN_MINUTES`; кадры не грузятся (`broken=1`) — если не
проверяли `BROKEN_RECHECK`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)

BROKEN_RECHECK = timedelta(minutes=5)

# Ссылка продавца на ролик в любом виде YouTube; подсказки формы («Вставьте ссылку…») — не ссылки.
_YOUTUBE_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/|live/|v/))([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])"
)


def youtube_id(url: str | None) -> str | None:
    """Id ролика YouTube из ссылки продавца; не YouTube / мусор — None."""
    if not url:
        return None
    match = _YOUTUBE_ID_RE.search(url)
    return match.group(1) if match else None


def refresh_media(session: Session, listing_id: int, *, broken: bool = False, now: datetime | None = None) -> bool:
    """Проверить объявление у источника, если пора. True — спрашивали источник.

    LookupError — объявления нет. Сбой источника — отдаём, что есть."""
    from app.core.config import get_settings
    from app.services.liveness import verify_one

    window = BROKEN_RECHECK if broken else timedelta(minutes=get_settings().liveness_open_minutes)
    return verify_one(session, listing_id, window=window, now=now)
