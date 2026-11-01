"""Разметка `listings.fuel_group` (план 01.11.2026, п. Е): mashina и бэкфилл lalafo.

**lalafo** — группа выводится из сырого `fuel_type` нормализатором при каждой
встрече; старым строкам — `backfill_lalafo_fuel_groups` (по различным
значениям, ~25 UPDATE).

**mashina:**

В ленте API mashina топлива нет, но API фильтрует по нему. Поэтому группу
ставим «от списка»: берём страницы объявлений одного типа топлива и
проставляем `listings.fuel_group` тем, чей id в них попал.

Два режима:
- **свежий** (`max_pages=1`, бот после каждого обхода mashina, ДО волны
  рассылки): по 1 странице (100 новейших по `created_at`) на тип — 5 запросов.
  За 10 минут столько объявлений одного топлива не появляется, так что новое
  объявление получает топливо в том же цикле, что и попало в базу;
- **полный** (`max_pages=None`, worker раз в сутки ночью и `cli.py
  fuel-backfill`): все страницы всех типов, ~550 запросов — бэкфилл старой базы
  и всё, что свежий режим упустил.

Не найденное ни в одном списке остаётся `NULL` — под фильтр по топливу не
попадает (не доказано — не шлём). Ошибка запроса прерывает только свой тип:
остальные размечаются, а упущенное доберёт следующий прогон.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Listing
from app.services.fuel import MASHINA_FUEL_VALUES, fuel_group_from_lalafo

logger = logging.getLogger(__name__)

PAGE_SIZE = 100  # максимум API mashina (200 → 422)
# Предохранитель полного режима: на 01.11.2026 у самого частого типа (бензин)
# ~344 страницы. Если API начнёт игнорировать фильтр и отдавать всю ленту —
# не крутимся бесконечно.
MAX_PAGES_PER_FUEL = 1000


@dataclass(slots=True)
class FuelLabelReport:
    requests: int = 0
    seen: int = 0  # id в ответах
    updated: int = 0  # строк, у которых группа поменялась
    failed: list[str] | None = None  # типы, прерванные ошибкой


def label_mashina_fuel(
    session: Session,
    parser,
    source_id: int,
    *,
    max_pages: int | None,
) -> FuelLabelReport:
    """Проставляет `fuel_group` объявлениям mashina по спискам API.

    `parser` — `MashinaParser` (нужен `fetch_fuel_page`). Коммит после каждой
    страницы: транзакция не переживает HTTP-запрос (иначе держит строки
    `listings`, и DDL при выкатке встаёт за ней — 21.10.2026).
    """
    report = FuelLabelReport(failed=[])
    limit = MAX_PAGES_PER_FUEL if max_pages is None else max_pages
    for fuel_value, group in MASHINA_FUEL_VALUES.items():
        for page in range(1, limit + 1):
            try:
                result = parser.fetch_fuel_page(fuel_value, page=page, size=PAGE_SIZE)
            except Exception:
                logger.exception("mashina fuel labeling: %s page %s failed", fuel_value, page)
                report.failed.append(fuel_value)
                break
            report.requests += 1
            ids = [str(item["id"]) for item in result.items if item.get("id") is not None]
            report.seen += len(ids)
            if ids:
                changed = session.execute(
                    update(Listing)
                    .where(
                        Listing.source_id == source_id,
                        Listing.external_id.in_(ids),
                        Listing.fuel_group.is_distinct_from(group),
                    )
                    .values(fuel_group=group)
                    .execution_options(synchronize_session=False)
                )
                report.updated += changed.rowcount or 0
                session.commit()
            if len(result.items) < PAGE_SIZE:
                break
    return report


def backfill_lalafo_fuel_groups(session: Session, source_id: int) -> dict[str | None, int]:
    """`fuel_group` из `fuel_type` у всех объявлений lalafo; группа → строк обновлено.

    По различным значениям, а не по строкам: их ~25 на ~120 тыс. объявлений.
    Идемпотентна — повтор ничего не меняет. Значения без группы (мусорные
    сочетания) получают NULL.
    """
    values = session.scalars(
        select(Listing.fuel_type).where(Listing.source_id == source_id).distinct()
    ).all()
    counts: dict[str | None, int] = {}
    for value in values:
        group = fuel_group_from_lalafo(value)
        condition = Listing.fuel_type.is_(None) if value is None else Listing.fuel_type == value
        changed = session.execute(
            update(Listing)
            .where(
                Listing.source_id == source_id,
                condition,
                Listing.fuel_group.is_distinct_from(group),
            )
            .values(fuel_group=group)
            .execution_options(synchronize_session=False)
        )
        counts[group] = counts.get(group, 0) + (changed.rowcount or 0)
        session.commit()
    return counts
