"""Описание продавца для страницы авто на сайте (заметка владельца 11.11.2026).

В ленте источников описания нет (у mashina вообще, у lalafo — только в
карточке), поэтому берём его **по запросу** — когда человек открыл страницу
авто — и храним в `listings.description` на `REFRESH_AFTER` (повтор — только
после этого срока):

- **mashina** — у API нет карточки, берём страницу объявления (~450 КБ, тот
  же запрос, что «Все фото» в боте): поле `description` рядом с `title` в
  экранированных данных Next.js. Проверено 11.11.2026.
- **lalafo** — карточка API (`fetch_details`, на проде через прокси).

Текст — чужие данные: на сайте только как текст (никакого `v-html`),
**телефоны маскируем** — номер продавца у нас персональные данные
(`CLAUDE.md § Номер продавца`), а в описаниях его пишут часто. Длина — до
`MAX_LENGTH`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Listing, Source

logger = logging.getLogger(__name__)

REFRESH_AFTER = timedelta(days=3)
MAX_LENGTH = 4000
PHONE_MASK = "[номер скрыт]"

_MASHINA_RE = re.compile(r'\\"description\\":\\"((?:[^"\\]|\\.)*?)\\",\\"category_id\\"')
_PHONE_RUN_RE = re.compile(r"\+?\d[\d\s\-().]{7,}\d")
_BLANKS_RE = re.compile(r"\n{3,}")


def mashina_description_from_page(html: str) -> str | None:
    """Описание объявления из страницы mashina (данные Next.js экранированы дважды)."""
    match = _MASHINA_RE.search(html)
    if not match:
        return None
    escaped = match.group(1)
    try:
        # Внешний слой — строка JS (\" и \\), внутренний — JSON-строка.
        inner = escaped.replace('\\\\', '\\').replace('\\"', '"')
        return json.loads(f'"{inner}"')
    except (ValueError, json.JSONDecodeError):
        return None


def _mask_phone(match: re.Match) -> str:
    digits = sum(ch.isdigit() for ch in match.group(0))
    return PHONE_MASK if 9 <= digits <= 13 else match.group(0)


def clean_description(text: str | None) -> str | None:
    """Пробелы, пустые строки, маска телефонов, потолок длины. Пусто → None."""
    if not text:
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANKS_RE.sub("\n\n", text).strip()
    text = _PHONE_RUN_RE.sub(_mask_phone, text)
    if len(text) > MAX_LENGTH:
        text = text[:MAX_LENGTH].rstrip() + "…"
    return text or None


def _fetch(source_name: str, listing: Listing, url: str) -> str | None:
    # Импорт здесь: модуль бота тянет httpx-клиенты и настройки прокси.
    from app.bot.photos import _fetch_mashina_page, can_fetch
    from app.services.sources import LALAFO, MASHINA

    if source_name == MASHINA:
        return mashina_description_from_page(_fetch_mashina_page(url, timeout=8))
    if source_name == LALAFO and can_fetch(source_name):
        from app.parsers.lalafo import LalafoParser

        parser = LalafoParser(timeout=8, retry_attempts=1)
        try:
            return (parser.fetch_details(listing.external_id) or {}).get("description")
        finally:
            parser.close()
    return None


def listing_description(session: Session, listing_id: int, *, now: datetime | None = None) -> dict:
    """Описание объявления: из базы, если свежее, иначе — у источника (и в базу)."""
    from app.services.sources import listing_public_url

    now = now or datetime.now(timezone.utc)
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise LookupError("Объявление не найдено")
    fetched = listing.description_fetched_at
    if fetched is not None and fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if fetched is not None and now - fetched < REFRESH_AFTER or not listing.is_active:
        return {"text": listing.description, "fetched_at": fetched}
    source = session.get(Source, listing.source_id)
    try:
        raw = _fetch(source.name if source else "", listing, listing_public_url(listing.source_slug, listing.external_id))
    except Exception:  # источник недоступен — отдаём, что было, повторим позже
        logger.warning("description fetch failed for listing %s", listing_id, exc_info=True)
        return {"text": listing.description, "fetched_at": fetched}
    listing.description = clean_description(raw)
    listing.description_fetched_at = now
    session.commit()
    return {"text": listing.description, "fetched_at": now}
