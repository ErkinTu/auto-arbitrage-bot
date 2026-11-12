"""Публичная лента для сайта: объявления + наша аналитика поверх.

Граница «что видит интернет» — здесь и в `api/public/`. Никогда не отдаём:
`seller_phone` (ПДн), `raw_payloads`, `title`/`description` источника как HTML,
внутренние флаги качества. Тексты источника — чужие данные: фронт выводит их
только текстом, не `v-html`.

Оценка рынка — тот же `matching.market_estimate`, что у бота (через пакетный
`showcase.market_estimates`): сайт и бот обязаны называть одну и ту же «выгоду».
Вердикт «выгодно / в рынке / дорого» — только раскраска поверх отклонения, порог
`PUBLIC_DEAL_THRESHOLD_PERCENT` (решение владельца 09.11.2026: главное —
средняя цена и отклонение, категории вторичны).

Пагинация курсорная (keyset), не OFFSET: лента пополняется каждые 10 минут, и
OFFSET давал бы дубли и пропуски на стыке страниц.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import Integer, Select, and_, cast, exists, func, or_, select, true
from sqlalchemy.orm import Session, aliased

from app.bot.matching import MarketEstimate, market_estimate
from app.core.config import get_settings
from app.models import Brand, CarModel, DailyMarketStat, Listing, ListingPriceHistory, Source
from app.services.exchange import get_latest_usd_rate
from app.services import best_picks
from app.services.images import real_photo
from app.services.normalizer import MAX_PLAUSIBLE_PRICE_USD, MIN_PLAUSIBLE_PRICE_USD
from app.services.regions import REGION_LABELS, is_foreign, region_matches, region_of
from app.services.sources import listing_public_url
from app.services.specs import group_labels, spec_group
from app.services.stats import mileage_bucket

Sort = Literal["fresh", "price_asc", "price_desc", "similar"]

# «Похожие на объявление» (sort=similar&near=<id>, кнопки «Искать дальше» на
# странице авто, 10.11.2026): ближе по цене, году и пробегу — выше. Вес года —
# 6 % цены за год, пробега — 1/3 цены за 100 тыс. км; неизвестный год/пробег —
# штраф как за 5 лет / 100 тыс. км. Пагинация — смещением: ключ сортировки
# вычисляемый, а выдача и так узкая (фильтр по марке/цене/году).
_SIMILAR_YEAR_WEIGHT = 0.06
_SIMILAR_KM_WEIGHT = 1 / 300_000
Deal = Literal["any", "good", "not_high"]

# Сколько кандидатов просматриваем максимум, когда фильтр зависит от оценки
# (deal=good): её не выразить в SQL, поэтому добираем пачками. Потолок — чтобы
# редкий фильтр не превращался в проход по всей базе.
_DEAL_SCAN_BATCH = 200
_DEAL_SCAN_MAX = 3000
# Счётчик «Показать N» с фильтром по оценке: точный до 1000 кандидатов, дальше —
# экстраполяция по 1000 свежих (флаг approximate). 3000 давали ~2 с на копии прода.
_DEAL_COUNT_SAMPLE = 1000
PRICE_DROP_WINDOW = timedelta(days=7)


class CursorError(ValueError):
    """Курсор не наш или испорчен — 400, а не 500."""


@dataclass(slots=True)
class FeedFilter:
    brand_ids: list[int] = field(default_factory=list)
    model_id: int | None = None
    cities: list[str] = field(default_factory=list)
    source: str | None = None  # "mashina" | "lalafo"
    price_from: int | None = None
    price_to: int | None = None
    year_from: int | None = None
    year_to: int | None = None
    mileage_from: int | None = None
    mileage_to: int | None = None
    seller_type: str | None = None  # "private" | "dealer"
    fuel_groups: list[str] = field(default_factory=list)
    # Характеристики — строки источника как есть («Седан», «Автомат»…); значения
    # для чипов сайт берёт из `feed_facets`, сравнение — без учёта регистра.
    # None — без фильтра; список (даже пустой) — только эти строки. API раскрывает
    # в них группы (`services/specs.py`): пустой список = группа без объявлений.
    body_types: list[str] | None = None
    gearboxes: list[str] | None = None
    drive_types: list[str] | None = None
    steering: list[str] | None = None
    customs: str | None = None  # "cleared" | "not_cleared"
    # Регионы как в боте (bishkek, chuy, …, foreign — `services/regions.py`).
    # API раскрывает их в точный список городов из базы (`region_cities`):
    # карта «город → область» живёт в Python, в SQL её не выразить.
    regions: list[str] = field(default_factory=list)
    region_cities: list[str] | None = None
    with_photo: bool = False
    new_24h: bool = False
    price_dropped: bool = False
    deal: Deal = "any"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- курсор -----------------------------------------------------------------


def encode_cursor(sort: Sort, listing: Listing) -> str:
    key = (
        listing.first_seen_at.isoformat()
        if sort == "fresh"
        else str(listing.current_price_usd)
    )
    raw = f"{sort}|{key}|{listing.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(sort: Sort, cursor: str) -> tuple[object, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        kind, key, listing_id = raw.split("|")
        if kind != sort:
            raise CursorError("cursor belongs to another sort")
        value: object = datetime.fromisoformat(key) if sort == "fresh" else Decimal(key)
        return value, int(listing_id)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise CursorError("bad cursor") from exc


# --- запрос -----------------------------------------------------------------


def _text_in(column, values: list[str]):
    """Строка источника — одно из значений: точно или без учёта регистра и
    крайних пробелов (lower() в Postgres знает кириллицу; точное — для SQLite
    в тестах и для индекса по city). NULL не попадает: фильтр не врёт."""
    return or_(
        column.in_(values),
        func.lower(func.trim(column)).in_([v.strip().lower() for v in values]),
    )


def _feed_conditions() -> list:
    """Что вообще попадает в ленту. Именно `= true`, не `IS true`: частичный
    индекс `ix_listing_feed_fresh` объявлен `WHERE is_active`, и `IS true`
    планировщик с ним не сопоставляет (замер на копии прода: 90 мс против индекса)."""
    return [
        Listing.is_active == true(),
        Listing.brand_id.is_not(None),
        Listing.car_model_id.is_not(None),
        Listing.year.is_not(None),
        Listing.current_price_usd.is_not(None),
        # Мусор цены («2 $», «206 $» — взнос рассрочки, цена в сомах без пересчёта)
        # на сайт не попадает: тот же порог, что у аналитики и бота
        # (`price_implausibly_low/high` в нормализаторе).
        Listing.current_price_usd >= MIN_PLAUSIBLE_PRICE_USD,
        Listing.current_price_usd <= MAX_PLAUSIBLE_PRICE_USD,
    ]


def _base_query(flt: FeedFilter, *, now: datetime) -> Select:
    """Активные объявления с распознанной маркой/моделью/годом и ценой.

    Новое объявление видно с `feed_at` — плавное появление после обхода
    (`sync._schedule_feed_reveal`); NULL — сразу.
    """
    conditions = _feed_conditions()
    conditions.append(or_(Listing.feed_at.is_(None), Listing.feed_at <= now))
    if flt.brand_ids:
        conditions.append(Listing.brand_id.in_(flt.brand_ids))
    if flt.model_id is not None:
        conditions.append(Listing.car_model_id == flt.model_id)
    if flt.cities:
        conditions.append(_text_in(Listing.city, flt.cities))
    if flt.region_cities is not None:
        # Пустой список (регион без объявлений) → ничего, а не «всё».
        conditions.append(Listing.city.in_(flt.region_cities))
    for column, values in (
        (Listing.body_type, flt.body_types),
        (Listing.gearbox, flt.gearboxes),
        (Listing.drive_type, flt.drive_types),
        (Listing.steering_wheel, flt.steering),
    ):
        if values is not None:
            conditions.append(_text_in(column, values))
    if flt.customs:
        conditions.append(Listing.customs_status == flt.customs)
    if flt.source:
        conditions.append(
            Listing.source_id.in_(select(Source.id).where(Source.name.ilike(f"{flt.source}%")))
        )
    if flt.price_from is not None:
        conditions.append(Listing.current_price_usd >= flt.price_from)
    if flt.price_to is not None:
        conditions.append(Listing.current_price_usd <= flt.price_to)
    if flt.year_from is not None:
        conditions.append(Listing.year >= flt.year_from)
    if flt.year_to is not None:
        conditions.append(Listing.year <= flt.year_to)
    # Пробег: границы применяются только к известному пробегу — NULL (у lalafo
    # ~43%) под «до 100 000 км» не попадает, иначе фильтр бы врал.
    if flt.mileage_from is not None:
        conditions.append(Listing.mileage_km >= flt.mileage_from)
    if flt.mileage_to is not None:
        conditions.append(Listing.mileage_km <= flt.mileage_to)
    if flt.seller_type:
        conditions.append(Listing.seller_type == flt.seller_type)
    if flt.fuel_groups:
        conditions.append(Listing.fuel_group.in_(flt.fuel_groups))
    if flt.with_photo:
        conditions.append(Listing.image_url.is_not(None))
    if flt.new_24h:
        conditions.append(Listing.first_seen_at >= now - timedelta(hours=24))
    if flt.price_dropped:
        history = aliased(ListingPriceHistory)
        conditions.append(
            exists().where(
                history.listing_id == Listing.id,
                history.observed_at >= now - PRICE_DROP_WINDOW,
                history.price_usd > Listing.current_price_usd,
            )
        )
    # Без JOIN-ов: с ними планировщик уходил в полный проход по `listings` с
    # top-N сортировкой (~200 мс на 120k активных); без них сортировка берётся
    # из индекса `ix_listing_feed_fresh`. Имена марок/моделей — `_names`.
    return select(Listing).where(*conditions)


def _order(query: Select, sort: Sort, cursor: str | None) -> Select:
    if sort == "fresh":
        column, descending = Listing.first_seen_at, True
    elif sort == "price_asc":
        column, descending = Listing.current_price_usd, False
    else:
        column, descending = Listing.current_price_usd, True
    if cursor:
        value, last_id = decode_cursor(sort, cursor)
        if descending:
            query = query.where(or_(column < value, and_(column == value, Listing.id < last_id)))
        else:
            query = query.where(or_(column > value, and_(column == value, Listing.id > last_id)))
    if descending:
        return query.order_by(column.desc(), Listing.id.desc())
    return query.order_by(column.asc(), Listing.id.asc())


# --- оценка рынка пачкой -----------------------------------------------------


def batch_market_estimates(
    session: Session, listings: list[Listing], *, min_samples: int
) -> dict[int, MarketEstimate | None]:
    """`matching.market_estimate` для страницы ОДНИМ запросом к статистике.

    Семантика та же, что у бота (тест `test_batch_estimates_match_bot`):
      * зарубежные и без марки/модели/года/цены — оценки нет;
      * пробег известен — строки своего бакета на ПОСЛЕДНЮЮ дату этого бакета;
      * пробег NULL — все бакеты модели+года на последнюю дату по всем бакетам;
      * сумма образцов < min_samples — оценки нет; цена — среднее, взвешенное
        по образцам (basis=avg, как в пушах).
    По-листинговый вызов делал 2 запроса на когорту: ~420 мс на страницу 24
    карточек; пачкой — один запрос с оконными max().
    """
    result: dict[int, MarketEstimate | None] = {}
    eligible = [
        item
        for item in listings
        if item.brand_id is not None
        and item.car_model_id is not None
        and item.year is not None
        and item.current_price_usd is not None
        and not is_foreign(item.city)
    ]
    for item in listings:
        result[item.id] = None
    if not eligible:
        return result

    triples = {(item.brand_id, item.car_model_id, item.year) for item in eligible}
    S = DailyMarketStat
    # Окно: статистика пересчитывается ежедневно для всех когорт, поэтому
    # последняя дата когорты почти всегда = глобальная последняя. Берём 7 дней
    # до неё — оконный max() не сканирует всю историю (230 → единицы мс). Если у
    # когорты в окне строк нет, её настоящая последняя дата старше — для таких
    # (редких) зовём сам market_estimate ниже: семантика остаётся точной.
    latest = session.scalar(select(func.max(S.stat_date)))
    if latest is None:
        return result
    window_start = latest - timedelta(days=7)
    windowed = (
        select(
            S.brand_id,
            S.car_model_id,
            S.year,
            S.mileage_bucket,
            S.stat_date,
            S.avg_price_usd,
            S.sample_count,
            func.max(S.stat_date)
            .over(partition_by=(S.brand_id, S.car_model_id, S.year, S.mileage_bucket))
            .label("max_bucket"),
            func.max(S.stat_date)
            .over(partition_by=(S.brand_id, S.car_model_id, S.year))
            .label("max_all"),
        )
        .where(
            S.stat_date >= window_start,
            or_(
                *(
                    and_(S.brand_id == b, S.car_model_id == m, S.year == y)
                    for b, m, y in triples
                )
            ),
        )
        .subquery()
    )
    rows = session.execute(
        select(windowed).where(
            or_(
                windowed.c.stat_date == windowed.c.max_bucket,
                windowed.c.stat_date == windowed.c.max_all,
            )
        )
    ).all()

    by_bucket: dict[tuple, list] = {}
    by_triple: dict[tuple, list] = {}
    for row in rows:
        triple = (row.brand_id, row.car_model_id, row.year)
        if row.stat_date == row.max_bucket:
            by_bucket.setdefault((*triple, row.mileage_bucket), []).append(row)
        if row.stat_date == row.max_all:
            by_triple.setdefault(triple, []).append(row)

    cache: dict[tuple, MarketEstimate | None] = {}
    for item in eligible:
        triple = (item.brand_id, item.car_model_id, item.year)
        key = (*triple, mileage_bucket(item.mileage_km)) if item.mileage_km is not None else triple
        if key not in cache:
            group = by_bucket.get(key, []) if item.mileage_km is not None else by_triple.get(key, [])
            if not group:
                # Последняя дата когорты старше окна — точный ответ даёт бот.
                cache[key] = market_estimate(session, item, min_samples=min_samples)
                result[item.id] = cache[key]
                continue
            total = sum(row.sample_count for row in group)
            cache[key] = (
                MarketEstimate(
                    avg_usd=sum(float(row.avg_price_usd) * row.sample_count for row in group) / total,
                    samples=total,
                )
                if total >= min_samples
                else None
            )
        result[item.id] = cache[key]
    return result


# --- аналитика поверх -------------------------------------------------------


def _verdict(diff_percent: float) -> str:
    """Строго больше порога: ровно ±5 % — ещё «в рынке» (владелец 09.11.2026:
    «выше/ниже рынка больше чем на 5 %»; внутри — карточка без бейджа)."""
    threshold = get_settings().public_deal_threshold_percent
    if diff_percent < -threshold:
        return "good"
    if diff_percent > threshold:
        return "high"
    return "market"


def _market(listing: Listing, estimate) -> dict | None:
    if estimate is None or not estimate.avg_usd:
        return None
    price = float(listing.current_price_usd)
    diff = price - estimate.avg_usd
    percent = diff / estimate.avg_usd * 100
    # Отклонение больше потолка бота — мусор цены (опечатка, «цена за месяц»).
    # Не выдумываем «−80% к рынку»: оценки просто нет, как и в боте.
    if abs(percent) > get_settings().bot_max_below_market_percent:
        return None
    return {
        "avg_price_usd": round(estimate.avg_usd),
        "samples": estimate.samples,
        "diff_usd": round(diff),
        "diff_percent": round(percent, 1),
        "verdict": _verdict(percent),
    }


def _price_drops(session: Session, listings: list[Listing], now: datetime) -> dict[int, int]:
    """Насколько цена снизилась за 7 дней: максимум за окно минус текущая."""
    ids = [listing.id for listing in listings]
    if not ids:
        return {}
    rows = session.execute(
        select(ListingPriceHistory.listing_id, func.max(ListingPriceHistory.price_usd))
        .where(
            ListingPriceHistory.listing_id.in_(ids),
            ListingPriceHistory.observed_at >= now - PRICE_DROP_WINDOW,
        )
        .group_by(ListingPriceHistory.listing_id)
    ).all()
    current = {listing.id: listing.current_price_usd for listing in listings}
    drops: dict[int, int] = {}
    for listing_id, peak in rows:
        price = current.get(listing_id)
        if peak is not None and price is not None and peak > price:
            drops[listing_id] = int(round(peak - price))
    return drops


def hd_image(url: str | None) -> str | None:
    """Крупный вариант фото для сайта.

    mashina хранит у нас `…_medium.webp` (360×480 — хватает Telegram), а рядом
    по тому же хэшу лежит `…_large.webp` (675×900; ключ `big` в их API).
    Проверено на живых объявлениях 09.11.2026. lalafo уже отдаёт `original`.
    """
    if url and "mashina.kg/" in url and "_medium." in url:
        return url.replace("_medium.", "_large.")
    return url


def card_image(url: str | None) -> str | None:
    """Лёгкий вариант фото для карточек ленты.

    lalafo хранит у нас `…/posters/original/…` (1024 px, 160–320 КБ) — для
    карточки 300–400 px это перебор: замер 11.11.2026 на 60 фото с 6 параллельными
    соединениями — оригиналы p90 6,3 с, максимум 24 с (CDN «подвисает» на тяжёлых),
    11,5 МБ; вариант `…/posters/api/…` (370 px, 25–60 КБ) — p90 0,28 с, максимум
    0,6 с, 2,1 МБ (174 из 177 проверенных отвечают; остальные — таймаут замера).
    Оригинал остаётся в галерее страницы авто (`images`). У mashina свой путь:
    `_medium` в карточке, `_large` в srcset (`hd_image`)."""
    if url and "lalafo.com/i/posters/original/" in url:
        return url.replace("/i/posters/original/", "/i/posters/api/", 1)
    return url


FEED_THUMBS = 3


def card_thumbs(cover: str | None, gallery: list[str]) -> list[str]:
    """До трёх лёгких фото для карточки-строки ленты: обложка + следующие кадры.

    Карточка-строка показывает веер миниатюр (обложка по центру, соседние за
    ней); крупнее карточного варианта не нужно — миниатюра меньше 400 px.
    Галерея уже загружена вместе с объявлением — лишних запросов нет."""
    first = real_photo(cover)
    seen: set[str] = set()
    out: list[str] = []
    for url in ([first] if first else []) + gallery:
        small = card_image(url)
        if small and small not in seen:
            seen.add(small)
            out.append(small)
        if len(out) == FEED_THUMBS:
            break
    return out


def _source_key(source_name: str) -> str:
    return source_name.split(".")[0].lower()


def serialize(
    listing: Listing,
    brand: str,
    model: str,
    source_name: str,
    *,
    market: dict | None,
    price_drop: int | None,
    usd_rate: Decimal | None,
) -> dict:
    price_usd = float(listing.current_price_usd) if listing.current_price_usd is not None else None
    if listing.current_price_kgs is not None:
        price_kgs = int(listing.current_price_kgs)
    elif price_usd is not None and usd_rate:
        price_kgs = int(round(price_usd * float(usd_rate), -3))
    else:
        price_kgs = None
    gallery = [url for url in (listing.image_urls or []) if real_photo(url)]
    return {
        "id": listing.id,
        "brand": brand,
        "model": model,
        "year": listing.year,
        "mileage_km": listing.mileage_km,
        "engine_volume": float(listing.engine_volume) if listing.engine_volume else None,
        "fuel_group": listing.fuel_group,
        "gearbox": listing.gearbox,
        "body_type": listing.body_type,
        "drive_type": listing.drive_type,
        "steering_wheel": listing.steering_wheel,
        "customs_status": listing.customs_status,
        "city": listing.city,
        "is_foreign": is_foreign(listing.city),
        "seller_type": listing.seller_type,
        "source": _source_key(source_name),
        "source_url": listing_public_url(listing.source_slug, listing.external_id),
        "image_url": card_image(real_photo(listing.image_url)),
        # Для srcset: браузер сам выберет 360 или 675 px под экран.
        # У lalafo крупнее карточного нет смысла: оригинал в srcset заставил бы
        # ретина-экраны тянуть тяжёлый файл (см. card_image); галерея — `images`.
        "image_url_hd": (
            card_image(real_photo(listing.image_url))
            if "lalafo.com/" in (listing.image_url or "")
            else hd_image(real_photo(listing.image_url))
        ),
        "photo_count": max(len(gallery), 1 if real_photo(listing.image_url) else 0),
        "thumbs": card_thumbs(listing.image_url, gallery),
        "price_usd": round(price_usd) if price_usd is not None else None,
        "price_kgs": price_kgs,
        "price_drop_usd": price_drop,
        "market": market,
        "published_at": listing.source_created_at or listing.first_seen_at,
        "first_seen_at": listing.first_seen_at,
        "is_active": listing.is_active,
        "deactivated_at": listing.deactivated_at,
    }


def _names(session: Session, listings: list[Listing]) -> tuple[dict, dict, dict]:
    """Имена марок, моделей и источников для страницы — три мелких запроса."""
    brand_ids = {item.brand_id for item in listings}
    model_ids = {item.car_model_id for item in listings}
    brands = dict(session.execute(select(Brand.id, Brand.name).where(Brand.id.in_(brand_ids))).all())
    models = dict(
        session.execute(select(CarModel.id, CarModel.name).where(CarModel.id.in_(model_ids))).all()
    )
    sources = dict(session.execute(select(Source.id, Source.name)).all())
    return brands, models, sources


def _enrich(session: Session, listings: list[Listing], now: datetime) -> list[dict]:
    if not listings:
        return []
    settings = get_settings()
    estimates = batch_market_estimates(
        session, listings, min_samples=settings.bot_below_avg_min_samples
    )
    brands, models, sources = _names(session, listings)
    drops = _price_drops(session, listings, now)
    rate = get_latest_usd_rate(session)
    return [
        serialize(
            listing,
            brands.get(listing.brand_id, ""),
            models.get(listing.car_model_id, ""),
            sources.get(listing.source_id, ""),
            market=_market(listing, estimates.get(listing.id)),
            price_drop=drops.get(listing.id),
            usd_rate=rate,
        )
        for listing in listings
    ]


def _deal_ok(item: dict, deal: Deal) -> bool:
    if deal == "any":
        return True
    market = item["market"]
    if market is None:
        return False
    if deal == "good":
        return market["verdict"] == "good"
    return market["verdict"] != "high"  # not_high: «в рынке и ниже»


# --- публичные функции ------------------------------------------------------


def _similar_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        kind, key, _ = base64.urlsafe_b64decode(padded.encode()).decode().split("|")
        if kind != "similar":
            raise CursorError("cursor belongs to another sort")
        return max(0, int(key))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise CursorError("bad cursor") from exc


def _similar_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"similar|{offset}|0".encode()).decode().rstrip("=")


def _similar_score(ref: Listing):
    price = float(ref.current_price_usd)
    score = func.abs(Listing.current_price_usd - price) / price
    if ref.year:
        score = score + func.coalesce(func.abs(Listing.year - ref.year), 5) * _SIMILAR_YEAR_WEIGHT
    if ref.mileage_km is not None:
        score = score + func.coalesce(func.abs(Listing.mileage_km - ref.mileage_km), 100_000) * _SIMILAR_KM_WEIGHT
    return score


def _similar_page(session: Session, base: Select, flt: FeedFilter, ref: Listing, *, limit: int, cursor: str | None, now) -> dict:
    offset = _similar_offset(cursor)
    ordered = base.where(Listing.id != ref.id).order_by(_similar_score(ref), Listing.id)
    items: list[dict] = []
    scanned = 0
    while len(items) < limit:
        batch = limit + 1 if flt.deal == "any" else _DEAL_SCAN_BATCH
        rows = list(session.scalars(ordered.offset(offset + scanned).limit(batch)).all())
        if not rows:
            return {"items": items, "next_cursor": None}
        for row, item in zip(rows, _enrich(session, rows, now)):
            scanned += 1
            if _deal_ok(item, flt.deal):
                items.append(item)
                if len(items) == limit:
                    break
        if len(rows) < batch and len(items) < limit:
            return {"items": items, "next_cursor": None}
        if flt.deal == "any" or scanned >= _DEAL_SCAN_MAX:
            break
    more = session.scalars(ordered.offset(offset + scanned).limit(1)).first() is not None
    return {"items": items, "next_cursor": _similar_cursor(offset + scanned) if more else None}


def feed_page(
    session: Session,
    flt: FeedFilter,
    *,
    sort: Sort = "fresh",
    limit: int = 24,
    cursor: str | None = None,
    near: int | None = None,
) -> dict:
    now = _now()
    base = _base_query(flt, now=now)
    items: list[dict] = []
    next_cursor: str | None = None

    if sort == "similar":
        ref = session.get(Listing, near) if near else None
        if ref is not None and ref.current_price_usd:
            return _similar_page(session, base, flt, ref, limit=limit, cursor=cursor, now=now)
        sort, cursor = "fresh", None  # не на что равняться — обычная лента с начала

    if flt.deal == "any":
        rows = list(session.scalars(_order(base, sort, cursor).limit(limit + 1)).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = _enrich(session, rows, now)
        if has_more and rows:
            next_cursor = encode_cursor(sort, rows[-1])
        return {"items": items, "next_cursor": next_cursor}

    # Фильтр по оценке: добираем пачками, пока не наберём страницу или потолок.
    scanned = 0
    scan_cursor = cursor
    last_row = None
    exhausted = False
    while len(items) < limit and scanned < _DEAL_SCAN_MAX:
        rows = list(session.scalars(_order(base, sort, scan_cursor).limit(_DEAL_SCAN_BATCH)).all())
        if not rows:
            exhausted = True
            break
        scanned += len(rows)
        for row, item in zip(rows, _enrich(session, rows, now)):
            last_row = row
            if _deal_ok(item, flt.deal):
                items.append(item)
                if len(items) == limit:
                    break
        scan_cursor = encode_cursor(sort, last_row)
        if len(rows) < _DEAL_SCAN_BATCH and len(items) < limit:
            exhausted = True
            break
    if not exhausted and last_row is not None:
        next_cursor = encode_cursor(sort, last_row)
    return {"items": items, "next_cursor": next_cursor}


def feed_count(session: Session, flt: FeedFilter) -> dict:
    """«Показать N авто». С фильтром по оценке — оценка по просмотренной выборке."""
    now = _now()
    base = _base_query(flt, now=now)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    if flt.deal == "any" or total == 0:
        return {"count": total, "approximate": False}
    rows = list(
        session.scalars(_order(base, "fresh", None).limit(min(total, _DEAL_COUNT_SAMPLE))).all()
    )
    matched = sum(1 for item in _enrich(session, rows, now) if _deal_ok(item, flt.deal))
    if total <= _DEAL_COUNT_SAMPLE:
        return {"count": matched, "approximate": False}
    # Экстраполяция по свежей выборке: честно помечаем как приблизительное.
    return {"count": round(matched / len(rows) * total), "approximate": True}


# --- значения для чипов фильтра ---------------------------------------------

# Сколько значений отдаём и с какого числа объявлений: у источников бывают
# опечатки и экзотика («Лимузин» × 2) — чипом они только шумят.
FACET_LIMIT = 24
CITY_FACET_LIMIT = 40
FACET_MIN_COUNT = 5


def _facet(session: Session, column, *, limit: int) -> list[dict]:
    """Значения колонки по активной ленте: варианты написания («седан» /
    «Седан ») склеиваются, подпись — самое частое написание. Фильтр потом
    сравнивает без учёта регистра (`_text_in`), так что чип ловит все варианты."""
    rows = session.execute(
        select(column, func.count())
        .where(*_feed_conditions(), column.is_not(None))
        .group_by(column)
    ).all()
    merged: dict[str, dict] = {}
    for raw, count in rows:
        label = str(raw).strip()
        # «-1» и прочие значения без букв — заглушка источника (mashina так
        # пишет «не указано» в КПП и руле): чипом не показываем.
        if not any(ch.isalpha() for ch in label):
            continue
        key = label.casefold()
        slot = merged.setdefault(key, {"value": label, "count": 0, "_best": 0})
        slot["count"] += count
        if count > slot["_best"]:
            slot["value"], slot["_best"] = label, count
    items = sorted(merged.values(), key=lambda x: (-x["count"], x["value"]))
    return [
        {"value": i["value"], "count": i["count"]} for i in items if i["count"] >= FACET_MIN_COUNT
    ][:limit]


def feed_spec_values(session: Session, field_name: str) -> dict[str, int]:
    """Все написания характеристики в активной ленте с числом объявлений."""
    column = getattr(Listing, field_name)
    rows = session.execute(
        select(column, func.count())
        .where(*_feed_conditions(), column.is_not(None))
        .group_by(column)
    ).all()
    return {value: count for value, count in rows}


def _group_facet(session: Session, field_name: str) -> list[dict]:
    """Чипы-группы (`services/specs.py`) с суммой объявлений по всем написаниям."""
    counts: dict[str, int] = {}
    for raw, count in feed_spec_values(session, field_name).items():
        code = spec_group(field_name, raw)
        if code:
            counts[code] = counts.get(code, 0) + count
    labels = group_labels(field_name)
    return [
        {"value": code, "label": labels[code], "count": counts[code]}
        for code in labels
        if counts.get(code, 0) >= FACET_MIN_COUNT
    ]


def feed_cities(session: Session) -> dict[str, int]:
    """Все города активной ленты с числом объявлений (для регионов). Кэш — у вызывающих."""
    rows = session.execute(
        select(Listing.city, func.count())
        .where(*_feed_conditions(), Listing.city.is_not(None))
        .group_by(Listing.city)
    ).all()
    return {city: count for city, count in rows}


def cities_for_regions(cities: dict[str, int], regions: list[str]) -> list[str]:
    """Точные написания городов из базы, попадающие под выбранные регионы."""
    return sorted(c for c in cities if any(region_matches(c, r) for r in regions))


def _region_facet(session: Session) -> list[dict]:
    """Регионы как в боте: Бишкек, Ош, 7 областей, «Зарубежные». Нераспознанные
    сёла в регион не попадают (как у бота) — только под «все»."""
    counts: dict[str, int] = {}
    for city, count in feed_cities(session).items():
        code = region_of(city)
        if code:
            counts[code] = counts.get(code, 0) + count
    order = [code for code in REGION_LABELS if code != "local"]
    return [
        {"value": code, "label": REGION_LABELS[code], "count": counts.get(code, 0)}
        for code in order
        if counts.get(code, 0) > 0
    ]


def feed_facets(session: Session) -> dict:
    """Значения для панели фильтров сайта — из базы, не захардкоженным списком.
    Считается по всей активной ленте (без учёта выбранных фильтров): набор чипов
    не прыгает при каждом нажатии. Кэшируется на уровне ручки."""
    customs = dict(
        session.execute(
            select(Listing.customs_status, func.count())
            .where(*_feed_conditions())
            .group_by(Listing.customs_status)
        ).all()
    )
    return {
        "regions": _region_facet(session),
        "body_types": _group_facet(session, "body_type"),
        "gearboxes": _group_facet(session, "gearbox"),
        "drive_types": _group_facet(session, "drive_type"),
        "steering": _group_facet(session, "steering_wheel"),
        "cities": _facet(session, Listing.city, limit=CITY_FACET_LIMIT),
        "customs": [
            {"value": key, "count": customs.get(key, 0)} for key in ("cleared", "not_cleared")
        ],
    }


def listings_by_ids(session: Session, ids: list[int]) -> list[dict]:
    """Для избранного: и активные, и снятые (их фронт показывает тусклыми)."""
    if not ids:
        return []
    now = _now()
    rows = list(
        session.scalars(
            select(Listing).where(
                Listing.id.in_(ids),
                Listing.brand_id.is_not(None),
                Listing.car_model_id.is_not(None),
                # Мусорная цена — как «нет объявления» (и в избранном, и на странице авто).
                or_(
                    Listing.current_price_usd.is_(None),
                    Listing.current_price_usd.between(MIN_PLAUSIBLE_PRICE_USD, MAX_PLAUSIBLE_PRICE_USD),
                ),
            )
        ).all()
    )
    by_id = {item["id"]: item for item in _enrich(session, rows, now)}
    return [by_id[i] for i in ids if i in by_id]


def listing_detail(session: Session, listing_id: int) -> dict:
    items = listings_by_ids(session, [listing_id])
    if not items:
        raise LookupError("Объявление не найдено")
    item = items[0]
    listing = session.get(Listing, listing_id)
    gallery = [hd_image(url) for url in (listing.image_urls or []) if real_photo(url)]
    if not gallery and real_photo(listing.image_url):
        gallery = [hd_image(real_photo(listing.image_url))]
    history = session.execute(
        select(ListingPriceHistory.observed_at, ListingPriceHistory.price_usd)
        .where(
            ListingPriceHistory.listing_id == listing_id,
            ListingPriceHistory.price_usd.is_not(None),
        )
        .order_by(ListingPriceHistory.observed_at.asc())
        .limit(200)
    ).all()
    item["images"] = gallery
    item["video_id"] = listing.video_id
    item["price_history"] = [
        {"at": at, "price_usd": round(float(price))} for at, price in history
    ]
    return item


SIMILAR_YEAR_SPAN = 2
SIMILAR_CANDIDATES = 60


def similar_listings(session: Session, listing_id: int, *, limit: int = 8) -> list[dict]:
    """«Похожие предложения» на странице авто: та же модель, год ±2, активные
    с фото; ближе по цене, году и **пробегу** — выше (та же мера, что у
    `sort=similar`; пробег — заметка владельца 11.11.2026: Camry 2018 с 60 тыс.
    и с 250 тыс. км — разные машины). Без модели/года — пусто (не угадываем)."""
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise LookupError("Объявление не найдено")
    if listing.car_model_id is None or listing.year is None:
        return []
    now = _now()
    price = float(listing.current_price_usd or 0)
    query = (
        _base_query(FeedFilter(with_photo=True), now=now)
        .where(
            Listing.id != listing.id,
            Listing.car_model_id == listing.car_model_id,
            Listing.year.between(listing.year - SIMILAR_YEAR_SPAN, listing.year + SIMILAR_YEAR_SPAN),
        )
        .order_by(
            _similar_score(listing) if price else func.abs(Listing.year - listing.year),
            Listing.id,
        )
        .limit(SIMILAR_CANDIDATES)
    )
    rows = list(session.scalars(query).all())[:limit]
    return _enrich(session, rows, now)


def _best_candidate(item: dict, now: datetime) -> best_picks.Candidate | None:
    market = item.get("market")
    if not market or item["is_foreign"] or market["diff_percent"] >= 0:
        return None
    first_seen = item["first_seen_at"]
    if first_seen.tzinfo is None:
        first_seen = first_seen.replace(tzinfo=timezone.utc)
    return best_picks.Candidate(
        ref=item,
        year=item["year"],
        photos=item["photo_count"],
        discount=-float(market["diff_percent"]),
        samples=int(market["samples"]),
        has_mileage=item["mileage_km"] is not None,
        age_hours=max((now - first_seen).total_seconds() / 3600, 0.0),
        model_key=(item["brand"], item["model"]),
        dup_key=(item["brand"], item["model"], item["year"], item["mileage_km"], item["price_usd"]),
    )


def best_now(session: Session, *, limit: int = 12) -> list[dict]:
    """«Лучшее сейчас» для сайта: выгодные по-настоящему, не самые дешёвые.

    Правила и ярусы нехватки — `services/best_picks.py` и `docs/planning/BEST_PICKS.md`.
    Сначала смотрим свежие за 24 ч; если набралось меньше `limit` — один раз расширяем
    пул до 72 ч (ярусы 2–3 берут из него), иначе отдаём сколько есть. Без проверки
    позиции у источника (её делает прогрев витрины бота), без побочных эффектов.
    """
    now = _now()
    cfg = best_picks.current_config()
    result: list[best_picks.Pick] = []
    for hours in (24, 72):
        base = _base_query(FeedFilter(with_photo=True), now=now).where(
            Listing.first_seen_at >= now - timedelta(hours=hours)
        )
        rows = list(session.scalars(_order(base, "fresh", None).limit(_DEAL_SCAN_MAX)).all())
        cands = [c for c in (_best_candidate(i, now) for i in _enrich(session, rows, now)) if c]
        result = best_picks.select(cands, size=limit, now_year=now.year, cfg=cfg)
        if len(result) >= limit:
            break
    return [p.cand.ref for p in result]


# --- гистограммы для ползунков ----------------------------------------------


@dataclass(frozen=True, slots=True)
class HistogramScale:
    """Шкала ползунка: `bins` корзин по `step` от `min`. Последняя корзина —
    «и больше» (у цены и пробега длинный хвост), первая — «и меньше» (год)."""

    min: int
    step: int
    bins: int

    @property
    def max(self) -> int:
        return self.min + self.step * (self.bins - 1)


def histogram_scales(now: datetime | None = None) -> dict[str, HistogramScale]:
    year_now = (now or _now()).year
    return {
        "price": HistogramScale(min=0, step=2_000, bins=26),  # 0 … 50 000+ $
        "year": HistogramScale(min=1995, step=1, bins=year_now - 1995 + 1),  # ≤1995 … текущий
        "mileage": HistogramScale(min=0, step=10_000, bins=31),  # 0 … 300 000+ км
    }


_HIST_COLUMNS = {
    "price": ("current_price_usd", ("price_from", "price_to")),
    "year": ("year", ("year_from", "year_to")),
    "mileage": ("mileage_km", ("mileage_from", "mileage_to")),
}


def _histogram(session: Session, flt: FeedFilter, key: str, scale: HistogramScale, now: datetime) -> list[int]:
    column_name, bounds = _HIST_COLUMNS[key]
    # Свой диапазон не применяем (иначе за ползунком пусто), остальные фильтры — да.
    own_free = replace(flt, **{name: None for name in bounds})
    sub = _base_query(own_free, now=now).subquery()
    column = sub.c[column_name]
    # `//` — целочисленное деление и в Postgres, и в SQLite (не `/`: SQLAlchemy 2
    # делает из него деление с дробью). Отрицательное (год < min) уходит в 0.
    # Корзина считается во внутреннем запросе, группировка — по её имени: иначе
    # Postgres сравнивает выражения в SELECT и GROUP BY с разными параметрами.
    bucket = (
        select(((cast(column, Integer) - scale.min) // scale.step).label("b"))
        .where(column.is_not(None))
        .subquery()
    )
    rows = session.execute(select(bucket.c.b, func.count()).group_by(bucket.c.b)).all()
    counts = [0] * scale.bins
    for index, count in rows:
        counts[min(max(int(index), 0), scale.bins - 1)] += count
    return counts


def feed_histograms(session: Session, flt: FeedFilter) -> dict:
    """Распределение цены, года и пробега под ползунки панели фильтров — с
    учётом остальных фильтров. Фильтр по оценке (`deal`) не учитывается: он не
    выражается в SQL, а гистограмма — подсказка формы, не счётчик."""
    now = _now()
    result = {}
    for key, scale in histogram_scales(now).items():
        result[key] = {
            "min": scale.min,
            "max": scale.max,
            "step": scale.step,
            "counts": _histogram(session, flt, key, scale, now),
        }
    return result
