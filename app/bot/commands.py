"""Меню команд Telegram: автодополнение по «/» и кнопка «Меню».

Задаётся кодом, а не руками в BotFather, по двум причинам: список едет вместе
с релизом (добавили команду — она сразу видна всем, без ручного шага, о котором
через полгода никто не вспомнит) и не расходится с реально
зарегистрированными хендлерами.

Чего в списке сознательно НЕТ:
  `/start`     — Telegram показывает его новым чатам сам;
  `/subscribe` — пока оплата обкатывается (`subscription_enabled=false` или
                 непустой `SUBSCRIPTION_TESTER_IDS`), команда отвечает «скоро».
                 Звать в неё из постоянного меню — вести человека в тупик.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand

logger = logging.getLogger(__name__)

# Порядок — по частоте использования, а не по алфавиту: меню читают сверху вниз,
# и первым должно стоять то, ради чего человек вообще открыл бота.
BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("add_filter", "Создать фильтр поиска"),
    ("filters", "Мои фильтры"),
    ("best", "Лучшие предложения сейчас"),
    ("blacklist", "Что не показывать"),
    ("invite", "Пригласить друзей — дни бесплатно"),
    ("status", "Мой доступ"),
    ("help", "Как это работает"),
    ("cancel", "Отменить текущее действие"),
)


async def setup_bot_commands(bot: Bot) -> None:
    """Публикует меню команд.

    Ошибку глушим намеренно: меню — украшение, а недоступный в этот момент
    api.telegram.org не повод не поднимать бота (рассылка важнее подсказок).
    """
    try:
        await bot.set_my_commands(
            [
                BotCommand(command=name, description=description)
                for name, description in BOT_COMMANDS
            ]
        )
    except Exception:
        logger.exception("failed to publish bot commands menu")
    else:
        logger.info("bot commands menu published: %s command(s)", len(BOT_COMMANDS))
