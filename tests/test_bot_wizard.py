import asyncio

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.bot.states import FilterPanel
from app.bot.wizard import WizardExitMiddleware


def _message(text: str | None) -> Message:
    return Message.model_construct(text=text)


async def _run(text: str | None, in_wizard: bool) -> tuple[FSMContext, bool, str | None]:
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    if in_wizard:
        await state.set_state(FilterPanel.active)
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    await WizardExitMiddleware()(handler, _message(text), {"state": state})
    return state, called, await state.get_state()


@pytest.mark.parametrize("text", ["/admin", "/filters", "/cancel", "/unknown@bot arg"])
def test_command_clears_wizard_and_passes_through(text) -> None:
    state, called, current = asyncio.run(_run(text, in_wizard=True))
    assert current is None
    assert called


@pytest.mark.parametrize("text", ["2015-2020", "150000", None])
def test_plain_input_keeps_wizard(text) -> None:
    state, called, current = asyncio.run(_run(text, in_wizard=True))
    assert current == FilterPanel.active.state
    assert called


def test_command_outside_wizard_is_untouched() -> None:
    state, called, current = asyncio.run(_run("/admin", in_wizard=False))
    assert current is None
    assert called


async def _run_with_data(text: str) -> dict:
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(FilterPanel.active)
    data = {"state": state, "raw_state": FilterPanel.active.state}
    seen = {}

    async def handler(event, handler_data):
        seen.update(handler_data)

    await WizardExitMiddleware()(handler, _message(text), data)
    return seen


def test_exit_also_resets_raw_state_seen_by_state_filters() -> None:
    """Фильтры состояний aiogram читают `raw_state` из data, а не из хранилища:
    без сброса `/admin` внутри визарда ловил фолбэк шага «годы»."""
    seen = asyncio.run(_run_with_data("/admin"))
    assert seen["raw_state"] is None


@pytest.mark.parametrize("text", ["📋 Мои фильтры", "➕ Фильтр", "💎 Подписка", "❓ Помощь"])
def test_menu_button_exits_wizard_like_a_command(text) -> None:
    state, called, current = asyncio.run(_run(text, in_wizard=True))
    assert current is None
    assert called


def test_menu_buttons_for_subscribe_and_help_need_no_access() -> None:
    from app.bot.access import AccessMiddleware

    assert AccessMiddleware._is_entry(_message("💎 Подписка"))
    assert AccessMiddleware._is_entry(_message("❓ Помощь"))
    assert not AccessMiddleware._is_entry(_message("➕ Фильтр"))
