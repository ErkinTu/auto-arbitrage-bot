"""Разметка привода `listings.drive_type` у mashina (10.11.2026).

В ленте API mashina привода нет (атрибуты: год, пробег, двигатель, КПП,
руль, кузов, город), но API по нему фильтрует: `drive_type: {"in": "полный"}`.
Проверено 10.11.2026: «полный» 20 336 + «передний» 28 606 + «задний» 5 551 =
54 493 из 54 506 активных. Поэтому — как с топливом
(`services/fuel_labeling.py`): берём списки объявлений одного привода и
проставляем его тем, чей id в них попал.

Значения — с заглавной, как у lalafo («Передний», «Задний»); полный у lalafo
бывает «4WD, полный» / «AWD, полный» — у mashina просто «Полный» (группа
«полный» на сайте ловит все три по подстроке).

Синк mashina пустым приводом из ленты размеченное не затирает
(`sync._set_current_fields`). Режимы те же, что у топлива: свежий (по 1
странице на значение, бот после обхода mashina) и полный (worker ночью,
`cli.py drive-backfill`, ~550 запросов).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Listing

logger = logging.getLogger(__name__)

PAGE_SIZE = 100  # максимум API mashina
MAX_PAGES_PER_VALUE = 1000  # предохранитель: API перестал фильтровать — не крутимся вечно

# Значение фильтра API mashina → что пишем в listings.drive_type.
MASHINA_DRIVE_VALUES: dict[str, str] = {
    "полный": "Полный",
    "передний": "Передний",
    "задний": "Задний",
}


@dataclass(slots=True)
class DriveLabelReport:
    requests: int = 0
    seen: int = 0
    updated: int = 0
    failed: list[str] = field(default_factory=list)


def label_mashina_drive(session: Session, parser, source_id: int, *, max_pages: int | None) -> DriveLabelReport:
    """Проставляет привод объявлениям mashina по спискам API.

    Коммит после каждой страницы: транзакция не переживает HTTP-запрос (иначе
    держит строки `listings`, и DDL при выкатке встаёт за ней — 21.10.2026).
    Ошибка запроса прерывает только своё значение.
    """
    report = DriveLabelReport()
    limit = MAX_PAGES_PER_VALUE if max_pages is None else max_pages
    for api_value, stored in MASHINA_DRIVE_VALUES.items():
        for page in range(1, limit + 1):
            try:
                result = parser.fetch_attribute_page("drive_type", api_value, page=page, size=PAGE_SIZE)
            except Exception:
                logger.exception("mashina drive labeling: %s page %s failed", api_value, page)
                report.failed.append(api_value)
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
                        Listing.drive_type.is_distinct_from(stored),
                    )
                    .values(drive_type=stored)
                    .execution_options(synchronize_session=False)
                )
                report.updated += changed.rowcount or 0
                session.commit()
            if len(result.items) < PAGE_SIZE:
                break
    return report
