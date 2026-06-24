from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class FetchPage:
    items: list[dict[str, Any]]
    total: int | None = None


class ParserAdapter(ABC):
    """A source adapter only fetches raw data. Database logic stays outside it."""

    source_name: str

    @abstractmethod
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
        raise NotImplementedError
