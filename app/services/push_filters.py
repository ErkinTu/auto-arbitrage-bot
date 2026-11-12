"""Фильтры уведомлений (пушей бота) с сайта — профиль «Мои уведомления».

Решение владельца 12.11.2026: фильтр уведомлений — это фильтр БОТА
(`user_filters`), отдельный от большого фильтра каталога на сайте. Источник
истины — сервер: сайт и бот правят одни и те же строки, а CRUD идёт через те же
функции `bot/db.py` (лимит, дубли, «правка = UPDATE той же строки»). Здесь —
то, чего в боте нет:

* `validate_spec` — проверка ввода с сайта (бот защищён своей панелью: в ней
  нельзя нажать то, чего нет; сайт — JSON, в нём можно прислать что угодно);
* `preview` — «сколько прислал бы этот фильтр за 7 дней». Правила — как у
  пушей (`matching.match_new_listings` / `preview_for_filter`): свежая
  публикация или снижение цены, только с настоящим фото, без неправдоподобной
  скидки, «ниже рынка» по той же оценке рынка (`batch_market_estimates` =
  `matching.market_estimate`, закреплено тестом), чёрный список человека.
  Отличия честно приблизительные: потолок пушей за цикл и отставание дешёвого
  тарифа не моделируются, а «ниже рынка» считается по сегодняшней статистике.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.bot.matching import filter_matches, has_real_photo, is_plausible_discount, passes_below_market
from app.core.config import get_settings
from app.models import BotUser, Brand, CarModel, Listing, ListingStatusHistory, UserFilter
from app.services.blocklist import load_block_sets
from app.services.filter_brands import MAX_FILTER_BRANDS, pack_brand_ids
from app.services.filter_presets import load_brand_groups, min_year_for_age
from app.services.fuel import FUEL_GROUPS, pack_fuel_groups
from app.services.normalizer import MIN_PLAUSIBLE_PRICE_USD
from app.services.public_feed import batch_market_estimates, listings_by_ids
from app.services.regions import REGION_LABELS

# Границы — как у панели бота (`bot/filter_panel.py`): что бот не принял бы
# текстом, не принимаем и с сайта.
MIN_YEAR = 1950
MAX_MILEAGE_KM = 1_500_000
MAX_PRICE_USD = 5_000_000
MAX_AGE_YEARS = 30
# Пороги «ниже рынка»: кнопки бота (5/10/20) + 15 из пресетов. Другие проценты
# бот отрисовал бы, но выбрать их в его панели нельзя — держим один набор.
MARKET_PERCENTS = (5, 10, 15, 20)
PREVIEW_DAYS = 7
PREVIEW_SAMPLES = 6


class SpecError(ValueError):
    """Ввод с сайта не годится; `field` — какое поле подсветить."""

    def __init__(self, field_name: str, message: str) -> None:
        self.field = field_name
        super().__init__(message)


@dataclass(slots=True)
class PushFilterSpec:
    """Проверенный фильтр уведомлений в терминах `user_filters`."""

    brand_ids: tuple[int, ...] = ()
    model_id: int | None = None
    year_from: int | None = None
    year_to: int | None = None
    max_age_years: int | None = None
    mileage_max_km: int | None = None
    price_max_usd: int | None = None
    below_market_percent: int | None = None  # None — без «ниже рынка»
    region: str | None = None  # None — «Все авто»
    fuel_groups: tuple[str, ...] = field(default_factory=tuple)

    def db_fields(self) -> dict:
        """Аргументы `bot/db.create_filter` / `update_filter` — как `filter_panel.filter_fields`."""
        single = self.brand_ids[0] if len(self.brand_ids) == 1 else None
        return {
            "brand_id": single,
            "brand_ids": pack_brand_ids(self.brand_ids),
            "car_model_id": self.model_id if single else None,
            "year_from": self.year_from,
            "year_to": self.year_to,
            "max_age_years": self.max_age_years,
            "mileage_max_km": self.mileage_max_km,
            "price_max_usd": float(self.price_max_usd) if self.price_max_usd is not None else None,
            "below_market_only": self.below_market_percent is not None,
            "below_market_percent": self.below_market_percent,
            "skip_foreign": False,
            "region": self.region,
            "brand_group": None,
            "fuel_groups": pack_fuel_groups(self.fuel_groups),
        }

    def transient(self) -> UserFilter:
        """Несохранённая строка `user_filters` — для `matching.filter_matches`."""
        fields = self.db_fields()
        price = fields.pop("price_max_usd")
        return UserFilter(
            **fields,
            price_max_usd=Decimal(str(price)) if price is not None else None,
            enabled=True,
        )


def _max_year() -> int:
    return datetime.now(timezone.utc).year + 1


def allowed_brand_ids(session: Session) -> set[int]:
    """Марки, которые бот предлагает в панели (`db.list_brands`)."""
    names = get_settings().allowed_brand_list
    return set(session.scalars(select(Brand.id).where(Brand.name.in_(names))).all())


def validate_spec(
    session: Session,
    *,
    brand_ids: list[int],
    model_id: int | None,
    year_from: int | None,
    year_to: int | None,
    max_age_years: int | None,
    mileage_max_km: int | None,
    price_max_usd: int | None,
    below_market_percent: int | None,
    region: str | None,
    fuel_groups: list[str],
) -> PushFilterSpec:
    """Ввод с сайта → `PushFilterSpec` или `SpecError` с понятным текстом."""
    brands = tuple(sorted(set(brand_ids)))
    if len(brands) > MAX_FILTER_BRANDS:
        raise SpecError("brand_ids", f"Не больше {MAX_FILTER_BRANDS} марок в одном фильтре")
    if brands:
        unknown = set(brands) - allowed_brand_ids(session)
        if unknown:
            raise SpecError("brand_ids", "Такой марки нет в списке бота")
    if model_id is not None:
        if len(brands) != 1:
            raise SpecError("model_id", "Модель выбирается только при одной марке")
        model_brand = session.scalar(select(CarModel.brand_id).where(CarModel.id == model_id))
        if model_brand != brands[0]:
            raise SpecError("model_id", "Модель не этой марки")

    if max_age_years is not None and (year_from is not None or year_to is not None):
        raise SpecError("max_age_years", "Либо годы, либо «не старше N лет»")
    if max_age_years is not None and not 1 <= max_age_years <= MAX_AGE_YEARS:
        raise SpecError("max_age_years", f"От 1 до {MAX_AGE_YEARS} лет")
    for name, year in (("year_from", year_from), ("year_to", year_to)):
        if year is not None and not MIN_YEAR <= year <= _max_year():
            raise SpecError(name, f"Год — от {MIN_YEAR} до {_max_year()}")
    if year_from is not None and year_to is not None and year_from > year_to:
        year_from, year_to = year_to, year_from  # как parse_years бота: «2022-2015» = 2015–2022

    if mileage_max_km is not None and not 0 < mileage_max_km <= MAX_MILEAGE_KM:
        raise SpecError("mileage_max_km", "Пробег — до 1 500 000 км")
    if price_max_usd is not None and not 0 < price_max_usd <= MAX_PRICE_USD:
        raise SpecError("price_max_usd", "Цена — до $5 000 000")
    if below_market_percent is not None and below_market_percent not in MARKET_PERCENTS:
        raise SpecError("below_market_percent", "Порог «ниже рынка» — 5, 10, 15 или 20 %")
    if region is not None and region not in REGION_LABELS:
        raise SpecError("region", "Неизвестный регион")
    unknown_fuel = set(fuel_groups) - set(FUEL_GROUPS)
    if unknown_fuel:
        raise SpecError("fuel_groups", "Неизвестный тип топлива")
    # Все пять типов = «любое» (pack_fuel_groups вернёт None) — храним пустым.
    fuels = tuple(g for g in FUEL_GROUPS if g in set(fuel_groups)) if pack_fuel_groups(fuel_groups) else ()

    return PushFilterSpec(
        brand_ids=brands,
        model_id=model_id if len(brands) == 1 else None,
        year_from=year_from,
        year_to=year_to,
        max_age_years=max_age_years,
        mileage_max_km=mileage_max_km,
        price_max_usd=price_max_usd,
        below_market_percent=below_market_percent,
        region=region,
        fuel_groups=fuels,
    )


# --- предпросмотр ------------------------------------------------------------------


def _aware(value: datetime) -> datetime:
    """SQLite в тестах отдаёт naive-время; Postgres — с зоной."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().scheduler_timezone)


def _candidates(session: Session, spec: PushFilterSpec, since: datetime, now: datetime) -> list[tuple[Listing, datetime]]:
    """Объявления, которые бот за окно рассматривал бы как пуш, и когда.

    Как `matching.find_new_listings`, только за `since`…`now`: «new» — если
    публикация на источнике была не раньше `bot_max_listing_age_hours` до
    события (VIP-поднятое старое — не новинка), «price_changed» — всегда.
    Снятые после пуша тоже считаются: уведомление тогда ушло.
    SQL-предфильтр по полям фильтра только сужает выборку; решение — за
    `filter_matches` ниже (регион, топливо, марки — ровно как у бота).
    """
    settings = get_settings()
    H = ListingStatusHistory
    events = (
        select(H.listing_id, H.event_type, func.min(H.observed_at).label("at"))
        .where(H.event_type.in_(("new", "price_changed")), H.observed_at >= since, H.observed_at <= now)
        .group_by(H.listing_id, H.event_type)
        .subquery()
    )
    max_age = timedelta(hours=settings.bot_max_listing_age_hours)
    conditions = [
        Listing.current_price_usd.is_not(None),
        Listing.current_price_usd >= MIN_PLAUSIBLE_PRICE_USD,
    ]
    if spec.brand_ids:
        conditions.append(Listing.brand_id.in_(spec.brand_ids))
    if spec.model_id is not None:
        conditions.append(Listing.car_model_id == spec.model_id)
    if spec.year_from is not None:
        conditions.append(Listing.year >= spec.year_from)
    if spec.year_to is not None:
        conditions.append(Listing.year <= spec.year_to)
    if spec.max_age_years is not None:
        conditions.append(Listing.year >= min_year_for_age(spec.max_age_years, now))
    if spec.mileage_max_km is not None:
        conditions.append(Listing.mileage_km <= spec.mileage_max_km)
    if spec.price_max_usd is not None:
        conditions.append(Listing.current_price_usd <= spec.price_max_usd)
    if spec.fuel_groups:
        conditions.append(Listing.fuel_group.in_(spec.fuel_groups))

    rows = session.execute(
        select(Listing, events.c.event_type, events.c.at)
        .join(events, events.c.listing_id == Listing.id)
        .where(*conditions)
    ).all()
    first: dict[int, tuple[Listing, datetime]] = {}
    for listing, event_type, at in rows:
        at = _aware(at)
        # Свежесть новинки — в Python: арифметика дат в SQL у Postgres и SQLite разная.
        if event_type == "new" and (
            listing.source_created_at is None or _aware(listing.source_created_at) < at - max_age
        ):
            continue
        known = first.get(listing.id)
        if known is None or at < known[1]:
            first[listing.id] = (listing, at)
    return list(first.values())


def preview(session: Session, spec: PushFilterSpec, *, user: BotUser | None = None, now: datetime | None = None) -> dict:
    """«За 7 дней этот фильтр прислал бы N авто» + раскладка по дням + примеры.

    `user` — вошедший: его чёрный список тоже применяется (как в рассылке).
    """
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    tz = _local_tz()
    today = now.astimezone(tz).date()
    days = [today - timedelta(days=offset) for offset in range(PREVIEW_DAYS - 1, -1, -1)]
    # Окно — с начала первого из 7 местных дней: столбики «по дням» полные.
    since = datetime.combine(days[0], datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)

    user_filter = spec.transient()
    brand_groups = load_brand_groups(session)
    blocked = load_block_sets(session, {user.id}).get(user.id) if user is not None else None
    candidates = [
        (listing, at)
        for listing, at in _candidates(session, spec, since, now)
        if (not settings.bot_push_require_photo or has_real_photo(listing))
        and filter_matches(listing, user_filter, brand_groups, now=now)
        and (blocked is None or not blocked.blocks(listing))
    ]
    estimates = batch_market_estimates(
        session, [listing for listing, _ in candidates], min_samples=settings.bot_below_avg_min_samples
    )

    per_day: Counter = Counter()
    picked: list[tuple[float, Listing]] = []
    without_market = 0
    for listing, at in candidates:
        estimate = estimates.get(listing.id)
        avg = estimate.avg_usd if estimate is not None else None
        below: float | None = None
        if avg is not None and avg > 0:
            diff = round((1 - float(listing.current_price_usd) / avg) * 100, 1)
            below = diff if diff > 0 else None
        if not passes_below_market(listing, user_filter, avg, factor=settings.bot_below_avg_factor):
            continue
        if not is_plausible_discount(below, settings.bot_max_below_market_percent):
            continue
        if avg is None:
            without_market += 1
        per_day[at.astimezone(tz).date()] += 1
        if listing.is_active:
            picked.append((below if below is not None else -1.0, listing))

    total = sum(per_day.values())
    picked.sort(key=lambda item: (item[0], item[1].id), reverse=True)
    samples = listings_by_ids(session, [listing.id for _, listing in picked[:PREVIEW_SAMPLES]])
    return {
        "days": PREVIEW_DAYS,
        "total": total,
        "per_day_avg": round(total / PREVIEW_DAYS, 1),
        "by_day": [{"date": day, "count": per_day.get(day, 0)} for day in days],
        "without_market": without_market,
        "samples": samples,
    }


def site_created_text(summary: str, *, edited: bool = False) -> str:
    """Сообщение в бота после сохранения фильтра на сайте: подтверждение и
    напоминание, что сайт и бот — одни и те же фильтры."""
    head = "✏️ Фильтр изменён на сайте" if edited else "✅ Фильтр добавлен на сайте"
    return f"{head}:\n{summary}\n\nОн уже работает и виден в «📋 Мои фильтры»."
