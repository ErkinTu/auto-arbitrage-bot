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
    """`collect_matches` не принимает результат обхода — только окно из базы
    (и расход потолка прошлыми волнами цикла).

    Это и есть причина, по которой отставший источник ничего не теряет.
    """
    import inspect

    assert list(inspect.signature(poller.collect_matches).parameters) == ["cycle_counts"]
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


# --- волны рассылки (план 31.10.2026) -----------------------------------------
# Рассылка — сразу по завершении каждого источника, а не после самого медленного.


def test_wave_per_source_in_completion_order(monkeypatch) -> None:
    """mashina доехала — её волна уходит, пока lalafo ещё обходит ленту."""
    mashina_wave_sent = threading.Event()
    waves: list[list[str]] = []

    def fake_sync(source_name: str) -> None:
        if source_name == LALAFO:
            # lalafo заканчивает только ПОСЛЕ волны mashina: доказывает, что
            # волна не ждёт самого медленного источника.
            assert mashina_wave_sent.wait(timeout=5)

    async def on_progress(names: list[str]) -> None:
        waves.append(names)
        if names == [MASHINA]:
            mashina_wave_sent.set()

    monkeypatch.setattr(poller, "sync_source", fake_sync)
    inflight: dict[str, asyncio.Task] = {}
    needs_final = asyncio.run(
        poller._run_sources(inflight, deadline_seconds=5, on_progress=on_progress)
    )

    assert waves == [[MASHINA], [LALAFO]]
    assert needs_final is False  # всё доехало, обе волны прошли


def test_deadline_asks_for_a_final_wave(monkeypatch) -> None:
    """Застрявший lalafo: волна mashina прошла, по дедлайну нужна финальная."""
    release = threading.Event()
    waves: list[list[str]] = []

    def fake_sync(source_name: str) -> None:
        if source_name == LALAFO:
            release.wait(timeout=5)

    async def on_progress(names: list[str]) -> None:
        waves.append(names)

    monkeypatch.setattr(poller, "sync_source", fake_sync)
    inflight: dict[str, asyncio.Task] = {}
    try:
        needs_final = asyncio.run(
            poller._run_sources(inflight, deadline_seconds=0.2, on_progress=on_progress)
        )
    finally:
        release.set()

    assert waves == [[MASHINA]]
    assert needs_final is True


def test_source_left_over_from_previous_cycle_gives_no_wave(monkeypatch) -> None:
    """Обход, начатый прошлым циклом, этот цикл не ждёт и волну за него не шлёт."""
    release = threading.Event()
    waves: list[list[str]] = []

    def fake_sync(source_name: str) -> None:
        if source_name == LALAFO:
            release.wait(timeout=5)

    async def on_progress(names: list[str]) -> None:
        waves.append(names)

    monkeypatch.setattr(poller, "sync_source", fake_sync)
    inflight: dict[str, asyncio.Task] = {}
    results: list[bool] = []

    async def two_cycles() -> None:
        results.append(await poller._run_sources(inflight, deadline_seconds=0.1))
        results.append(
            await poller._run_sources(inflight, deadline_seconds=5, on_progress=on_progress)
        )

    try:
        asyncio.run(two_cycles())
    finally:
        release.set()

    assert waves == [[MASHINA]]  # второй цикл: только свежий обход mashina
    assert results == [True, False]


def test_failed_wave_does_not_break_the_cycle(monkeypatch, caplog) -> None:
    """Упала подборка/рассылка одной волны — следующая волна всё равно идёт."""
    calls: list[int] = []

    def fake_collect(cycle_counts):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db hiccup")
        return []

    monkeypatch.setattr(poller, "collect_matches", fake_collect)
    batches = poller._BatchClock()

    async def two_waves() -> None:
        await poller._send_wave(object(), [MASHINA], cycle_counts={}, batches=batches)
        await poller._send_wave(object(), [LALAFO], cycle_counts={}, batches=batches)

    with caplog.at_level("ERROR"):
        asyncio.run(two_waves())

    assert len(calls) == 2
    assert "db hiccup" in caplog.text


def test_batch_ids_of_waves_never_collide(monkeypatch) -> None:
    """Две волны в одну минуту получают разные номера пачки дайджеста.

    Кнопка «Показать с фото» читает строки по (юзер, batch_id): с общим
    номером вторая волна подмешалась бы в список первой.
    """
    from datetime import UTC, datetime

    moment = datetime(2026, 10, 31, 12, 0, 30, tzinfo=UTC)
    monkeypatch.setattr(poller, "utcnow", lambda: moment)
    clock = poller._BatchClock()

    first, second, third = clock.next(), clock.next(), clock.next()
    assert first == poller.batch_id_for(moment)
    assert second == first + 1
    assert third == first + 2
