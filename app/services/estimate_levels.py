"""Уровни точности оценки «Оценить авто» (решение владельца 19.11.2026).

| Уровень | Выборка | Кому |
|---|---|---|
| 1 | марка + модель + точный год, активные — **ровно как сейчас** (`get_vehicle_summary`) | всем |
| 2 | то же, но год ±1; соседние годы приведены к целевому поправкой на год | подписчикам и админам |
| 3 | уровень 2 + снятые с продажи за `ARCHIVE_DAYS` дней («старая база») | только админам |

Считаются **всегда все** (админу — все три, подписчику — 1 и 2), независимо от
того, хватает ли данных уровню 1: каждый уровень — самостоятельная оценка, а не
запасной вариант. Внутри уровня — тот же код, что у уровня 1
(`analytics.select_by_mileage`: окно пробега ±50 000 км, поправка регрессией).

Поправка на год: цены соседнего года умножаются на `(1 + шаг) ** (год - их год)`.
Шаг — из данных этой модели: медианы цен по годам в пределах ±3 лет, наклон
log(медианы) по году с весом числа объявлений, в пределах 0–25 % за год. Если
лет с данными меньше двух — шаг по умолчанию `DEFAULT_YEAR_STEP` с пометкой
`year_step_source="default"`.

Снятые (уровень 3): последняя цена перед снятием, без поправки на изменение
рынка (индекса цен ещё нет — задача 32 плана 18.11). Снятое ≠ проданное. Повторы
одной машины («Вероятно, та же машина», `listing_same_car`) берутся один раз.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Listing, ListingSameCar
from app.services.analytics import MILEAGE_WINDOW_KM, WINDOW_NEARBY, select_by_mileage

ARCHIVE_DAYS = 120
YEAR_SPAN = 1  # уровень 2: год ±1
SLOPE_SPAN = 3  # шаг года считаем по ±3 годам
DEFAULT_YEAR_STEP = 0.07  # 7 % за год, если по данным модели не посчитать
MAX_YEAR_STEP = 0.25
COHORT_LIMIT = 5000  # ±3 года популярной модели (Camry) — ~2 тыс.; потолок от случайного взрыва

LABELS = {
    1: "Точный год, в продаже",
    2: "Год ±1, в продаже",
    3: "Год ±1 + снятые за 120 дней",
}


@dataclass(frozen=True, slots=True)
class _Point:
    """Объявление в выборке уровня: цена уже приведена к целевому году."""

    mileage_km: int | None
    current_price_usd: float
    year: int
    archived: bool


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _cohort(
    session: Session,
    *,
    brand_id: int,
    model_id: int,
    years: tuple[int, int],
    active: bool,
    since=None,
    fuel: str | None = None,
):
    conditions = [
        Listing.analytics_eligible.is_(True),
        Listing.brand_id == brand_id,
        Listing.car_model_id == model_id,
        Listing.year >= years[0],
        Listing.year <= years[1],
        Listing.current_price_usd.is_not(None),
    ]
    if fuel is not None:
        conditions.append(Listing.fuel_group == fuel)
    if active:
        conditions.append(Listing.is_active.is_(True))
    else:
        conditions += [
            Listing.is_active.is_(False),
            Listing.deactivated_at >= since,
            Listing.price_suspect.is_not(True),
        ]
    order = Listing.deactivated_at.desc() if not active else Listing.id
    return list(session.scalars(select(Listing).where(*conditions).order_by(order).limit(COHORT_LIMIT)).all())


def year_step(listings: list[Listing], year: int) -> tuple[float, str]:
    """Насколько (доля) машина на год новее дороже — по медианам цен по годам."""
    by_year: dict[int, list[float]] = {}
    for item in listings:
        if item.year is not None and abs(item.year - year) <= SLOPE_SPAN:
            by_year.setdefault(item.year, []).append(float(item.current_price_usd))
    points = [(y, math.log(median(p)), len(p)) for y, p in by_year.items() if p and median(p) > 0]
    if len(points) < 2:
        return DEFAULT_YEAR_STEP, "default"
    total = sum(w for *_, w in points)
    x_avg = sum(x * w for x, _, w in points) / total
    y_avg = sum(y * w for _, y, w in points) / total
    denominator = sum(w * (x - x_avg) ** 2 for x, _, w in points)
    if denominator == 0:
        return DEFAULT_YEAR_STEP, "default"
    slope = sum(w * (x - x_avg) * (y - y_avg) for x, y, w in points) / denominator
    step = math.exp(slope) - 1
    return max(0.0, min(step, MAX_YEAR_STEP)), "data"


def _dedupe_archive(session: Session, archive: list[Listing], active_ids: set[int]) -> list[Listing]:
    """Снятые повторы одной машины — один раз; и не дублируем то, что ещё в продаже."""
    if not archive:
        return []
    ids = [item.id for item in archive]
    pairs: dict[int, set[int]] = {}
    for left, right in session.execute(
        select(ListingSameCar.listing_id, ListingSameCar.other_id).where(ListingSameCar.listing_id.in_(ids))
    ).all():
        pairs.setdefault(left, set()).add(right)
    taken: set[int] = set(active_ids)
    result: list[Listing] = []
    for item in archive:  # свежеснятые первыми
        if pairs.get(item.id, set()) & taken:
            continue
        taken.add(item.id)
        result.append(item)
    return result


def _level(level: int, points: list[_Point], mileage_km: int | None, extra: dict) -> dict:
    base = {"level": level, "label": LABELS[level], **extra}
    if not points:
        return {
            **base,
            "available": False,
            "estimated_price": None,
            "estimated_range": None,
            "samples": 0,
            "confidence": "none",
            "window": None,
            "years": [],
            "archive_count": 0,
        }
    window = select_by_mileage(points, mileage_km)  # type: ignore[arg-type]
    prices = window.prices or [p.current_price_usd for p in points]
    selected = (
        [p for p in points if p.mileage_km is not None and abs(p.mileage_km - mileage_km) <= MILEAGE_WINDOW_KM]
        if window.window == WINDOW_NEARBY and mileage_km is not None
        else points
    )
    years: dict[int, int] = {}
    for p in selected:
        years[p.year] = years.get(p.year, 0) + 1
    samples = len(prices)
    return {
        **base,
        "available": True,
        "estimated_price": round(window.estimated_price, 2),
        "estimated_range": {"min": round(_percentile(prices, 0.25), 2), "max": round(_percentile(prices, 0.75), 2)},
        "samples": samples,
        "confidence": "high" if samples >= 20 else "medium" if samples >= 8 else "low",
        "window": window.window,
        "years": [{"year": y, "count": n} for y, n in sorted(years.items())],
        "archive_count": sum(1 for p in selected if p.archived),
    }


def estimate_levels(
    session: Session,
    *,
    brand_id: int,
    model_id: int,
    year: int,
    mileage_km: int | None = None,
    fuel: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Все три уровня. Кому что показать — решает ручка (по роли). `fuel` — тип
    топлива (как у основной оценки): сужает все уровни, шаг года считается по нему же."""
    now = now or datetime.now(timezone.utc)
    wide = _cohort(
        session, brand_id=brand_id, model_id=model_id, years=(year - SLOPE_SPAN, year + SLOPE_SPAN), active=True, fuel=fuel
    )
    step, step_source = year_step(wide, year)

    def point(item: Listing, archived: bool = False) -> _Point:
        factor = (1 + step) ** (year - item.year)
        return _Point(item.mileage_km, float(item.current_price_usd) * factor, item.year, archived)

    exact = [point(item) for item in wide if item.year == year]
    near = [point(item) for item in wide if abs(item.year - year) <= YEAR_SPAN]
    archive = _cohort(
        session,
        brand_id=brand_id,
        model_id=model_id,
        years=(year - YEAR_SPAN, year + YEAR_SPAN),
        active=False,
        since=now - timedelta(days=ARCHIVE_DAYS),
        fuel=fuel,
    )
    active_ids = {item.id for item in wide if abs(item.year - year) <= YEAR_SPAN}
    archived = [point(item, archived=True) for item in _dedupe_archive(session, archive, active_ids)]

    step_info = {"year_step_percent": round(step * 100, 1), "year_step_source": step_source}
    return [
        _level(1, exact, mileage_km, {"year_step_percent": None, "year_step_source": None}),
        _level(2, near, mileage_km, step_info),
        _level(3, near + archived, mileage_km, step_info),
    ]
