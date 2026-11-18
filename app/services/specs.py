"""Канонические группы характеристик для фильтров сайта.

Источники пишут одно и то же по-разному: mashina — «внедорожник 5 дв.»,
«хэтчбек 3 дв.», lalafo — «Внедорожник», «Хэтчбэк»; бывают «-1» и мусор вроде
«{name: Автомат, category_id: 1}». Сайту нужны понятные чипы без дублей.

Как с регионами (`services/regions.py`): в базе лежит строка источника как
есть, группа считается в Python по правилам ниже, а фильтр раскрывает выбранные
группы в точный список строк из базы. Миграции и бэкфилла не нужно; новое
написание у источника — одна строка правила.
"""

from __future__ import annotations

import re

# (код, подпись, признаки в нормализованной строке) — порядок важен: первое
# совпадение побеждает («пикап» раньше «кабин», «минивэн» раньше «вэн»).
BODY_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("sedan", "Седан", ("седан",)),
    ("suv", "Внедорожник / кроссовер", ("внедорожник", "кроссовер")),
    ("hatchback", "Хэтчбек", ("хэтчбэк", "хэтчбек")),
    ("liftback", "Лифтбек / фастбек", ("лифтбек", "фастбек")),
    ("wagon", "Универсал", ("универсал",)),
    ("minivan", "Минивэн", ("минивэн", "компактвэн", "микровэн")),
    ("pickup", "Пикап", ("пикап",)),
    ("coupe", "Купе", ("купе", "хардтоп")),
    ("convertible", "Кабриолет / родстер", ("кабриолет", "родстер", "roadster", "тарга")),
    ("van", "Фургон / бус", ("фургон", "бус")),
]
GEARBOX_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("automatic", "Автомат", ("автомат", "типтроник")),
    ("manual", "Механика", ("механик",)),
    ("cvt", "Вариатор", ("вариатор",)),
    ("robot", "Робот", ("робот",)),
]
DRIVE_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("front", "Передний", ("передн",)),
    ("rear", "Задний", ("задн",)),
    ("awd", "Полный", ("полный", "4wd", "awd")),
]
STEERING_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("left", "Левый", ("слева", "левый")),
    ("right", "Правый", ("справа", "правый")),
]

SPEC_GROUPS: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {
    "body_type": BODY_GROUPS,
    "gearbox": GEARBOX_GROUPS,
    "drive_type": DRIVE_GROUPS,
    "steering_wheel": STEERING_GROUPS,
}


def _norm(raw: str) -> str:
    return re.sub(r"[\s_]+", " ", raw.strip().lower().replace("ё", "е"))


def spec_group(field: str, raw: str | None) -> str | None:
    """Код группы для строки источника или None (неизвестно, «-1», мусор)."""
    if not raw:
        return None
    text = _norm(raw)
    for code, _label, needles in SPEC_GROUPS[field]:
        if any(needle in text for needle in needles):
            return code
    return None


def group_labels(field: str) -> dict[str, str]:
    return {code: label for code, label, _ in SPEC_GROUPS[field]}


def raw_values_for(field: str, values: dict[str, int], codes: list[str]) -> list[str]:
    """Строки из базы (`values` — написание → число), попадающие под группы."""
    wanted = set(codes)
    return sorted(raw for raw in values if spec_group(field, raw) in wanted)
