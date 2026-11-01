"""Общие фикстуры тестов."""

import pytest

from app.bot import handlers


@pytest.fixture(autouse=True)
def _reset_click_limits():
    """Ограничители нажатий (план 01.11.2026, п. К) живут в памяти процесса:
    без сброса кулдаун витрины или «фото уже выше» из одного теста отбивал бы
    нажатия в следующем."""
    handlers.reset_click_limits()
    yield
    handlers.reset_click_limits()
