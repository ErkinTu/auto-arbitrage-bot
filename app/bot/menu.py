"""Постоянное меню: четыре кнопки под полем ввода.

Кнопки — основной путь для тех, кто не знает про «/»; команды остаются
запасным, внутри сценариев — инлайн-кнопки, как раньше. Всё в кнопки НЕ
переводим: настройку фильтров заберёт мини-приложение.

Текст кнопки приходит обычным сообщением без «/», поэтому в двух местах его
приравниваем к команде: доступ (`access.py` — «Подписка» и «Помощь» нужны и
без доступа) и выход из визарда (`wizard.py` — нажатие «Мои фильтры» на шаге
«годы» иначе читалось бы как неверно введённый год).
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

MENU_ADD_FILTER = "➕ Фильтр"
MENU_FILTERS = "📋 Мои фильтры"
MENU_SUBSCRIBE = "💎 Подписка"
MENU_HELP = "❓ Помощь"

# Текст кнопки -> команда, которую она заменяет.
MENU_COMMANDS: dict[str, str] = {
    MENU_ADD_FILTER: "add_filter",
    MENU_FILTERS: "filters",
    MENU_SUBSCRIBE: "subscribe",
    MENU_HELP: "help",
}


def menu_command(text: str | None) -> str | None:
    """Команда за кнопкой меню; None — это не кнопка меню."""
    return MENU_COMMANDS.get((text or "").strip())


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_ADD_FILTER), KeyboardButton(text=MENU_FILTERS)],
            [KeyboardButton(text=MENU_SUBSCRIBE), KeyboardButton(text=MENU_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
