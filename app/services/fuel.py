"""Тип топлива: единые группы для обоих источников и фильтра бота (план 01.11.2026, п. Е).

Источники пишут топливо по-разному, поэтому в `listings.fuel_group` лежит
каноническая группа, а сырое значение lalafo остаётся в `listings.fuel_type`.

- **lalafo** — поле «Топливо» карточки, бывает списком через запятую
  («Бензин, Газ»). Разбор — `fuel_group_from_lalafo`.
- **mashina** — в ленте API топлива нет вовсе, но API фильтрует по нему
  (`fuel_type: {"in": "гибрид"}`), значений ровно пять (замер 01.11.2026:
  54 035 из 54 072 объявлений). Разметка — `services/fuel_labeling.py`.

Решения владельца 01.11.2026: плагин-гибрид — это гибрид (честно отличить его
не может ни один источник), дизель — отдельная группа, «бензин/газ» (ГБО) —
отдельно от бензина, в фильтре можно выбрать несколько групп сразу.
"""

from __future__ import annotations

from typing import Iterable

PETROL = "petrol"
PETROL_GAS = "petrol_gas"
HYBRID = "hybrid"
ELECTRIC = "electric"
DIESEL = "diesel"

# Порядок — как в панели фильтра (порядок владельца).
FUEL_GROUPS: tuple[str, ...] = (PETROL, PETROL_GAS, HYBRID, ELECTRIC, DIESEL)
FUEL_LABELS: dict[str, str] = {
    PETROL: "Бензин",
    PETROL_GAS: "Бензин/газ",
    HYBRID: "Гибрид",
    ELECTRIC: "Электро",
    DIESEL: "Дизель",
}

# Значение `fuel_type` в API mashina → группа. «газ» у mashina — бензиновая
# машина с ГБО (по заголовку её от бензиновой не отличить).
MASHINA_FUEL_VALUES: dict[str, str] = {
    "бензин": PETROL,
    "газ": PETROL_GAS,
    "гибрид": HYBRID,
    "электро": ELECTRIC,
    "дизель": DIESEL,
}

# Части значения lalafo (в нижнем регистре).
_L_PETROL = "бензин"
_L_GAS = {"газ", "газ-бензин (гбо)"}
_L_HYBRID = "гибрид"
_L_ELECTRIC = "электромобиль"
_L_DIESEL = "дизель"
_L_KNOWN = {_L_PETROL, _L_HYBRID, _L_ELECTRIC, _L_DIESEL, *_L_GAS}


def fuel_group_from_lalafo(value: str | None) -> str | None:
    """«Бензин, Газ» → `petrol_gas`; None — не указано или противоречиво.

    Сочетания (замер 01.11.2026, виденные за 30 дней): «Бензин, Гибрид» и
    «Бензин, Гибрид, Электромобиль» (~120/мес, похоже на плагины) — гибрид;
    «Газ», «Бензин, Газ», «Газ-бензин (ГБО)» — бензин/газ. Дизель вместе с
    чем-то ещё («Бензин, Дизель», «все пять сразу») — мусор, группы нет:
    под фильтр по топливу попадает только доказанное.
    """
    if not value:
        return None
    parts = {part.strip().lower() for part in value.split(",") if part.strip()}
    if not parts or not parts <= _L_KNOWN:
        return None
    if _L_DIESEL in parts:
        return DIESEL if parts == {_L_DIESEL} else None
    if _L_HYBRID in parts:
        return HYBRID
    if _L_ELECTRIC in parts:
        if parts == {_L_ELECTRIC}:
            return ELECTRIC
        # «Бензин, Электромобиль» — по смыслу плагин/последовательный гибрид.
        return HYBRID if parts == {_L_PETROL, _L_ELECTRIC} else None
    if parts & _L_GAS:
        return PETROL_GAS
    return PETROL  # остался только «Бензин»


def pack_fuel_groups(groups: Iterable[str] | None) -> str | None:
    """Выбор в фильтре → строка для `user_filters.fuel_groups` («hybrid,electric»).

    Порядок канонический — два одинаковых выбора дают одну строку (проверка
    дубликата фильтра сравнивает строки). Пусто или все пять — None («все типы»).
    """
    chosen = {g for g in (groups or ()) if g in FUEL_LABELS}
    if not chosen or len(chosen) == len(FUEL_GROUPS):
        return None
    return ",".join(g for g in FUEL_GROUPS if g in chosen)


def unpack_fuel_groups(value: str | None) -> tuple[str, ...]:
    """Строка из `user_filters.fuel_groups` → группы; пусто — «все типы»."""
    if not value:
        return ()
    chosen = set(value.split(","))
    return tuple(g for g in FUEL_GROUPS if g in chosen)


def fuel_groups_label(value: str | None) -> str | None:
    """«Гибрид, Электро» для итога фильтра; None — все типы (строку не пишем)."""
    groups = unpack_fuel_groups(value)
    return ", ".join(FUEL_LABELS[g] for g in groups) if groups else None
