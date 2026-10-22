"""Пресеты фильтров и группы марок.

Пресет — готовый фильтр одной кнопкой: новичок не проходит семь шагов визарда,
чтобы бот перестал молчать (у 55% пользователей на 21.10.2026 не было ни одного
фильтра). Пресетов не больше четырёх плюс «Свой»: больше кнопок — снова выбор,
от которого человек уходит.

Две ловушки, решённые схемой, а не текстом:

- **Группа марок** («Японцы») хранится одним полем `user_filters.brand_group`,
  а не несколькими фильтрами по марке: иначе один пресет съедал бы 4 слота из 5.
- **Возраст** хранится как «не старше N лет» (`max_age_years`), а год считается
  при матчинге. `year_from=2016` через год молча превратился бы в «до 11 лет».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Brand
from app.services.regions import BISHKEK, LOCAL

# Код группы -> (подпись, марки). Марки — по имени каталога (`brands.name`):
# id в разных базах разные, а имя — то же, что в ALLOWED_BRANDS.
BRAND_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "japan": ("Японские марки", ("Toyota", "Lexus", "Honda", "Subaru")),
}


def brand_group_label(code: str | None) -> str | None:
    group = BRAND_GROUPS.get(code or "")
    return group[0] if group else None


def is_valid_brand_group(code: str | None) -> bool:
    return code is None or code in BRAND_GROUPS


def load_brand_groups(session: Session) -> dict[str, frozenset[int]]:
    """Код группы -> id марок в ЭТОЙ базе. Одним запросом на цикл рассылки.

    Марки группы, которых нет в каталоге (не включены в ALLOWED_BRANDS),
    просто не попадают в множество — фильтр по группе их и не найдёт.
    """
    names = {name for _, members in BRAND_GROUPS.values() for name in members}
    ids_by_name = {
        row.name: row.id
        for row in session.execute(select(Brand.id, Brand.name).where(Brand.name.in_(names)))
    }
    return {
        code: frozenset(ids_by_name[name] for name in members if name in ids_by_name)
        for code, (_, members) in BRAND_GROUPS.items()
    }


def min_year_for_age(max_age_years: int, now: datetime | None = None) -> int:
    """«Не старше N лет» -> минимальный год выпуска на сегодня.

    Машина 2016 года в 2026-м — ровно десятилетняя, и под «до 10 лет» она
    подходит: считаем по году, а не по дате выпуска (её в объявлении нет).
    """
    now = now or datetime.now(timezone.utc)
    return now.year - max_age_years


@dataclass(frozen=True, slots=True)
class FilterPreset:
    code: str
    button: str  # подпись кнопки
    summary: str  # «Поставил фильтр: …» — человеческими словами
    region: str | None
    below_market_percent: int
    price_max_usd: float | None = None
    brand_group: str | None = None
    max_age_years: int | None = None


BASE_PRESET_CODE = "kr_below"


def presets() -> tuple[FilterPreset, ...]:
    """Реестр пресетов. Функция, а не константа: порог базового — из настроек.

    Все пресеты — «ниже рынка»: так настроены 87% фильтров, и без порога даже
    узкий пресет присылал бы сотни авто в сутки (≤$8k — треть рынка). Порог
    базовый у всех, кроме «Японцев»: там сегмент сужен и марками, и возрастом,
    и при 20% почти всегда тишина. «До $8 000» узким только кажется — дешёвый
    сегмент самый шумный (замер `cli.py preset-volume` на копии базы: при 10%
    он давал больше пушей, чем базовый фильтр по всей КР).
    """
    base = get_settings().bot_base_filter_percent
    return (
        FilterPreset(
            code=BASE_PRESET_CODE,
            button=f"🇰🇬 Вся КР, ниже рынка на {base}%",
            summary=f"КР, ниже рынка на {base}%",
            region=LOCAL,
            below_market_percent=base,
        ),
        FilterPreset(
            code="bishkek_below",
            button=f"🏙 Бишкек, ниже рынка на {base}%",
            summary=f"Бишкек, ниже рынка на {base}%",
            region=BISHKEK,
            below_market_percent=base,
        ),
        FilterPreset(
            code="budget_8k",
            button=f"💵 До $8 000, ниже рынка на {base}%",
            summary=f"КР, до $8 000, ниже рынка на {base}%",
            region=LOCAL,
            below_market_percent=base,
            price_max_usd=8000.0,
        ),
        FilterPreset(
            code="japan_10y",
            button="🇯🇵 Японцы до 10 лет, ниже рынка на 10%",
            summary="КР, японские марки не старше 10 лет, ниже рынка на 10%",
            region=LOCAL,
            below_market_percent=10,
            brand_group="japan",
            max_age_years=10,
        ),
    )


def find_preset(code: str) -> FilterPreset | None:
    return next((preset for preset in presets() if preset.code == code), None)


def base_preset() -> FilterPreset:
    preset = find_preset(BASE_PRESET_CODE)
    assert preset is not None
    return preset
