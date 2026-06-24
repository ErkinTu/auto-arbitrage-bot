from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.core.config import get_settings
from app.parsers.base import FetchPage, ParserAdapter


class MashinaParser(ParserAdapter):
    source_name = "mashina.kg"

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

        response = self.client.get(self.settings.mashina_api_url, params=params)
        response.raise_for_status()
        data = response.json()
        time.sleep(self.settings.request_delay_seconds)
        return FetchPage(items=data.get("items", []), total=data.get("total"))
