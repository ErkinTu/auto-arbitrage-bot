"""Чистые функции расписания для worker-а и telegram-бота.

Все расчеты в локальном времени (settings.scheduler_timezone), чтобы
«2 часа ночи» и «8:00–24:00» означали время Бишкека, а не UTC контейнера.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def local_now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def split_brands_for_weekend(brands: list[str]) -> tuple[list[str], list[str]]:
    """Стабильное разбиение марок: первая половина — суббота, вторая — воскресенье."""
    ordered = sorted(brands)
    middle = (len(ordered) + 1) // 2
    return ordered[:middle], ordered[middle:]


def seconds_until_window(now: datetime, start_hour: int, end_hour: int) -> float:
    """0, если now внутри окна [start_hour, end_hour); иначе секунды до следующего старта.

    end_hour=24 означает «до полуночи».
    """
    if start_hour <= now.hour < end_hour:
        return 0.0
    next_start = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if next_start <= now:
        next_start += timedelta(days=1)
    return (next_start - now).total_seconds()


def last_slot_start(now: datetime, start_hour: int, interval_hours: int) -> datetime:
    """Начало последнего слота сетки start_hour + k*interval_hours, не позже now.

    Например, start=10, interval=8 дает слоты 10:00, 18:00, 02:00.
    """
    anchor = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if anchor > now:
        anchor -= timedelta(days=1)
    slots_passed = int((now - anchor).total_seconds() // 3600) // interval_hours
    return anchor + timedelta(hours=slots_passed * interval_hours)
