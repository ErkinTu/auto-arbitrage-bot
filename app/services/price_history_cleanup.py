"""Очистка ложных «смен цены» из `listing_price_history` (разбор 16.11.2026).

До 18.11.2026 полный обход mashina записывал «смену цены» почти каждому
объявлению каждую субботу: менялись только сомы, пересчитанные по курсу
(`sync.is_real_price_change`). Эти строки рисуют ступеньки в «Истории цены»
на сайте и дают «↓ $1 цену снизили» (`public_feed._price_drops`).

Проход по истории каждого объявления в порядке записи: строка `price_changed`,
которая по новому правилу не смена относительно последней оставленной строки,
— ложная. `initial` не трогаем никогда. События в `listing_status_history`
не трогаем: без `details.direction` бот и предпросмотр их уже не видят.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import ListingPriceHistory as H
from app.services.sync import is_real_price_change


@dataclass
class CleanupReport:
    listings: int = 0
    rows: int = 0
    false_changes: int = 0
    deleted: int = 0


def find_false_changes(session: Session) -> tuple[list[int], CleanupReport]:
    """id ложных строк `price_changed` и сводка (только чтение)."""
    report = CleanupReport()
    false_ids: list[int] = []
    rows = session.execute(
        select(H.id, H.listing_id, H.event_type, H.price_usd, H.price_kgs, H.price_amount)
        .order_by(H.listing_id, H.observed_at, H.id)
        .execution_options(yield_per=5000)
    )
    current_listing = None
    kept = None
    for row in rows:
        report.rows += 1
        if row.listing_id != current_listing:
            current_listing, kept = row.listing_id, row
            report.listings += 1
            continue
        if row.event_type == "price_changed" and not is_real_price_change(
            SimpleNamespace(current_price_usd=kept.price_usd, current_price_kgs=kept.price_kgs,
                            current_price_amount=kept.price_amount),
            SimpleNamespace(price_usd=row.price_usd, price_kgs=row.price_kgs, price_amount=row.price_amount),
        ):
            false_ids.append(row.id)
            continue
        kept = row
    report.false_changes = len(false_ids)
    return false_ids, report


def delete_rows(session: Session, ids: list[int], *, batch: int = 5000) -> int:
    """Удаляет пачками с коммитом после каждой: обход не держит долгую блокировку."""
    deleted = 0
    for start in range(0, len(ids), batch):
        chunk = ids[start : start + batch]
        session.execute(delete(H).where(H.id.in_(chunk)))
        session.commit()
        deleted += len(chunk)
    return deleted
