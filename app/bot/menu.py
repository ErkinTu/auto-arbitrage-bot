"""Постоянное меню: четыре кнопки под полем ввода.

Кнопки — основной путь для тех, кто не знает про «/»; команды остаются
запасным, внутри сценариев — инлайн-кнопки, как раньше. Всё в кнопки НЕ
переводим: настройку фильтров заберёт мини-приложение.

Текст кнопки приходит обычным сообщением без «/», поэтому в двух местах его
приравниваем к команде: доступ (`access.py` — «Доступ» нужен и без доступа)
и выход из визарда (`wizard.py` — нажатие «Мои фильтры» на шаге «годы» иначе
читалось бы как неверно введённый год).

Набор кнопок — решение владельца 22.10.2026. «Помощь» ушла из меню (осталась
командой /help и текстом /start), но её старая кнопка, как и «💎 Подписка»,
продолжает работать: reply-клавиатура живёт в чате, пока её не заменят, и у
всех, кто открыл бота до смены, ещё долго будут прежние кнопки.
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

MENU_BEST = "🔥 Лучшее сейчас"
MENU_ADD_FILTER = "➕ Фильтр"
MENU_FILTERS = "📋 Мои фильтры"
MENU_ACCESS = "💎 Доступ"
# Кнопки прежнего меню — только чтобы старые клавиатуры в чатах не сломались.
LEGACY_MENU_SUBSCRIBE = "💎 Подписка"
LEGACY_MENU_HELP = "❓ Помощь"

# Текст кнопки -> команда, которую она заменяет. «Доступ» — входная команда
# `subscribe`: открывать её должен и тот, у кого доступа нет.
MENU_COMMANDS: dict[str, str] = {
    MENU_BEST: "best",
    MENU_ADD_FILTER: "add_filter",
    MENU_FILTERS: "filters",
    MENU_ACCESS: "subscribe",
    LEGACY_MENU_SUBSCRIBE: "subscribe",
    LEGACY_MENU_HELP: "help",
}


def menu_command(text: str | None) -> str | None:
    """Команда за кнопкой меню; None — это не кнопка меню."""
    return MENU_COMMANDS.get((text or "").strip())


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_BEST), KeyboardButton(text=MENU_ADD_FILTER)],
            [KeyboardButton(text=MENU_FILTERS), KeyboardButton(text=MENU_ACCESS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
