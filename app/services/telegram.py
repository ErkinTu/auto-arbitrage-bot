"""Отправка сообщения через Bot API по HTTP из процессов без aiogram (worker).

sendMessage по HTTP не конфликтует с long-polling бота (конфликт даёт лишь
второй getUpdates), поэтому worker/api могут слать сообщения тем же токеном.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def send_message(chat_id: int, text: str) -> bool:
    """Best-effort отправка; возвращает True при успехе, False при ошибке/без токена."""
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        return False
    payload: dict[str, object] = {"chat_id": chat_id, "text": text}
    # Этот путь минует aiogram (и его session-middleware), поэтому защиту
    # ставим здесь сами — правило про whitelist общее с ботом.
    if settings.protect_content_for(chat_id):
        payload["protect_content"] = True
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.exception("failed to send telegram message to %s", chat_id)
        return False
