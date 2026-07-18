"""Реестр источников: имя -> base_url и построение парсера.

lalafo-парсер получает курс НБКР на сегодня (для цен в сомах);
недоступность nbkr.kg сбор не останавливает — конвертация просто пропускается.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.parsers.base import ParserAdapter
from app.parsers.lalafo import LalafoParser
from app.parsers.mashina import MashinaParser
from app.services.exchange import ensure_fresh_usd_rate

MASHINA = "mashina.kg"
LALAFO = "lalafo.kg"
SOURCE_NAMES = (MASHINA, LALAFO)


def source_base_url(source_name: str) -> str:
    settings = get_settings()
    if source_name == MASHINA:
        return settings.mashina_api_url
    if source_name == LALAFO:
        return settings.lalafo_base_url
    raise ValueError(f"unknown source: {source_name!r}")


def build_parser(source_name: str, session: Session) -> ParserAdapter:
    if source_name == MASHINA:
        return MashinaParser()
    if source_name == LALAFO:
        return LalafoParser(usd_rate_kgs=ensure_fresh_usd_rate(session))
    raise ValueError(f"unknown source: {source_name!r}")


def listing_public_url(slug: str | None, external_id: str) -> str:
    """Публичная ссылка на объявление.

    У lalafo source_slug — это путь с ведущим «/» («/bishkek/ads/…», см.
    normalize_lalafo_listing), у mashina — слаг без слеша. Форма слага
    детерминирована нашими же нормализаторами."""
    settings = get_settings()
    if slug and slug.startswith("/"):
        return f"{settings.lalafo_base_url}{slug}"
    if slug:
        return f"https://www.mashina.kg/details/{slug}/"
    return f"https://www.mashina.kg/details/{external_id}/"
