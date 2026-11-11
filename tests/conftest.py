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


@pytest.fixture()
def lenient_best(monkeypatch):
    """Мягкий отбор «лучших» для тестов, которые проверяют не скоринг, а фильтры
    витрины (повторы, заглушки, свежесть номера, возраст): без минимума фото и базы,
    любой год, один широкий ярус. Сам скоринг — tests/test_best_picks.py."""
    from app.services import best_picks

    monkeypatch.setattr(
        best_picks,
        "current_config",
        lambda: best_picks.BestConfig(min_photos=1, min_year=1950, old_min_score=0.0, new_slots=5, max_per_model=99),
    )
    monkeypatch.setattr(best_picks, "TIERS", (best_picks.Tier(24, 0.0, 100.0, 1),))
