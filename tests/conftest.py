"""Общие фикстуры тестов."""

import pytest

from app.bot import handlers
from app.core.cache import response_cache
from app.core.ratelimit import limiter


@pytest.fixture(autouse=True)
def _reset_click_limits():
    """Ограничители нажатий (план 01.11.2026, п. К) живут в памяти процесса:
    без сброса кулдаун витрины или «фото уже выше» из одного теста отбивал бы
    нажатия в следующем."""
    handlers.reset_click_limits()
    yield
    handlers.reset_click_limits()


@pytest.fixture(autouse=True)
def _reset_api_limits():
    """Лимитер запросов и кэш ответов API — тоже память процесса: без сброса
    кэш отдал бы ответ по базе из прошлого теста, а лимитер копил бы попытки."""
    limiter.reset()
    response_cache.clear()
    yield
    limiter.reset()
    response_cache.clear()
