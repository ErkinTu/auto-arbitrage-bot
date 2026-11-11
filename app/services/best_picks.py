"""«Лучшее сейчас»: общий отбор и порядок для сайта (`/public/best`) и бота (`/best`).

Решения владельца 12.11.2026 (разбор: сайт показывал наверху старьё 1988–2001
со скидкой 57–59 %, «чем дешевле, тем лучше» — неверно):

- лучшее = **выгодно по-настоящему** (скидка к рынку в «здоровом» коридоре), а не
  «максимально дёшево»: скидка 50 %+ — это битая машина, ошибка цены, рассрочка;
- минимум 3 фото; база рынка — не меньше 20 похожих (мягче — только в крайнем ярусе);
- первые 5 мест — **новые** (2018+, т. е. не старше `new_age_years` = 8 лет), дальше —
  старые; машины до 2005 г. не берём, 2005–2017 — только с хорошим скором;
- коридор «мягкий»: основной −20…−35 %, при нехватке — шире (ярусы ниже);
- не больше 2 машин одной модели (иначе карусель из десяти Camry), повторы склеиваются.

**Если хороших не набирается** (ночью объявлений мало, тихий день): отбор идёт
ЯРУСАМИ — каждый следующий шире предыдущего по окну свежести, коридору скидки и (в
последнем) размеру базы рынка. Позиции прежних ярусов всегда выше новых. Минимум фото
и потолок скидки не ослабляются никогда. Если даже после последнего яруса меньше
`size` — возвращаем сколько есть (честно, без добивки мусором); пустой результат —
карусель на сайте скрывается, в боте — прежний пустой ответ витрины.

Модуль чистый (без БД и сети): кандидаты — `Candidate`, их собирают вызывающие
(`public_feed.best_now`, `bot.showcase.compute_showcase`).
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Tier:
    """Ярус отбора: чем выше номер, тем шире условия."""

    hours: int  # окно свежести: объявление не старше N часов (по первому появлению)
    lo: float  # скидка к рынку от, %
    hi: float  # скидка к рынку до, %
    min_samples: int  # минимум похожих объявлений в базе рынка


# Ярус 0 — основной (коридор −20…−35 %, как просил владелец); ярус 1 — «мягкий» коридор
# −15…−40 %; ярусы 2–3 включаются только при нехватке: окно 48 → 72 ч, затем коридор
# −10…−45 % и база от 15. Выше −45 % не берём нигде: это не «выгодно», а «подозрительно».
TIERS: tuple[Tier, ...] = (
    Tier(24, 20.0, 35.0, 20),
    Tier(24, 15.0, 40.0, 20),
    Tier(48, 15.0, 40.0, 20),
    Tier(72, 10.0, 45.0, 15),
)
# В «новый» блок (первые места) берём новые машины не расслабленнее этого яруса:
# ярус 3 (72 ч, −10…−45 %, база 15) на самые видные места не годится.
NEW_TIER_MAX = 2

W_DISCOUNT, W_YEAR, W_PHOTOS, W_TRUST = 0.40, 0.30, 0.15, 0.15


@dataclass(frozen=True, slots=True)
class BestConfig:
    min_photos: int = 3
    new_age_years: int = 8  # «новая» = не старше 8 лет (2018+ в 2026)
    new_slots: int = 5  # сколько первых мест отдано новым
    min_year: int = 2005  # старше не берём вообще
    old_min_score: float = 0.55  # «хороший скор» для не новых машин
    max_per_model: int = 2


def current_config() -> BestConfig:
    from app.core.config import get_settings

    s = get_settings()
    return BestConfig(
        min_photos=s.best_min_photos,
        new_age_years=s.best_new_age_years,
        new_slots=s.best_new_slots,
        min_year=s.best_min_year,
        old_min_score=s.best_old_min_score,
        max_per_model=s.best_max_per_model,
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    ref: Any  # что вернуть вызывающему (позиция сайта / Listing + оценка)
    year: int | None
    photos: int
    discount: float  # % НИЖЕ рынка, положительное число
    samples: int  # сколько похожих в базе рынка
    has_mileage: bool
    age_hours: float  # сколько часов объявлению у нас
    model_key: Hashable  # для лимита «не больше N одной модели»
    dup_key: Hashable  # та же машина на двух источниках / переподана


@dataclass(frozen=True, slots=True)
class Pick:
    cand: Candidate
    tier: int
    score: float
    is_new: bool


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def discount_score(p: float) -> float:
    """1 на плато −20…−35 %, к краям спадает до 0 (−8 % и −50 %)."""
    if 20 <= p <= 35:
        return 1.0
    if p < 20:
        return _clamp((p - 8) / 12)
    return _clamp((50 - p) / 15)


def year_score(age: int) -> float:
    if age <= 5:
        return 1.0
    if age <= 8:
        return 0.8
    if age <= 11:
        return 0.6
    if age <= 16:
        return 0.4
    if age <= 21:
        return 0.2
    return 0.0


def photos_score(n: int) -> float:
    return _clamp((n - 2) / 6)  # 3 фото — минимум (0,17), 8+ — полный балл


def trust_score(samples: int, has_mileage: bool) -> float:
    base = _clamp(math.log10(max(samples, 1) / 20))  # база 20 → 0, 200 → 1
    return 0.6 * base + 0.4 * (1.0 if has_mileage else 0.0)


def score(c: Candidate, *, now_year: int) -> float:
    age = max(now_year - (c.year or now_year), 0)
    return (
        W_DISCOUNT * discount_score(c.discount)
        + W_YEAR * year_score(age)
        + W_PHOTOS * photos_score(c.photos)
        + W_TRUST * trust_score(c.samples, c.has_mileage)
    )


def _key(p: Pick) -> tuple:
    return (p.tier, -round(p.score, 6), p.cand.age_hours)


def _admit(c: Candidate, *, now_year: int, cfg: BestConfig, tiers: tuple[Tier, ...]) -> Pick | None:
    if c.year is None or c.year < cfg.min_year or c.photos < cfg.min_photos:
        return None
    s = score(c, now_year=now_year)
    is_new = c.year >= now_year - cfg.new_age_years
    if not is_new and s < cfg.old_min_score:
        return None  # 2005–2017 — только «хорошие по скору»
    for i, t in enumerate(tiers):
        if c.age_hours <= t.hours and t.lo <= c.discount <= t.hi and c.samples >= t.min_samples:
            return Pick(c, i, s, is_new)
    return None


def rank(
    cands: Iterable[Candidate],
    *,
    now_year: int,
    cfg: BestConfig | None = None,
    tiers: tuple[Tier, ...] | None = None,
) -> list[Pick]:
    """Допущенные кандидаты по убыванию «качества»: сначала ярус, затем скор.
    Повторы (`dup_key`) схлопываются — остаётся лучший."""
    cfg = cfg or current_config()
    tiers = TIERS if tiers is None else tiers
    best: dict[Hashable, Pick] = {}
    for c in cands:
        pick = _admit(c, now_year=now_year, cfg=cfg, tiers=tiers)
        if pick is None:
            continue
        cur = best.get(c.dup_key)
        if cur is None or _key(pick) < _key(cur):
            best[c.dup_key] = pick
    return sorted(best.values(), key=_key)


def arrange(
    picks: Iterable[Pick],
    *,
    size: int,
    cfg: BestConfig | None = None,
    alive: Callable[[Pick], bool] | None = None,
) -> list[Pick]:
    """Порядок карусели: `new_slots` новых (по убыванию качества) → старые → остальные
    новые. Не больше `max_per_model` одной модели. `alive` — проверка «не снято»
    (бот): мёртвые пропускаются и не занимают место."""
    cfg = cfg or current_config()
    ordered = sorted(picks, key=_key)
    result: list[Pick] = []
    seen: set[int] = set()
    per_model: Counter = Counter()

    def try_add(p: Pick) -> None:
        if id(p) in seen or len(result) >= size:
            return
        if per_model[p.cand.model_key] >= cfg.max_per_model:
            return
        if alive is not None and not alive(p):
            seen.add(id(p))
            return
        seen.add(id(p))
        per_model[p.cand.model_key] += 1
        result.append(p)

    for p in ordered:
        if len(result) >= min(cfg.new_slots, size):
            break
        if p.is_new and p.tier <= NEW_TIER_MAX:
            try_add(p)
    for p in ordered:
        if not p.is_new:
            try_add(p)
    for p in ordered:
        try_add(p)
    return result


def select(
    cands: Iterable[Candidate],
    *,
    size: int,
    now_year: int,
    cfg: BestConfig | None = None,
    alive: Callable[[Pick], bool] | None = None,
) -> list[Pick]:
    cfg = cfg or current_config()
    result = arrange(rank(cands, now_year=now_year, cfg=cfg), size=size, cfg=cfg, alive=alive)
    if len(result) < size:
        logger.warning("best picks: набралось %s из %s (ярусы исчерпаны)", len(result), size)
    return result
