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


async def _run_with_data(text: str) -> dict:
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(AddFilter.years)
    data = {"state": state, "raw_state": AddFilter.years.state}
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


class _FakeBot:
    def __init__(self) -> None:
        self.edited: list[tuple[int, str | None]] = []

    async def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edited.append((message_id, text))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kwargs):
        self.edited.append((message_id, None))


class _FakeMessage:
    """Ровно то, чем пользуются текстовые шаги визарда."""

    def __init__(self, text: str, bot: _FakeBot) -> None:
        from types import SimpleNamespace

        self.text = text
        self.bot = bot
        self.chat = SimpleNamespace(id=1)
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        from types import SimpleNamespace

        self.answers.append((text, reply_markup))
        return SimpleNamespace(message_id=100 + len(self.answers))


async def _mileage_step(text: str):
    from app.bot.handlers import wizard_mileage

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(AddFilter.mileage)
    await state.update_data(prompt_id=7)  # вопрос «Шаг 4/7» с кнопкой «Пропустить»
    bot = _FakeBot()
    message = _FakeMessage(text, bot)
    await wizard_mileage(message, state)
    return bot, message, await state.get_data(), await state.get_state()


def test_answered_text_step_turns_prompt_into_summary_without_buttons() -> None:
    bot, message, data, current = asyncio.run(_mileage_step("150000"))
    assert bot.edited == [(7, "✅ Пробег: до 150 000 км")]  # старый вопрос — итог, кнопок нет
    assert message.answers[0][0].startswith("Шаг 5/7")
    assert data["prompt_id"] == 101  # текущим стал новый вопрос
    assert current == AddFilter.price.state


def test_invalid_input_strips_old_buttons_and_reasks() -> None:
    bot, message, data, current = asyncio.run(_mileage_step("много"))
    assert bot.edited == [(7, None)]  # у старого вопроса просто сняты кнопки
    assert message.answers[0][1] is not None  # у подсказки — снова «Пропустить»
    assert data["prompt_id"] == 101
    assert current == AddFilter.mileage.state
