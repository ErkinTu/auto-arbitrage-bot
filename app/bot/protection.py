"""Защита контента бота: запрет пересылки/сохранения с whitelist-исключениями.

Ставим `protect_content=true` централизованно — в session-middleware aiogram,
а не на каждом `answer()`/`send_message()`: вызовов десятки, и любой новый
хендлер иначе молча уехал бы без защиты.

Что реально даёт `protect_content` (ограничения Telegram, не наши):
- «Переслать» и «Сохранить» пропадают у сообщения — это работает везде;
- скриншот блокирует только Android-клиент; iOS/Desktop/Web — нет;
- ссылку внутри сообщения юзер всё равно может открыть и скопировать из
  браузера, и всегда может сфотографировать экран другим телефоном.
То есть это барьер против массового репоста, а не защита информации.

Whitelist (`BOT_SHARE_WHITELIST_IDS`) — id тех, кому пересылка разрешена:
их сообщения уходят без protect_content.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType

from app.core.config import get_settings


class ContentProtectionMiddleware(BaseRequestMiddleware):
    """Проставляет protect_content всем исходящим методам, которые его поддерживают.

    Методы без этого поля (answerCallbackQuery, editMessageText, getChatMember…)
    проходят нетронутыми. Явно заданный в коде protect_content не перетираем.

    Незаданное поле у aiogram — не None, а sentinel `Default('protect_content')`;
    только его и None считаем «вызывающий не решал». Значение проставляем явным
    True/False (а не «True или оставить дефолт»), чтобы whitelist оставался
    главным, даже если когда-нибудь в DefaultBotProperties добавят
    protect_content.
    """

    async def __call__(
        self,
        make_request: Any,
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        if "protect_content" in type(method).model_fields:
            current = method.protect_content
            if current is None or isinstance(current, Default):
                method.protect_content = get_settings().protect_content_for(
                    getattr(method, "chat_id", None)
                )
        return await make_request(bot, method)
