"""Entrypoint: python -m app.bot"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.access import AccessMiddleware
from app.bot.handlers import router
from app.bot.poller import polling_loop
from app.core.config import get_settings
from app.db.session import SessionLocal, init_db
from app.services.catalog_service import seed_default_catalog
from app.services.lookup import get_or_create_source
from app.services.sources import SOURCE_NAMES, source_base_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def bootstrap() -> None:
    init_db()
    with SessionLocal() as session:
        for source_name in SOURCE_NAMES:
            get_or_create_source(session, source_name, source_base_url(source_name))
        seed_default_catalog(session)
        session.commit()


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан. Заполните .env и перезапустите.")
    if not settings.telegram_access_chat_id:
        logger.warning(
            "TELEGRAM_ACCESS_CHAT_ID не задан: проверка членства в группе отключена, "
            "бот доступен всем."
        )

    await asyncio.to_thread(bootstrap)

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    access = AccessMiddleware()
    dispatcher.message.outer_middleware(access)
    dispatcher.callback_query.outer_middleware(access)
    dispatcher.include_router(router)

    poller_task = asyncio.create_task(polling_loop(bot))
    try:
        logger.info("bot polling started")
        await dispatcher.start_polling(bot)
    finally:
        poller_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
