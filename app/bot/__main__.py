"""Entrypoint: python -m app.bot"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.access import AccessMiddleware
from app.bot.commands import setup_bot_commands
from app.bot.executors import install_default_executor
from app.bot.handlers import router
from app.bot.poller import polling_loop
from app.bot.protection import ContentProtectionMiddleware
from app.bot.throttle import ThrottlingMiddleware
from app.bot.wizard import WizardExitMiddleware
from app.core.config import get_settings
from app.core.logging_setup import setup_logging

setup_logging("%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан. Заполните .env и перезапустите.")
    if not settings.telegram_access_chat_id:
        logger.warning(
            "TELEGRAM_ACCESS_CHAT_ID не задан: проверка членства в группе отключена, "
            "бот доступен всем."
        )

    # Схему, источники и каталог готовит сервис `migrate` (init-db + seed) до
    # старта бота: раньше это делали все три сервиса одновременно и толкались
    # за блокировку `listings` (зависание выкатки 21.10.2026).

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Session-middleware, а не default-параметр: whitelist решает по chat_id
    # каждого вызова, а DefaultBotProperties одинаков для всех чатов.
    bot.session.middleware(ContentProtectionMiddleware())
    if settings.bot_protect_content:
        logger.info(
            "content protection on; forwarding allowed for %s id(s)",
            len(settings.share_whitelist_id_set),
        )
    # Пул потоков для запросов к базе — явно (п. К плана 01.11.2026): пул по
    # умолчанию на 2 vCPU — 6 потоков, и медленные источники выедали его целиком.
    install_default_executor(asyncio.get_running_loop())
    dispatcher = Dispatcher(storage=MemoryStorage())
    # Ограничитель частоты — ПЕРВЫМ: лишний апдейт не идёт даже в базу за
    # проверкой доступа. Один экземпляр на сообщения и кнопки — ведро общее.
    throttle = ThrottlingMiddleware(
        settings.bot_throttle_burst, settings.bot_throttle_rate_per_second
    )
    dispatcher.message.outer_middleware(throttle)
    dispatcher.callback_query.outer_middleware(throttle)
    access = AccessMiddleware()
    dispatcher.message.outer_middleware(access)
    # После access: сбрасываем визард только у тех, кого бот вообще обслуживает.
    dispatcher.message.outer_middleware(WizardExitMiddleware())
    dispatcher.callback_query.outer_middleware(access)
    dispatcher.include_router(router)

    await setup_bot_commands(bot)

    poller_task = asyncio.create_task(polling_loop(bot))
    try:
        logger.info("bot polling started")
        await dispatcher.start_polling(bot)
    finally:
        poller_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
