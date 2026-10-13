import asyncio

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.bot.states import AddFilter
from app.bot.wizard import WizardExitMiddleware


def _message(text: str | None) -> Message:
    return Message.model_construct(text=text)


async def _run(text: str | None, in_wizard: bool) -> tuple[FSMContext, bool, str | None]:
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    if in_wizard:
        await state.set_state(AddFilter.years)
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
    assert current == AddFilter.years.state
    assert called


def test_command_outside_wizard_is_untouched() -> None:
    state, called, current = asyncio.run(_run("/admin", in_wizard=False))
    assert current is None
    assert called
