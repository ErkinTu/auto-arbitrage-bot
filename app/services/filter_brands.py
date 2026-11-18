"""Несколько марок в одном фильтре бота (решение владельца 10.11.2026).

Хранение — `user_filters.brand_ids`: id марок через запятую по возрастанию
(«3,8,12»), как `fuel_groups`. Одна марка — по-прежнему `brand_id` (тогда к
ней можно выбрать модель); `brand_ids` заполняется только при двух и более.
Так старый код и старые фильтры с одной маркой не меняются вовсе.

Группа «Японцы» (`brand_group`) — legacy: новые фильтры её не пишут (кнопка
группы в панели и пресет отмечают её марки в `brand_ids`), а старые строки с
группой продолжают матчиться как раньше и превращаются в `brand_ids` при
первой правке.

Потолок — `MAX_FILTER_BRANDS`: сообщение панели и подпись фильтра должны
оставаться читаемыми, а «20 марок» — это уже «все марки».
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

MAX_FILTER_BRANDS = 10


def pack_brand_ids(ids: Iterable[int] | None) -> str | None:
    """Выбор марок → строка для `brand_ids`. Меньше двух — None (это `brand_id`)."""
    unique = sorted({int(i) for i in (ids or ()) if int(i) > 0})
    return ",".join(map(str, unique)) if len(unique) >= 2 else None


def unpack_brand_ids(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(sorted({int(part) for part in value.split(",") if part.strip().isdigit()}))


def filter_brand_set(user_filter, brand_groups: Mapping[str, frozenset[int]] | None = None) -> frozenset[int] | None:
    """Какие марки пропускает фильтр; None — любые.

    Порядок полей: несколько марок → одна марка → legacy-группа. Группа без
    карты групп — пустое множество («не доказано — не шлём», как раньше).
    """
    many = unpack_brand_ids(getattr(user_filter, "brand_ids", None))
    if many:
        return frozenset(many)
    if user_filter.brand_id is not None:
        return frozenset({user_filter.brand_id})
    if user_filter.brand_group is not None:
        return (brand_groups or {}).get(user_filter.brand_group, frozenset())
    return None


def brands_label(names: Iterable[str], *, limit: int = 3) -> str:
    """«Toyota, Lexus, Honda +2» — подпись фильтра с несколькими марками."""
    names = list(names)
    head = ", ".join(names[:limit])
    return f"{head} +{len(names) - limit}" if len(names) > limit else head
