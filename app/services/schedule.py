"""Чистые функции расписания для worker-а и telegram-бота.

Все расчеты в локальном времени (settings.scheduler_timezone), чтобы
«2 часа ночи» и «8:00–24:00» означали время Бишкека, а не UTC контейнера.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def window_end_utc(now: datetime, end_hour: int, tz_name: str) -> datetime:
    """Ближайшая граница конца активного окна бота — в UTC.

    Нужна отстающим тарифам: перед ночной паузой выдержку приходится сбрасывать
    (объявление, дозревающее в 00:10, пришлось бы отдать утром — а за ночь его
    успевают снять, и уведомление на мёртвую страницу читается как сломанный
    бот). Значит матчингу нужно знать, когда сегодняшние циклы кончатся.

    `end_hour=24` = местная полночь (то есть 00:00 следующего дня). Считаем в
    локальной зоне и переводим в UTC: окно задано бишкекскими часами, а `now`
    и события в БД — в UTC.
    """
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    boundary = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=end_hour)
    if boundary <= local:
        boundary += timedelta(days=1)
    return boundary.astimezone(timezone.utc)


def last_slot_start(now: datetime, start_hour: int, interval_hours: int) -> datetime:
    """Начало последнего слота сетки start_hour + k*interval_hours, не позже now.

    Например, start=10, interval=8 дает слоты 10:00, 18:00, 02:00.
    """
    anchor = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if anchor > now:
        anchor -= timedelta(days=1)
    slots_passed = int((now - anchor).total_seconds() // 3600) // interval_hours
    return anchor + timedelta(hours=slots_passed * interval_hours)
