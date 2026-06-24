from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.services.catalog import find_model


@dataclass(slots=True)
class NormalizedListing:
    external_id: str
    title: str | None
    slug: str | None
    make: str | None
    model: str | None
    generation: str | None
    year: int | None
    mileage_km: int | None
    body_type: str | None
    engine_volume: Decimal | None
    fuel_type: str | None
    gearbox: str | None
    drive_type: str | None
    steering_wheel: str | None
    city: str | None
    customs_status: str
    seller_type: str | None
    is_verified_seller: bool
    is_official_dealer: bool
    price_amount: Decimal | None
    price_currency: str | None
    price_kgs: Decimal | None
    price_usd: Decimal | None
    status: str
    source_created_at: datetime | None
    source_upped_at: datetime | None
    safe_payload: dict[str, Any]

    def analytics_payload(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("safe_payload", None)
        return data

    @property
    def payload_hash(self) -> str:
        raw = json.dumps(self.analytics_payload(), ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _attributes_by_slug(raw: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for attr in raw.get("attributes", []) or []:
        slug = attr.get("slug")
        if not slug:
            continue
        value = attr.get("value_number")
        if value is None:
            value = attr.get("value_text")
        if value is None and isinstance(attr.get("value_json"), dict):
            value = attr["value_json"].get("name") or attr["value_json"].get("value")
        result[slug] = value
    return result


def _parse_title(
    title: str | None,
    fallback_make: str | None = None,
) -> tuple[str | None, str | None, str | None, int | None]:
    """Extract vehicle data from flexible listing-title formats.

    The API filter already tells us the requested brand. When a title is non-standard,
    use that brand as a safe fallback rather than discarding the whole listing.
    """
    if not title:
        return fallback_make, None, None, None

    clean = " ".join(title.split())
    clean = re.sub(r"^(?:Продажа|Продам|Продаю)\s+", "", clean, flags=re.IGNORECASE)

    make = fallback_make
    vehicle = clean

    if fallback_make:
        brand_match = re.search(rf"(?<!\w){re.escape(fallback_make)}(?!\w)", clean, flags=re.IGNORECASE)
        if brand_match:
            vehicle = clean[brand_match.end() :].strip(" -:|")
    else:
        parts = clean.split(maxsplit=1)
        if parts:
            make = parts[0]
            vehicle = parts[1] if len(parts) > 1 else ""

    # Prefer the last 4-digit year in the title. Attribute `year` still has priority.
    year_matches = list(re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", vehicle))
    year = int(year_matches[-1].group(1)) if year_matches else None
    if year_matches:
        vehicle = vehicle[: year_matches[-1].start()].strip(" -:|,")

    model, generation = find_model(make, vehicle) if make and vehicle else (None, None)
    return make, model, generation, year


def _customs_status(attributes: dict[str, Any]) -> str:
    for key in ("customs_status", "customs_cleared", "rastamozhka", "customs"):
        value = attributes.get(key)
        if value is None:
            continue
        text = str(value).lower()
        if any(word in text for word in ("не растамож", "без растамож", "not cleared")):
            return "not_cleared"
        if any(word in text for word in ("растамож", "cleared")):
            return "cleared"
    return "unknown"


def _safe_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only technical listing data. Never retain seller identity/contact data."""
    safe_attributes = []
    for attr in raw.get("attributes", []) or []:
        safe_attributes.append(
            {
                "slug": attr.get("slug"),
                "value_text": attr.get("value_text"),
                "value_number": attr.get("value_number"),
                "value_json": attr.get("value_json"),
            }
        )
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "slug": raw.get("slug"),
        "prices": raw.get("prices", []),
        "status": raw.get("status"),
        "created_at": raw.get("created_at"),
        "upped_at": raw.get("upped_at"),
        "attributes": safe_attributes,
        "dealer": {"is_official": bool((raw.get("dealer") or {}).get("is_official"))},
        "is_verified_seller": bool((raw.get("user") or {}).get("is_verified_seller")),
    }


def normalize_mashina_listing(
    raw: dict[str, Any],
    *,
    fallback_make: str | None = None,
) -> NormalizedListing:
    attrs = _attributes_by_slug(raw)
    parsed_make, parsed_model, parsed_generation, parsed_year = _parse_title(
        raw.get("title"),
        fallback_make=fallback_make,
    )

    original_price = next((price for price in raw.get("prices", []) if price.get("is_original")), None)
    kgs_price = next((price for price in raw.get("prices", []) if price.get("currency") == "KGS"), None)
    usd_price = next((price for price in raw.get("prices", []) if price.get("currency") == "USD"), None)

    dealer = raw.get("dealer") or {}
    user = raw.get("user") or {}
    seller_type = "dealer" if dealer else "private"

    return NormalizedListing(
        external_id=str(raw["id"]),
        title=raw.get("title"),
        slug=raw.get("slug"),
        make=parsed_make,
        model=parsed_model,
        generation=parsed_generation,
        year=_as_int(attrs.get("year")) or parsed_year,
        mileage_km=_as_int(attrs.get("mileage")),
        body_type=attrs.get("body_type"),
        engine_volume=_as_decimal(attrs.get("engine_volume")),
        fuel_type=attrs.get("fuel_type"),
        gearbox=attrs.get("gearbox"),
        drive_type=attrs.get("drive_type"),
        steering_wheel=attrs.get("steering_wheel"),
        city=attrs.get("city"),
        customs_status=_customs_status(attrs),
        seller_type=seller_type,
        is_verified_seller=bool(user.get("is_verified_seller")),
        is_official_dealer=bool(dealer.get("is_official")),
        price_amount=_as_decimal((original_price or {}).get("amount")),
        price_currency=(original_price or {}).get("currency"),
        price_kgs=_as_decimal((kgs_price or {}).get("amount")),
        price_usd=_as_decimal((usd_price or {}).get("amount")),
        status=raw.get("status") or "active",
        source_created_at=_parse_datetime(raw.get("created_at")),
        source_upped_at=_parse_datetime(raw.get("upped_at")),
        safe_payload=_safe_payload(raw),
    )
