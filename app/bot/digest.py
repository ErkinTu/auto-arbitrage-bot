"""Дайджест: общие правила списка и кнопки «Показать с фото».

Отдельный модуль, потому что правила нужны с двух сторон: `notifier` собирает
список, `db` по кнопке читает его позиции из `sent_notifications`. Порядок и
границы обязаны совпадать — иначе кнопка покажет не те машины, что в списке.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

_T = TypeVar("_T")

# Потолок позиций в одном сообщении-списке. Лимит Telegram — 4096 символов
# ВИДИМОГО текста (адреса ссылок не считаются), позиция — две строки ~90
# символов плюс заголовки марок. Сверх — следующим сообщением.
DIGEST_MAX_ITEMS = 25
# Прежний список по марке (откат `BOT_RICH_CARDS_PER_CYCLE=0`) — 10 на сообщение.
LEGACY_DIGEST_MAX_ITEMS = 10
# Раздел для объявлений без распознанной марки — последним.
OTHER_BRANDS_LABEL = "Другие марки"
# Сколько карточек с фото отдаёт одно нажатие кнопки.
DIGEST_PAGE = 3
# Срок жизни кнопки: дальше объявления в основном сняты, а «свежее» — в витрине.
DIGEST_TTL_MINUTES = 24 * 60


def batch_id_for(moment: datetime) -> int:
    """Номер пачки = unix-минуты цикла: влезает в INTEGER и сам несёт время."""
    return int(moment.timestamp() // 60)


def batch_age_minutes(batch_id: int, now: datetime) -> int:
    return batch_id_for(now) - batch_id


def digest_sort_key(below_market_percent: float | None, listing_id: int) -> tuple[float, int]:
    """Порядок позиций в дайджесте — и в списке, и в выдаче по кнопке."""
    percent = float(below_market_percent) if below_market_percent is not None else float("-inf")
    return (-percent, listing_id)


def order_digest(
    items: list[_T], key: Callable[[_T], tuple[str | None, float | None, int]]
) -> list[_T]:
    """Позиции дайджеста по порядку: разделы по маркам, внутри — по выгоде.

    `key(item)` → (марка, % ниже рынка, id объявления). Раздел марки идёт тем
    раньше, чем выгоднее её лучшая машина; без марки — в конце. Один порядок
    для списка (`notifier`) и для выдачи по кнопке (`db.digest_page`): кнопка
    обязана идти ровно по тем позициям, что человек видит в списке.
    """
    best: dict[str | None, float] = {}
    for item in items:
        brand, percent, _ = key(item)
        value = float(percent) if percent is not None else float("-inf")
        best[brand] = max(best.get(brand, float("-inf")), value)

    def sort_key(item: _T) -> tuple:
        brand, percent, listing_id = key(item)
        return (brand is None, -best[brand], brand or "", *digest_sort_key(percent, listing_id))

    return sorted(items, key=sort_key)
