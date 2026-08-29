"""Темп цикла бота: пауза — это ОСТАТОК интервала, а не полный интервал.

Регрессия на замеренный баг: `sleep(interval)` в конце итерации давал период
`interval + длительность цикла`. На проде это превращало «каждые 10 минут» в
14.5 (сбор ~4 мин, из них ~2/3 — lalafo), то есть уведомления опаздывали на
полтора цикла в час.
"""

from __future__ import annotations

import asyncio

import pytest

from app.bot import poller


class _Stop(BaseException):
    """Прерывает бесконечный `polling_loop`.

    Именно BaseException: вокруг цикла в `polling_loop` стоит `except Exception`,
    и обычное исключение он бы проглотил — тест висел бы вечно.
    """


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _run_one_cycle(monkeypatch, *, cycle_seconds: float) -> list[float]:
    """Прогоняет ровно одну итерацию и возвращает запрошенные паузы."""
    clock = _FakeClock()
    slept: list[float] = []

    monkeypatch.setattr(poller, "time", clock)
    monkeypatch.setattr(poller, "seconds_until_window", lambda *a, **kw: 0)
    monkeypatch.setattr(poller, "local_now", lambda *a, **kw: None)

    async def fake_sources(*args, **kwargs) -> None:
        clock.now += cycle_seconds  # обход источников «занял» столько времени

    monkeypatch.setattr(poller, "_run_sources", fake_sources)
    monkeypatch.setattr(poller, "collect_matches", list)

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise _Stop

    monkeypatch.setattr(poller.asyncio, "sleep", fake_sleep)

    with pytest.raises(_Stop):
        asyncio.run(poller.polling_loop(object()))
    return slept


def test_sleep_is_interval_minus_elapsed(monkeypatch) -> None:
    """Цикл длиной 4 мин при интервале 10 → спим 6, а не 10."""
    interval = poller.get_settings().bot_fresh_interval_minutes * 60
    slept = _run_one_cycle(monkeypatch, cycle_seconds=240)

    assert slept == [pytest.approx(interval - 240)]


def test_overrun_falls_back_to_min_pause(monkeypatch) -> None:
    """Цикл дольше интервала не даёт нулевую паузу: остаётся пол MIN_PAUSE_SECONDS.

    Овер случается на таймаутах прокси lalafo — гнать следующий цикл вплотную
    именно тогда, когда источник не отвечает, нельзя.
    """
    interval = poller.get_settings().bot_fresh_interval_minutes * 60
    slept = _run_one_cycle(monkeypatch, cycle_seconds=interval + 300)

    assert slept == [poller.MIN_PAUSE_SECONDS]
