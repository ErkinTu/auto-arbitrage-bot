from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from app.core.config import get_settings
from app.parsers.base import FetchPage, ParserAdapter
from app.services.normalizer import NormalizedListing, normalize_mashina_listing


logger = logging.getLogger(__name__)


class MashinaParser(ParserAdapter):
    source_name = "mashina.kg"

    def normalize(self, raw: dict[str, Any], *, fallback_make: str | None = None) -> NormalizedListing:
        return normalize_mashina_listing(raw, fallback_make=fallback_make)

    def __init__(self) -> None:
        self.settings = get_settings()
        self.client = httpx.Client(
            timeout=self.settings.request_timeout_seconds,
            headers={"User-Agent": self.settings.user_agent, "Accept": "application/json"},
        )

    def close(self) -> None:
        self.client.close()

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
        attributes: dict[str, Any] = {"make": {"in": brand}}
        if model:
            # Verify this attribute name in browser Network requests before relying on model filtering.
            attributes["model"] = {"in": model}

        params: dict[str, Any] = {
            "category_id": 1,
            "groups": json.dumps([{"attributes": attributes}], ensure_ascii=False, separators=(",", ":")),
            "sort_by": sort_by,
            "order": order,
            "page": page,
            "size": size,
        }
        if created_period:
            params["created_period"] = created_period
        return self._get_page(params, page)

    def fetch_fuel_page(
        self,
        fuel_value: str,
        *,
        page: int,
        size: int = 100,
        sort_by: str = "created_at",
        order: str = "desc",
    ) -> FetchPage:
        """Страница объявлений ОДНОГО типа топлива, без фильтра по марке.

        В ленте mashina атрибута топлива нет, но API по нему фильтрует
        (`fuel_type: {"in": "гибрид"}`, значения — `services/fuel.py::
        MASHINA_FUEL_VALUES`). Так размечаем `listings.fuel_group`
        (`services/fuel_labeling.py`). `size` у API — не больше 100 (200 → 422).
        """
        attributes = {"fuel_type": {"in": fuel_value}}
        params: dict[str, Any] = {
            "category_id": 1,
            "groups": json.dumps([{"attributes": attributes}], ensure_ascii=False, separators=(",", ":")),
            "sort_by": sort_by,
            "order": order,
            "page": page,
            "size": size,
        }
        return self._get_page(params, page)

    def _get_page(self, params: dict[str, Any], page: int) -> FetchPage:
        response: httpx.Response | None = None
        attempts = max(1, self.settings.request_retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = self.client.get(self.settings.mashina_api_url, params=params)
            except httpx.TransportError as exc:
                # Connection drops / timeouts (flaky network) are transient.
                if attempt == attempts:
                    raise
                wait_seconds = self.settings.request_retry_backoff_seconds * attempt
                logger.warning(
                    "mashina request error page=%s attempt=%s/%s: %r; retrying in %.1fs",
                    page,
                    attempt,
                    attempts,
                    exc,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            if response.status_code < 400:
                break

            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt == attempts:
                break

            wait_seconds = self.settings.request_retry_backoff_seconds * attempt
            logger.warning(
                "mashina request failed status=%s page=%s attempt=%s/%s; retrying in %.1fs",
                response.status_code,
                page,
                attempt,
                attempts,
                wait_seconds,
            )
            time.sleep(wait_seconds)

        assert response is not None
        response.raise_for_status()
        data = response.json()
        time.sleep(self.settings.request_delay_seconds)
        return FetchPage(items=data.get("items", []), total=data.get("total"))
