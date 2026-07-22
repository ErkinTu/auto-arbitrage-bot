from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.services.catalog import parse_vehicle_part, split_listing_year_suffix

# Mileage above this (km) is treated as implausible. Such values also overflow the
# INTEGER mileage column (e.g. a bogus 2_394_664_666 km) and would abort the sync,
# so the raw number is dropped after the quality flag is recorded.
IMPLAUSIBLE_MILEAGE_KM = 2_500_000

# Цена целого авто ниже этого порога (USD) — мусор ввода (взнос рассрочки,
# «180 сом» и т.п.). Такие объявления сохраняем, но помечаем price_implausibly_low:
# критический флаг исключает их из аналитики и уведомлений «ниже рынка».
MIN_PLAUSIBLE_PRICE_USD = 500

# Верхний предохранитель: цена выше этого (USD) — заведомо невозможная (плейсхолдеры
# $999 999 999, битый ввод, переполнения). Порог намеренно высокий — реальный верх
# рынка ~$500k (заказные G-Class AMG, Maybach), цель — только явный мусор. Помечаем
# price_implausibly_high (критический флаг): выпадает и из статистики, и из бота, и
# из /estimate, и с фронта — даже в когорте из одной машины, где отсев по группе бессилен.
MAX_PLAUSIBLE_PRICE_USD = 2_000_000


@dataclass(slots=True)
class NormalizedListing:
    external_id: str
    title: str | None
    slug: str | None
    make: str | None
    model: str | None
    generation: str | None
    source_make_text: str | None
    source_model_text: str | None
    source_generation_text: str | None
    normalization_status: str
    normalization_confidence: str
    quality_flags: list[str]
    analytics_eligible: bool
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


@dataclass(frozen=True, slots=True)
class ParsedTitle:
    make: str | None
    source_make_text: str | None
    source_model_text: str | None
    source_generation_text: str | None
    suggested_model: str | None
    suggested_generation: str | None
    year: int | None


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
    except (ArithmeticError, TypeError, ValueError):
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


def _parse_title(title: str | None, fallback_make: str | None = None) -> ParsedTitle:
    """Extract source fields only. Canonical DB resolution happens later in catalog_service."""
    if not title:
        return ParsedTitle(fallback_make, fallback_make, None, None, None, None, None)

    clean = " ".join(title.split())
    clean = re.sub(r"^(?:Продажа|Продам|Продаю)\s+", "", clean, flags=re.IGNORECASE)

    make = fallback_make
    source_make_text = fallback_make
    vehicle = clean

    if fallback_make:
        brand_match = re.search(
            rf"(?<!\w){re.escape(fallback_make)}(?!\w)",
            clean,
            flags=re.IGNORECASE,
        )
        if brand_match:
            source_make_text = clean[brand_match.start() : brand_match.end()]
            vehicle = clean[brand_match.end() :].strip(" -:|")
    else:
        parts = clean.split(maxsplit=1)
        if parts:
            make = parts[0]
            source_make_text = parts[0]
            vehicle = parts[1] if len(parts) > 1 else ""

    vehicle, parsed_year = split_listing_year_suffix(vehicle)

    parts = parse_vehicle_part(make, vehicle) if make and vehicle else None
    return ParsedTitle(
        make=make,
        source_make_text=source_make_text,
        source_model_text=parts.source_model_text if parts else None,
        source_generation_text=parts.source_generation_text if parts else None,
        suggested_model=parts.suggested_model if parts else None,
        suggested_generation=parts.suggested_generation if parts else None,
        year=parsed_year,
    )


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


def _quality_flags(*, year: int | None, parsed_year: int | None, mileage_km: int | None) -> list[str]:
    flags: list[str] = []
    if year and parsed_year and year != parsed_year:
        flags.append("year_title_conflict")
    if mileage_km is not None:
        if mileage_km > IMPLAUSIBLE_MILEAGE_KM:
            flags.append("mileage_implausibly_high")
        current_year = datetime.now().year
        if year and current_year - year >= 6 and 0 <= mileage_km <= 1_000:
            flags.append("mileage_unusually_low")
    return flags


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


# --- lalafo.kg -------------------------------------------------------------

# Имена параметров карточки lalafo (русские, как отдаёт API). Матчим по префиксу:
# «Пробег (км.)» и подобные могут менять хвост.
_LALAFO_PARAM_PREFIXES = {
    "model": ("Модель",),
    "year": ("Год",),
    "mileage": ("Пробег",),
    "fuel": ("Топливо",),
    "body": ("Кузов",),
    "gearbox": ("Коробка",),
    "steering": ("Руль",),
    "drive": ("Привод",),
    "engine_volume": ("Объем двигателя", "Объём двигателя"),
    "customs": ("Растаможка",),
    "registration_country": ("Страна учета", "Страна учёта"),
}

_LALAFO_TITLE_YEAR_RE = re.compile(r"(19|20)\d{2}\s*г")


def _lalafo_params(raw: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for param in raw.get("params") or []:
        name = str(param.get("name") or "")
        value = param.get("value")
        if value is None or value == "":
            continue
        for field, prefixes in _LALAFO_PARAM_PREFIXES.items():
            if field not in values and any(name.startswith(prefix) for prefix in prefixes):
                values[field] = str(value)
    return values


def _parse_lalafo_title(title: str | None, fallback_make: str | None) -> tuple[str | None, int | None]:
    """Заголовок lalafo структурирован: «Toyota Camry: 2016 г., 2.5 л, Вариатор, …».
    Возвращает (текст модели, год из заголовка)."""
    if not title:
        return None, None
    clean = " ".join(title.split())

    year_match = _LALAFO_TITLE_YEAR_RE.search(clean)
    year = int(year_match.group(0)[:4]) if year_match else None

    head = clean.split(":", 1)[0].strip()
    if fallback_make:
        brand_match = re.search(rf"(?<!\w){re.escape(fallback_make)}(?!\w)", head, flags=re.IGNORECASE)
        if brand_match:
            head = head[brand_match.end() :]
    model = head.strip(" -:|,")
    return (model or None), year


def _lalafo_customs_status(customs_value: str | None) -> str:
    if not customs_value:
        return "unknown"
    text = customs_value.lower()
    if "не растамож" in text or "без растамож" in text:
        return "not_cleared"
    if "растамож" in text:
        return "cleared"
    return "unknown"


def _lalafo_safe_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Только технические данные объявления; контакты/личность продавца не храним."""
    national_price = raw.get("national_price") or {}
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "url": raw.get("url"),
        "price": raw.get("price"),
        "currency": raw.get("currency"),
        "national_price_amount": national_price.get("price"),
        "city": raw.get("city"),
        "status_id": raw.get("status_id"),
        "created_time": raw.get("created_time"),
        "updated_time": raw.get("updated_time"),
        "is_vip": bool(raw.get("is_vip")),
        "params": [
            {"name": param.get("name"), "value": param.get("value")}
            for param in raw.get("params") or []
        ],
    }


def _from_unix(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def normalize_lalafo_listing(
    raw: dict[str, Any],
    *,
    fallback_make: str | None = None,
    usd_rate_kgs: Decimal | None = None,
) -> NormalizedListing:
    """`raw` — карточка (`fetch_details`) или элемент ленты (тогда params пустые
    и модель/год берутся из структурированного заголовка).

    `usd_rate_kgs` — курс НБКР (KGS за 1 USD): цена в USD вычисляется ТОЛЬКО
    для объявлений без собственного USD (продавец указал сомы). Такая цена
    помечается флагом usd_converted — sync не считает её смену курса
    изменением цены продавцом."""
    params = _lalafo_params(raw)
    title_model, title_year = _parse_lalafo_title(raw.get("title"), fallback_make)

    model_text = params.get("model") or title_model
    # «Другая модель» — заглушка lalafo, а не модель: оставляем пустым,
    # чтобы объявление ушло в missing_model, а не плодило ложные alias-issues.
    if model_text and model_text.strip().lower() in {"другая модель", "другая"}:
        model_text = title_model if params.get("model") else None
        if model_text and model_text.strip().lower() in {"другая модель", "другая"}:
            model_text = None
    year = _as_int(params.get("year")) or title_year
    mileage_km = _as_int(params.get("mileage"))
    flags = _quality_flags(year=year, parsed_year=title_year, mileage_km=mileage_km)
    if mileage_km is not None and mileage_km > IMPLAUSIBLE_MILEAGE_KM:
        mileage_km = None

    currency = raw.get("currency")
    price = _as_decimal(raw.get("price"))
    national_kgs = _as_decimal((raw.get("national_price") or {}).get("price"))
    price_usd = price if currency == "USD" else None
    price_kgs = price if currency == "KGS" else national_kgs
    if price_usd is None and price_kgs is not None and usd_rate_kgs and usd_rate_kgs > 0:
        price_usd = (price_kgs / usd_rate_kgs).quantize(Decimal("1"))
        flags.append("usd_converted")
    if price_usd is not None and price_usd < MIN_PLAUSIBLE_PRICE_USD:
        flags.append("price_implausibly_low")
    if price_usd is not None and price_usd > MAX_PLAUSIBLE_PRICE_USD:
        flags.append("price_implausibly_high")

    user = raw.get("user") or {}

    return NormalizedListing(
        external_id=str(raw["id"]),
        title=raw.get("title"),
        slug=raw.get("url"),
        make=fallback_make,
        model=model_text,
        generation=None,
        source_make_text=fallback_make,
        source_model_text=model_text,
        source_generation_text=None,
        normalization_status="pending_catalog_resolution",
        normalization_confidence="pending",
        quality_flags=flags,
        analytics_eligible=False,
        year=year,
        mileage_km=mileage_km,
        body_type=params.get("body"),
        engine_volume=_as_decimal(params.get("engine_volume")),
        fuel_type=params.get("fuel"),
        gearbox=params.get("gearbox"),
        drive_type=params.get("drive"),
        steering_wheel=params.get("steering"),
        city=raw.get("city"),
        customs_status=_lalafo_customs_status(params.get("customs")),
        seller_type="dealer" if user.get("pro") else "private",
        is_verified_seller=False,
        is_official_dealer=False,
        price_amount=price,
        price_currency=currency,
        price_kgs=price_kgs,
        price_usd=price_usd,
        status="active",
        source_created_at=_from_unix(raw.get("created_time")),
        source_upped_at=_from_unix(raw.get("updated_time")),
        safe_payload=_lalafo_safe_payload(raw),
    )


def normalize_mashina_listing(
    raw: dict[str, Any],
    *,
    fallback_make: str | None = None,
) -> NormalizedListing:
    attrs = _attributes_by_slug(raw)
    parsed = _parse_title(raw.get("title"), fallback_make=fallback_make)

    original_price = next((price for price in raw.get("prices", []) if price.get("is_original")), None)
    kgs_price = next((price for price in raw.get("prices", []) if price.get("currency") == "KGS"), None)
    usd_price = next((price for price in raw.get("prices", []) if price.get("currency") == "USD"), None)

    dealer = raw.get("dealer") or {}
    user = raw.get("user") or {}
    seller_type = "dealer" if dealer else "private"
    year = _as_int(attrs.get("year")) or parsed.year
    mileage_km = _as_int(attrs.get("mileage"))
    flags = _quality_flags(year=year, parsed_year=parsed.year, mileage_km=mileage_km)
    if mileage_km is not None and mileage_km > IMPLAUSIBLE_MILEAGE_KM:
        # Keep the quality flag but drop the bogus value so it neither pollutes
        # analytics nor overflows the INTEGER column.
        mileage_km = None

    price_usd = _as_decimal((usd_price or {}).get("amount"))
    if price_usd is not None and price_usd > MAX_PLAUSIBLE_PRICE_USD:
        flags.append("price_implausibly_high")

    return NormalizedListing(
        external_id=str(raw["id"]),
        title=raw.get("title"),
        slug=raw.get("slug"),
        make=parsed.make,
        model=parsed.suggested_model,
        generation=parsed.suggested_generation,
        source_make_text=parsed.source_make_text,
        source_model_text=parsed.source_model_text,
        source_generation_text=parsed.source_generation_text,
        normalization_status="pending_catalog_resolution",
        normalization_confidence="pending",
        quality_flags=flags,
        analytics_eligible=False,
        year=year,
        mileage_km=mileage_km,
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
        price_usd=price_usd,
        status=raw.get("status") or "active",
        source_created_at=_parse_datetime(raw.get("created_at")),
        source_upped_at=_parse_datetime(raw.get("upped_at")),
        safe_payload=_safe_payload(raw),
    )
