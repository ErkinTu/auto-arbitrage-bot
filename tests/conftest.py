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


@pytest.fixture(autouse=True)
def _no_photo_download(monkeypatch):
    """Тесты не ходят в сеть за фото: «скачать самим» по умолчанию не удаётся
    (ступень пропускается), отдельные тесты подменяют `fetch_photo`."""
    from app.bot import photo_upload

    async def _none(url):
        return None

    monkeypatch.setattr(photo_upload, "fetch_photo", _none)
    yield
