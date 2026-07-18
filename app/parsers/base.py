from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.normalizer import NormalizedListing


@dataclass(slots=True)
class FetchPage:
    items: list[dict[str, Any]]
    total: int | None = None


class ParserAdapter(ABC):
    """A source adapter only fetches raw data and maps it to NormalizedListing.
    Database logic stays outside it."""

    source_name: str
    # Источник отдаёт в ленте неполные данные: перед сохранением НОВОГО
    # объявления sync дотягивает карточку через fetch_details.
    needs_detail_enrichment: bool = False

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

    @abstractmethod
    def normalize(self, raw: dict[str, Any], *, fallback_make: str | None = None) -> NormalizedListing:
        """Convert this source's raw payload to the unified NormalizedListing."""
        raise NotImplementedError

    def fetch_details(self, external_id: str | int) -> dict[str, Any]:
        """Full ad payload; required when needs_detail_enrichment is True."""
        raise NotImplementedError
