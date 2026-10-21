"""Меню команд: публикация и синхронность со списком хендлеров.

Смысл тестов — не «проверить aiogram», а поймать расхождение: команду добавили
в хендлеры и забыли в меню (или наоборот, оставили в меню удалённую).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app.bot.commands import BOT_COMMANDS, setup_bot_commands

HANDLERS_SOURCE = Path(__file__).resolve().parents[1] / "app" / "bot" / "handlers.py"

# Команды, которые в меню не показываем осознанно (см. docstring commands.py).
# `admin` — привилегия: в автодополнении у всех она раскрывала бы сам факт
# существования панели (`docs/admin/ADMIN_PANEL_PLAN.md`).
INTENTIONALLY_HIDDEN = {"subscribe", "admin"}


def _registered_commands() -> set[str]:
    """Что реально висит на @router.message(Command("...")) в хендлерах."""
    source = HANDLERS_SOURCE.read_text(encoding="utf-8")
    return set(re.findall(r'Command\("([a-z_]+)"\)', source))


class _FakeBot:
    """Минимальный дублёр: нас интересует только вызов set_my_commands."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[list] = []

    async def set_my_commands(self, commands):
        self.calls.append(commands)
        if self.error is not None:
            raise self.error


# --- синхронность с хендлерами ---------------------------------------------


def test_every_menu_command_has_a_handler():
    registered = _registered_commands()
    missing = {name for name, _ in BOT_COMMANDS} - registered
    assert not missing, f"в меню есть команды без хендлера: {sorted(missing)}"


def test_every_handler_command_is_in_menu_or_hidden_on_purpose():
    in_menu = {name for name, _ in BOT_COMMANDS}
    forgotten = _registered_commands() - in_menu - INTENTIONALLY_HIDDEN
    assert not forgotten, (
        f"команда есть в боте, но не в меню: {sorted(forgotten)}. "
        "Добавьте её в BOT_COMMANDS или в INTENTIONALLY_HIDDEN с объяснением."
    )


def test_subscribe_stays_out_of_the_menu():
    """Оплата обкатывается: из постоянного меню в неё звать нельзя."""
    assert "subscribe" not in {name for name, _ in BOT_COMMANDS}


# --- формат, который требует Telegram ---------------------------------------


def test_commands_match_telegram_constraints():
    for name, description in BOT_COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), name
        assert 1 <= len(description) <= 256, name
        # Ведущий «/» Telegram добавляет сам; свой сделал бы «//add_filter».
        assert not name.startswith("/"), name


def test_menu_has_no_duplicates():
    names = [name for name, _ in BOT_COMMANDS]
    assert len(names) == len(set(names))


# --- публикация -------------------------------------------------------------


def test_setup_publishes_all_commands_in_order():
    bot = _FakeBot()
    asyncio.run(setup_bot_commands(bot))

    assert len(bot.calls) == 1
    published = [(item.command, item.description) for item in bot.calls[0]]
    assert published == list(BOT_COMMANDS)


def test_telegram_failure_does_not_break_startup():
    """Меню — украшение; недоступный Telegram не должен ронять запуск бота."""
    bot = _FakeBot(error=RuntimeError("telegram is down"))
    asyncio.run(setup_bot_commands(bot))  # не должно бросить
    assert len(bot.calls) == 1
