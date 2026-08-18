from datetime import datetime, timezone

from app.services.schedule import (
    last_slot_start,
    seconds_until_window,
    split_brands_for_weekend,
    window_end_utc,
)


def _dt(hour: int, minute: int = 0, day: int = 11) -> datetime:
    return datetime(2026, 7, day, hour, minute)


def test_split_brands_for_weekend() -> None:
    saturday, sunday = split_brands_for_weekend(["Toyota", "BMW", "Audi", "Kia", "Honda"])
    assert saturday == ["Audi", "BMW", "Honda"]
    assert sunday == ["Kia", "Toyota"]
    # Порядок входа не влияет на разбиение.
    assert split_brands_for_weekend(["Kia", "Honda", "Audi", "BMW", "Toyota"]) == (saturday, sunday)
    assert split_brands_for_weekend(["BMW"]) == (["BMW"], [])
    assert split_brands_for_weekend([]) == ([], [])


def test_seconds_until_window() -> None:
    # Внутри окна 8-24.
    assert seconds_until_window(_dt(8, 0), 8, 24) == 0.0
    assert seconds_until_window(_dt(23, 59), 8, 24) == 0.0
    # Ночью ждем до 8 утра.
    assert seconds_until_window(_dt(0, 0), 8, 24) == 8 * 3600
    assert seconds_until_window(_dt(7, 30), 8, 24) == 30 * 60
    # После конца окна (end < 24) ждем до старта завтра.
    assert seconds_until_window(_dt(22, 0), 8, 22) == 10 * 3600


def test_last_slot_start() -> None:
    # Сетка 10:00 + 8ч: слоты 10:00, 18:00, 02:00.
    assert last_slot_start(_dt(10, 0), 10, 8) == _dt(10, 0)
    assert last_slot_start(_dt(14, 30), 10, 8) == _dt(10, 0)
    assert last_slot_start(_dt(18, 0), 10, 8) == _dt(18, 0)
    assert last_slot_start(_dt(23, 59), 10, 8) == _dt(18, 0)
    # После полуночи последний слот - 02:00 текущего дня либо 18:00 вчера.
    assert last_slot_start(_dt(1, 0, day=12), 10, 8) == _dt(18, 0, day=11)
    assert last_slot_start(_dt(2, 0, day=12), 10, 8) == _dt(2, 0, day=12)
    assert last_slot_start(_dt(9, 59, day=12), 10, 8) == _dt(2, 0, day=12)


def test_window_end_utc_midnight() -> None:
    """end_hour=24 = местная полночь. Нужно отстающим тарифам: до неё они ждут,
    после — уже некому забирать, поэтому выдержка сбрасывается."""
    # 10:00 UTC = 16:00 в Бишкеке (UTC+6) -> ближайшая граница 00:00 20.08 местных.
    now = datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc)
    assert window_end_utc(now, 24, "Asia/Bishkek") == datetime(
        2026, 8, 19, 18, 0, tzinfo=timezone.utc
    )
    # 18:30 UTC = 00:30 20.08 местных: сегодняшняя полночь уже прошла.
    now = datetime(2026, 8, 19, 18, 30, tzinfo=timezone.utc)
    assert window_end_utc(now, 24, "Asia/Bishkek") == datetime(
        2026, 8, 20, 18, 0, tzinfo=timezone.utc
    )


def test_window_end_utc_non_midnight_hour() -> None:
    now = datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc)  # 16:00 местных
    assert window_end_utc(now, 23, "Asia/Bishkek") == datetime(
        2026, 8, 19, 17, 0, tzinfo=timezone.utc
    )
    # Граница уже прошла (23:30 местных) -> ждём завтрашнюю.
    now = datetime(2026, 8, 19, 17, 30, tzinfo=timezone.utc)
    assert window_end_utc(now, 23, "Asia/Bishkek") == datetime(
        2026, 8, 20, 17, 0, tzinfo=timezone.utc
    )
