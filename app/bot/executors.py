"""Пулы потоков бота (план 01.11.2026, п. К — «одна точка отказа для всех»).

Все синхронные операции бота идут в потоки. Раньше — в общий пул asyncio по
умолчанию: min(32, CPU+4) = 6 потоков на проде (2 vCPU). Обход источников
держал из них до 2 надолго (lalafo в затор — до 22 мин), а «📷 Все фото» по
старому объявлению — по потоку на запрос к источнику до таймаута. Четыре
быстрых нажатия при медленном прокси — и бот не отвечал никому: каждый хендлер
ждал свободного потока для запроса к базе.

Теперь три пула, и медленное не выедает быстрое:
- **по умолчанию** (`asyncio.to_thread`) — короткие запросы к базе,
  `BOT_DB_THREADS` потоков;
- **click** — запросы к источникам по нажатию, `BOT_CLICK_SOURCE_THREADS`;
  лишние нажатия ждут в очереди своего пула, а не пула базы;
- **background** — обход источников и прогрев витрины в цикле бота.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

from app.core.config import get_settings

P = ParamSpec("P")
T = TypeVar("T")

# Обход mashina + обход lalafo + прогрев витрины.
BACKGROUND_THREADS = 3

_click_pool: ThreadPoolExecutor | None = None
_background_pool: ThreadPoolExecutor | None = None


def install_default_executor(loop: asyncio.AbstractEventLoop) -> None:
    """Пул для `asyncio.to_thread` — задать явно при старте бота."""
    workers = max(4, get_settings().bot_db_threads)
    loop.set_default_executor(ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bot-db"))


def _click() -> ThreadPoolExecutor:
    global _click_pool
    if _click_pool is None:
        workers = max(1, get_settings().bot_click_source_threads)
        _click_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bot-click")
    return _click_pool


def _background() -> ThreadPoolExecutor:
    global _background_pool
    if _background_pool is None:
        _background_pool = ThreadPoolExecutor(
            max_workers=BACKGROUND_THREADS, thread_name_prefix="bot-bg"
        )
    return _background_pool


async def _run(pool: ThreadPoolExecutor, func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(pool, functools.partial(func, *args, **kwargs))


async def run_click(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Запрос к источнику по нажатию человека — в свой пул."""
    return await _run(_click(), func, *args, **kwargs)


async def run_background(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Долгая фоновая работа цикла бота (обход, прогрев) — в свой пул."""
    return await _run(_background(), func, *args, **kwargs)
