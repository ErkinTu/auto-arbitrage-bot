from __future__ import annotations

import json
import logging
import re
import time
from decimal import Decimal
from typing import Any, ClassVar

import httpx

from app.core.config import get_settings
from app.parsers.base import FetchPage, ParserAdapter
from app.services.normalizer import NormalizedListing, normalize_lalafo_listing


logger = logging.getLogger(__name__)

# Категория марки зашита в SSR-страницу марки (__NEXT_DATA__), API дерева марок нет.
_CATEGORY_RE = re.compile(r'"searchRequestUrlFromServer":"[^"]*category_id=(\d+)')


class LalafoBrandNotFoundError(RuntimeError):
    """Не удалось разрешить марку в category_id lalafo (нет такой страницы марки)."""


class LalafoParser(ParserAdapter):
    source_name = "lalafo.kg"
    needs_detail_enrichment = True

    # Кэши на процесс (бот создает парсер каждый цикл): category_id марок
    # стабильны, перечитывать HTML страниц марок каждые 10 минут незачем.
    _category_cache: ClassVar[dict[str, int]] = {}
    _generic_category_id: ClassVar[int | None] = None

    def __init__(self, usd_rate_kgs: Decimal | None = None) -> None:
        # Курс НБКР для объявлений в сомах; None — конвертации не будет.
        self.usd_rate_kgs = usd_rate_kgs
        self.settings = get_settings()
        # Пауза карточек живёт на инстансе (один цикл): каждый ответ 429
        # удваивает её до конца работы парсера — вежливый бэкофф.
        self._detail_delay = self.settings.lalafo_detail_delay_seconds
        self.client = httpx.Client(
            timeout=self.settings.request_timeout_seconds,
            headers={"User-Agent": self.settings.user_agent},
            follow_redirects=True,
            # Cloudflare lalafo режет датацентровые IP — с сервера ходим
            # через резидентный прокси (см. LALAFO.md), локально — напрямую.
            proxy=self.settings.lalafo_proxy_url or None,
        )

    def close(self) -> None:
        self.client.close()

    # -- HTTP с ретраями (транспортные ошибки, 429, 5xx) --------------------

    def _get_with_retries(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        headers = {
            # Без этих заголовков API отвечает 404/451: он мультистрановый.
            "device": "pc",
            "country-id": "12",  # Кыргызстан
            "language": "ru_RU",
            "Accept": "application/json",
        }
        response: httpx.Response | None = None
        attempts = max(1, self.settings.request_retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = self.client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                if attempt == attempts:
                    raise
                wait_seconds = self.settings.request_retry_backoff_seconds * attempt
                logger.warning(
                    "lalafo request error url=%s attempt=%s/%s: %r; retrying in %.1fs",
                    url,
                    attempt,
                    attempts,
                    exc,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            if response.status_code < 400:
                break

            if response.status_code == 429:
                self._detail_delay *= 2
                logger.warning(
                    "lalafo 429: detail delay doubled to %.1fs until parser restart",
                    self._detail_delay,
                )

            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt == attempts:
                break

            wait_seconds = self.settings.request_retry_backoff_seconds * attempt
            logger.warning(
                "lalafo request failed status=%s url=%s attempt=%s/%s; retrying in %.1fs",
                response.status_code,
                url,
                attempt,
                attempts,
                wait_seconds,
            )
            time.sleep(wait_seconds)

        assert response is not None
        return response

    # -- Разрешение марки в category_id --------------------------------------

    @staticmethod
    def _slug_candidates(brand: str) -> list[str]:
        slug = re.sub(r"[^a-z0-9]+", "-", brand.lower()).strip("-")
        # У части марок страница живёт под слагом с постфиксом (honda, mercedes-benz).
        return [slug, f"{slug}-avtomobili-s-probegom"]

    def _category_id_from_page(self, path: str) -> int | None:
        url = f"{self.settings.lalafo_base_url}{path}"
        response = self._get_with_retries(url)
        if response.status_code >= 400:
            return None
        match = _CATEGORY_RE.search(response.text)
        return int(match.group(1)) if match else None

    def _get_generic_category_id(self) -> int | None:
        # Для неизвестного слага lalafo не отдаёт 404, а молча показывает общий
        # раздел «Продажа авто». Его id нужен, чтобы отличить марку от фолбэка.
        if LalafoParser._generic_category_id is None:
            LalafoParser._generic_category_id = self._category_id_from_page(
                "/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley"
            )
        return LalafoParser._generic_category_id

    def resolve_category_id(self, brand: str) -> int:
        cached = self._category_cache.get(brand)
        if cached is not None:
            return cached

        generic_id = self._get_generic_category_id()
        for slug in self._slug_candidates(brand):
            category_id = self._category_id_from_page(
                f"/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/{slug}"
            )
            if category_id is None or category_id == generic_id:
                continue
            self._category_cache[brand] = category_id
            logger.info("lalafo brand=%s slug=%s -> category_id=%s", brand, slug, category_id)
            return category_id

        raise LalafoBrandNotFoundError(f"lalafo: cannot resolve brand {brand!r} to a category id")

    # -- Данные ---------------------------------------------------------------

    def fetch_listings(
        self,
        *,
        brand: str,
        model: str | None,
        created_period: str | None,
        page: int,
        size: int,
        sort_by: str,
        order: str,
    ) -> FetchPage:
        # lalafo не умеет фильтр по периоду и серверную сортировку по дате создания:
        # sort_by=newest — единственный «свежий» режим, model/created_period игнорируются.
        category_id = self.resolve_category_id(brand)
        params: dict[str, Any] = {
            "category_id": category_id,
            "expand": "url",
            "page": page,
            "per-page": size,
            "sort_by": "newest",
        }
        response = self._get_with_retries(
            f"{self.settings.lalafo_base_url}/api/search/v3/ads/search", params=params
        )
        response.raise_for_status()
        data = response.json()
        time.sleep(self.settings.lalafo_feed_delay_seconds)
        items = [item for item in data.get("items", []) if item.get("id")]
        total = (data.get("_meta") or {}).get("totalCount")
        return FetchPage(items=items, total=total)

    def normalize(self, raw: dict[str, Any], *, fallback_make: str | None = None) -> NormalizedListing:
        return normalize_lalafo_listing(raw, fallback_make=fallback_make, usd_rate_kgs=self.usd_rate_kgs)

    def fetch_details(self, external_id: str | int) -> dict[str, Any]:
        """Карточка объявления: содержит params (модель, год, пробег, КПП и т.д.),
        которых нет в ленте."""
        response = self._get_with_retries(
            f"{self.settings.lalafo_base_url}/api/search/v3/feed/details/{external_id}",
            params={"expand": "url"},
        )
        # Пауза ДО raise_for_status: вежливый интервал держим и на 410 Gone
        # (валидатор проверяет карточки умерших объявлений сериями).
        time.sleep(self._detail_delay)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "id" not in data:
            raise ValueError(f"lalafo: unexpected details payload for id={external_id}: {json.dumps(data)[:200]}")
        return data
