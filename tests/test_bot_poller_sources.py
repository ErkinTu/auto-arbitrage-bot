"""Параллельный обход источников: изоляция, дедлайн и «один синк на источник».

Источники ничем не связаны, поэтому идут в разных потоках. Существенное
поведение, которое здесь и проверяется: **застрявший источник не держит
рассылку**, но и второй его обход поверх первого не запускается.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.bot import poller
from app.services.sources import LALAFO, MASHINA, SOURCE_NAMES


def test_sources_run_in_parallel(monkeypatch) -> None:
    """Оба источника стартуют одновременно, а не один за другим.

    Проверяем барьером: если бы обход был последовательным, обе стороны никогда
    бы не встретились и barrier.wait() упал бы по таймауту.
    """
    barrier = threading.Barrier(len(SOURCE_NAMES), timeout=5)
    visited: list[str] = []

    def fake_sync(source_name: str) -> None:
        barrier.wait()
        visited.append(source_name)

    monkeypatch.setattr(poller, "sync_source", fake_sync)
    inflight: dict[str, asyncio.Task] = {}
    asyncio.run(poller._run_sources(inflight, deadline_seconds=5))

    assert sorted(visited) == sorted(SOURCE_NAMES)
    assert all(task.done() for task in inflight.values())


def test_slow_source_does_not_hold_the_cycle(monkeypatch) -> None:
    """Дедлайн истёк — возвращаемся, не дожидаясь застрявшего источника."""
    release = threading.Event()

    def fake_sync(source_name: str) -> None:
        if source_name == LALAFO:
            release.wait(timeout=5)  # «затор прокси»

    monkeypatch.setattr(poller, "sync_source", fake_sync)
    inflight: dict[str, asyncio.Task] = {}
    state: dict[str, bool] = {}

    async def one_cycle() -> None:
        await poller._run_sources(inflight, deadline_seconds=0.05)
        # Состояние снимаем ВНУТРИ цикла: `asyncio.run` на выходе отменяет всё
        # незавершённое, а в проде петля живёт всё время работы бота.
        state["fast_done"] = inflight[MASHINA].done()
        state["slow_running"] = not inflight[LALAFO].done()

    try:
        asyncio.run(one_cycle())
    finally:
        release.set()

    assert state["fast_done"]
    assert state["slow_running"]  # застрявший остался работать в фоне


def test_stuck_source_is_not_started_twice(monkeypatch) -> None:
    """Пока прошлый обход источника жив, новый не заводится.

    Иначе на заторе lalafo к третьему циклу на неё пришлось бы три
    одновременных обхода — по уже отваливающемуся прокси.
    """
    release = threading.Event()
    starts: list[str] = []

    def fake_sync(source_name: str) -> None:
        starts.append(source_name)
        if source_name == LALAFO:
            release.wait(timeout=5)

    monkeypatch.setattr(poller, "sync_source", fake_sync)
    inflight: dict[str, asyncio.Task] = {}

    async def two_cycles() -> None:
        await poller._run_sources(inflight, deadline_seconds=0.05)
        await poller._run_sources(inflight, deadline_seconds=0.05)

    try:
        asyncio.run(two_cycles())
    finally:
        release.set()

    assert starts.count(MASHINA) == 2  # быстрый источник обошли дважды
    assert starts.count(LALAFO) == 1  # застрявший — только один раз


def test_failed_source_is_logged_not_raised(monkeypatch, caplog) -> None:
    """Падение обхода не роняет цикл, но и не теряется молча."""

    def fake_sync(source_name: str) -> None:
        if source_name == LALAFO:
            raise RuntimeError("boom")

    monkeypatch.setattr(poller, "sync_source", fake_sync)
    inflight: dict[str, asyncio.Task] = {}
    with caplog.at_level("ERROR"):
        asyncio.run(poller._run_sources(inflight, deadline_seconds=5))

    assert "boom" in caplog.text
    assert inflight[MASHINA].done()


def test_collect_matches_is_independent_of_sync(monkeypatch) -> None:
    """`collect_matches` не принимает результат обхода — только окно из базы.

    Это и есть причина, по которой отставший источник ничего не теряет.
    """
    import inspect

    assert list(inspect.signature(poller.collect_matches).parameters) == []
    assert list(inspect.signature(poller.sync_source).parameters) == ["source_name"]


@pytest.mark.parametrize("name", SOURCE_NAMES)
def test_sync_source_opens_its_own_session(monkeypatch, name: str) -> None:
    """Сессия создаётся внутри вызова: объект Session не потокобезопасен."""
    opened: list[int] = []

    class FakeSession:
        def __enter__(self):
            opened.append(1)
            raise RuntimeError("stop here")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(poller, "SessionLocal", FakeSession)
    with pytest.raises(RuntimeError):
        poller.sync_source(name)
    assert opened == [1]
