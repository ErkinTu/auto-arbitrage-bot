"""Метка «Вероятно, та же машина» (план 18.11 № 20, DU-1 на хэшах фото).

Решение владельца 19.11.2026: на сайте только «вероятно» (точность ≥ 95 %),
ничего не скрываем. Замер (`docs/data/DUPLICATES_MEASURE_2026-11-19.md`) показал,
что такую точность даёт только совпадение снимка; телефон, описание и пробег
без фото — 13–64 %, поэтому в метку не идут (очередь «возможно» — админка, DU-7).

Правило пары:
- та же марка, модель и год; топливо не противоречит (NULL — не конфликт);
- какой-то из первых кадров совпал: расстояние dHash ≤ `SAME_CAR_MAX_DISTANCE`;
- пробег не противоречит: оба известны → разница ≤ max(5000 км, 10 %);
- кадр не «общий»: точный хэш, встреченный у > `GENERIC_FRAME_LISTINGS`
  объявлений (баннер салона, фото из каталога), не считается;
- у объявления не больше `MAX_MATCHES` совпадений: больше — это склад/шаблон
  дилера, а не перевыставленная машина; такие пары отбрасываются целиком.

Берём активные и снятые за последние `WINDOW_DAYS` дней: «выставлялась раньше
за $X» — главное, что метка даёт. Новые (< 1000 км) не хэшируются вовсе.
Пересчёт целиком (несколько тысяч пар) — раз в `SAME_CAR_REBUILD_MINUTES`.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import combinations

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import Listing, ListingPhotoHash, ListingSameCar
from app.services.photo_hashes import distance, is_informative

logger = logging.getLogger(__name__)

WINDOW_DAYS = 90
GENERIC_FRAME_LISTINGS = 5
MAX_MATCHES = 5
MILEAGE_ABS_KM = 5000
MILEAGE_REL = 0.10


@dataclass(frozen=True)
class Item:
    id: int
    block: tuple[int, int, int]
    fuel: str | None
    km: int | None
    hashes: tuple[int, ...]


def _fuel_ok(a: str | None, b: str | None) -> bool:
    return a is None or b is None or a == b


def _km_ok(a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return True
    return abs(a - b) <= max(MILEAGE_ABS_KM, MILEAGE_REL * max(a, b))


def match_pairs(items: list[Item], max_distance: int) -> dict[tuple[int, int], int]:
    """Пары (меньший id, больший id) → наименьшее расстояние кадров. Чистая функция."""
    owners: dict[int, set[int]] = defaultdict(set)
    for item in items:
        for h in item.hashes:
            owners[h].add(item.id)
    generic = {h for h, ids in owners.items() if len(ids) > GENERIC_FRAME_LISTINGS}

    blocks: dict[tuple[int, int, int], list[Item]] = defaultdict(list)
    for item in items:
        frames = tuple(h for h in item.hashes if is_informative(h) and h not in generic)
        if frames:
            blocks[item.block].append(Item(item.id, item.block, item.fuel, item.km, frames))

    pairs: dict[tuple[int, int], int] = {}
    for members in blocks.values():
        for a, b in combinations(members, 2):
            if not (_fuel_ok(a.fuel, b.fuel) and _km_ok(a.km, b.km)):
                continue
            best = min(distance(x, y) for x in a.hashes for y in b.hashes)
            if best <= max_distance:
                pairs[(min(a.id, b.id), max(a.id, b.id))] = best

    degree = Counter(i for pair in pairs for i in pair)
    crowded = {i for i, n in degree.items() if n > MAX_MATCHES}
    return {p: d for p, d in pairs.items() if p[0] not in crowded and p[1] not in crowded}


def load_items(session: Session, now: datetime) -> list[Item]:
    since = now - timedelta(days=WINDOW_DAYS)
    rows = session.execute(
        select(
            Listing.id, Listing.brand_id, Listing.car_model_id, Listing.year,
            Listing.fuel_group, Listing.mileage_km, ListingPhotoHash.hashes,
        )
        .join(ListingPhotoHash, ListingPhotoHash.listing_id == Listing.id)
        .where(
            ListingPhotoHash.frames > 0,
            Listing.brand_id.is_not(None),
            Listing.car_model_id.is_not(None),
            Listing.year.is_not(None),
            Listing.price_suspect.is_not(True),
            or_(Listing.is_active.is_(True), Listing.deactivated_at >= since),
        )
    ).all()
    return [
        Item(lid, (brand, model, year), fuel, km, tuple(int(h) for h in hashes or ()))
        for lid, brand, model, year, fuel, km, hashes in rows
    ]


def rebuild(session: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Пересчитать все пары и заменить таблицу одной транзакцией."""
    now = now or datetime.now(timezone.utc)
    items = load_items(session, now)
    pairs = match_pairs(items, get_settings().same_car_max_distance)
    rows = []
    for (a, b), dist in pairs.items():
        rows.append({"listing_id": a, "other_id": b, "distance": dist, "computed_at": now})
        rows.append({"listing_id": b, "other_id": a, "distance": dist, "computed_at": now})
    session.execute(delete(ListingSameCar))
    if rows:
        session.execute(insert(ListingSameCar), rows)
    session.commit()
    stats = {"listings": len(items), "pairs": len(pairs), "marked": len({r["listing_id"] for r in rows})}
    logger.info("same car rebuilt: %s", stats)
    return stats


def rebuild_if_due(last_at: datetime | None, *, now: datetime | None = None) -> datetime | None:
    """Для цикла worker-а: пересчитать, если прошло `SAME_CAR_REBUILD_MINUTES`."""
    from app.db.session import SessionLocal

    now = now or datetime.now(timezone.utc)
    minutes = get_settings().same_car_rebuild_minutes
    if minutes <= 0 or (last_at is not None and now - last_at < timedelta(minutes=minutes)):
        return None
    with SessionLocal() as session:
        rebuild(session, now=now)
    return now


def other_ids(session: Session, listing_id: int) -> list[int]:
    """Объявления той же машины — ближе по снимку первыми."""
    return list(
        session.scalars(
            select(ListingSameCar.other_id)
            .where(ListingSameCar.listing_id == listing_id)
            .order_by(ListingSameCar.distance, ListingSameCar.other_id.desc())
        )
    )


def counts(session: Session, listing_ids: list[int]) -> dict[int, int]:
    """Для карточек ленты: сколько ещё объявлений той же машины."""
    if not listing_ids:
        return {}
    return dict(
        session.execute(
            select(ListingSameCar.listing_id, func.count())
            .where(ListingSameCar.listing_id.in_(listing_ids))
            .group_by(ListingSameCar.listing_id)
        ).all()
    )
