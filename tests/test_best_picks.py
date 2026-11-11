"""«Лучшее сейчас»: общий скоринг сайта и бота (`services/best_picks.py`)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services import best_picks as bp
from app.services.best_picks import BestConfig, Candidate

NOW_YEAR = 2026
CFG = BestConfig()
_n = iter(range(1, 100_000))


def _c(year=2022, photos=8, discount=27.0, samples=60, mileage=True, age=2.0, model="Camry", ref=None) -> Candidate:
    n = next(_n)
    return Candidate(
        ref=ref or n,
        year=year,
        photos=photos,
        discount=discount,
        samples=samples,
        has_mileage=mileage,
        age_hours=age,
        model_key=model,
        dup_key=("k", n),
    )


def _ids(picks) -> list:
    return [p.cand.ref for p in picks]


def test_corridor_hides_suspicious_discounts() -> None:
    """−58 % — не «лучшее», а битая машина/ошибка цены; −27 % — да."""
    ok = _c(discount=27)
    assert _ids(bp.select([_c(discount=58), _c(discount=46), ok], size=12, now_year=NOW_YEAR, cfg=CFG)) == [ok.ref]


def test_soft_corridor_is_a_fallback_tier_not_the_default() -> None:
    strict, soft = _c(discount=27), _c(discount=38)
    ranked = bp.rank([soft, strict], now_year=NOW_YEAR, cfg=CFG)
    assert [(p.cand.ref, p.tier) for p in ranked] == [(strict.ref, 0), (soft.ref, 1)]


def test_min_photos_and_market_base() -> None:
    assert bp.rank([_c(photos=2), _c(photos=1)], now_year=NOW_YEAR, cfg=CFG) == []
    # база 15–19 пускается только крайним ярусом (72 ч, −10…−45 %) и не в «новый» блок
    thin = _c(samples=17, age=60, discount=27)
    assert [p.tier for p in bp.rank([thin], now_year=NOW_YEAR, cfg=CFG)] == [3]
    assert bp.rank([_c(samples=12, age=60)], now_year=NOW_YEAR, cfg=CFG) == []


def test_old_cars_need_a_good_score_and_pre_2005_never() -> None:
    assert bp.rank([_c(year=1998, photos=12, samples=300, discount=27)], now_year=NOW_YEAR, cfg=CFG) == []
    weak_old = _c(year=2008, photos=3, samples=20, mileage=False, discount=18)
    assert bp.rank([weak_old], now_year=NOW_YEAR, cfg=CFG) == []
    strong_old = _c(year=2008, photos=10, samples=300, discount=27)
    assert _ids(bp.rank([strong_old], now_year=NOW_YEAR, cfg=CFG)) == [strong_old.ref]


def test_score_prefers_new_well_photographed_and_plateau_discount() -> None:
    base = dict(samples=60, mileage=True)
    s = lambda **kw: bp.score(_c(**{**base, **kw}), now_year=NOW_YEAR)  # noqa: E731
    assert s(year=2024) > s(year=2012)
    assert s(discount=27) > s(discount=12) and s(discount=27) > s(discount=44)
    assert s(photos=10) > s(photos=3)
    assert s(samples=200) > s(samples=20)


def test_first_five_are_new_then_old() -> None:
    new = [_c(year=2024 - i % 3, discount=22 + i, model=f"N{i}") for i in range(8)]
    old = [_c(year=2009, photos=10, samples=300, discount=27, model=f"O{i}") for i in range(10)]
    top = bp.select(new + old, size=12, now_year=NOW_YEAR, cfg=CFG)
    assert len(top) == 12
    assert all(p.is_new for p in top[:5])
    assert not any(p.is_new for p in top[5:12])  # дальше — старые (пока они есть)


def test_at_most_two_cars_of_one_model() -> None:
    cams = [_c(model="Camry", discount=22 + i) for i in range(6)]
    top = bp.select(cams + [_c(model="RX")], size=12, now_year=NOW_YEAR, cfg=CFG)
    assert sum(1 for p in top if p.cand.model_key == "Camry") == 2 and len(top) == 3


def test_duplicates_collapse_to_the_best() -> None:
    a, b = _c(photos=8), _c(photos=4)
    twin = Candidate(**{**b.__dict__, "dup_key": a.dup_key}) if hasattr(b, "__dict__") else None
    if twin is None:  # slots=True: собираем вручную
        twin = Candidate(b.ref, b.year, b.photos, b.discount, b.samples, b.has_mileage, b.age_hours, b.model_key, a.dup_key)
    assert _ids(bp.select([twin, a], size=12, now_year=NOW_YEAR, cfg=CFG)) == [a.ref]


# --- нехватка: что если 12 не набирается -------------------------------------------


def test_shortage_returns_what_exists_without_padding_with_junk() -> None:
    good = [_c(model=f"M{i}") for i in range(3)]
    junk = [_c(photos=1), _c(discount=57), _c(year=1990), _c(samples=3)]
    top = bp.select(good + junk, size=12, now_year=NOW_YEAR, cfg=CFG)
    assert _ids(top) == _ids(bp.rank(good, now_year=NOW_YEAR, cfg=CFG))  # только три хороших
    assert bp.select([], size=12, now_year=NOW_YEAR, cfg=CFG) == []  # пусто — тоже честный ответ


def test_relaxed_tiers_fill_the_gap_but_rank_below_strict_ones() -> None:
    strict = [_c(model=f"S{i}", discount=27) for i in range(2)]
    soft = [_c(model=f"F{i}", discount=38) for i in range(2)]  # ярус 1
    older = [_c(model=f"W{i}", discount=27, age=40) for i in range(2)]  # ярус 2 (48 ч)
    thin = [_c(model=f"T{i}", discount=27, age=60, samples=16) for i in range(2)]  # ярус 3
    top = bp.select(thin + older + soft + strict, size=12, now_year=NOW_YEAR, cfg=CFG)
    assert [p.tier for p in top] == sorted(p.tier for p in top)  # строгие выше расслабленных
    assert {p.tier for p in top} == {0, 1, 2, 3} and len(top) == 8


def test_new_block_never_uses_the_loosest_tier_while_better_old_exist() -> None:
    loose_new = _c(year=2024, samples=16, age=60, model="LN")  # ярус 3
    good_old = _c(year=2009, photos=10, samples=300, model="GO")  # ярус 0
    top = bp.select([loose_new, good_old], size=12, now_year=NOW_YEAR, cfg=CFG)
    assert _ids(top) == [good_old.ref, loose_new.ref]


def test_alive_check_skips_removed_listings_and_backfills() -> None:
    cs = [_c(model=f"M{i}", discount=22 + i) for i in range(7)]
    dead = {cs[6].ref, cs[5].ref}
    top = bp.select(cs, size=5, now_year=NOW_YEAR, cfg=CFG, alive=lambda p: p.cand.ref not in dead)
    assert len(top) == 5 and not dead & set(_ids(top))
