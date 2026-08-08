"""Чёрный список пользователя: «это не показывать никогда».

Почему отдельная сущность, а не поле фильтра — см. docstring `UserBlock`.
Здесь только чистая логика (нормализация значений и проверка объявления),
чтобы её одинаково видели и матчинг, и хендлеры бота, и тесты.

Правило применения: запрет ГЛОБАЛЬНЫЙ — срабатывает поверх любого фильтра
пользователя. Это осознанно сильнее фильтра: человек говорит «никогда», а не
«не в этом фильтре».
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Listing, UserBlock

KIND_BRAND = "brand"
KIND_MODEL = "model"
KIND_CITY = "city"
KINDS = (KIND_BRAND, KIND_MODEL, KIND_CITY)

# Потолок на пользователя: чёрный список — короткий список исключений, а не
# второй язык запросов. Упёрлись — значит фильтр задан слишком широко.
MAX_BLOCKS_PER_USER = 20


def normalize_city(city: str | None) -> str:
    """Город к сравнимому виду: регистр и края источник пишет как попало."""
    return (city or "").strip().lower()


def block_value(kind: str, *, brand_id: int | None = None, car_model_id: int | None = None,
                city: str | None = None) -> str:
    """Нормализованное значение записи (то, что ляжет в `UserBlock.value`)."""
    if kind == KIND_BRAND:
        if brand_id is None:
            raise ValueError("brand block requires brand_id")
        return str(brand_id)
    if kind == KIND_MODEL:
        if car_model_id is None:
            raise ValueError("model block requires car_model_id")
        return str(car_model_id)
    if kind == KIND_CITY:
        value = normalize_city(city)
        if not value:
            raise ValueError("city block requires a non-empty city")
        return value
    raise ValueError(f"unknown block kind: {kind!r}")


@dataclass(slots=True)
class BlockSet:
    """Запреты одного пользователя в виде, готовом к проверке в памяти."""

    brands: set[int] = field(default_factory=set)
    models: set[int] = field(default_factory=set)
    cities: set[str] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.brands or self.models or self.cities)

    def blocks(self, listing: Listing) -> bool:
        """Запрещено ли показывать это объявление.

        Неизвестные поля объявления (NULL марка/модель/город) под запрет НЕ
        попадают: запрет должен быть доказан, а не предположен — иначе один
        заблокированный город прятал бы все объявления без города.
        """
        if listing.brand_id is not None and listing.brand_id in self.brands:
            return True
        if listing.car_model_id is not None and listing.car_model_id in self.models:
            return True
        if self.cities and normalize_city(listing.city) in self.cities:
            return True
        return False


def load_block_sets(session: Session, user_ids: set[int]) -> dict[int, BlockSet]:
    """Запреты нескольких пользователей одним запросом (для цикла матчинга).

    Пользователи без запретов в результат не попадают — вызывающий берёт их
    через `.get(user_id)` и получает None, то есть «проверять нечего».
    """
    if not user_ids:
        return {}
    result: dict[int, BlockSet] = {}
    rows = session.execute(
        select(UserBlock.user_id, UserBlock.kind, UserBlock.value).where(
            UserBlock.user_id.in_(user_ids)
        )
    )
    for user_id, kind, value in rows:
        block_set = result.setdefault(user_id, BlockSet())
        if kind == KIND_BRAND:
            block_set.brands.add(int(value))
        elif kind == KIND_MODEL:
            block_set.models.add(int(value))
        elif kind == KIND_CITY:
            block_set.cities.add(value)
    return result
