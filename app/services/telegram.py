"""Отправка сообщения через Bot API по HTTP из процессов без aiogram (worker).

sendMessage по HTTP не конфликтует с long-polling бота (конфликт даёт лишь
второй getUpdates), поэтому worker/api могут слать сообщения тем же токеном.

Результат — три состояния, а не bool: вызывающему важно отличать «адресата
больше нет» от «сейчас не вышло, но стоит повторить». Оба сообщения, которые
шлёт worker, одноразовые по флагу в БД, и от этой разницы зависит, ставить
флаг или нет: на 429 флаг = молча потерянное сообщение, на заблокировавшего
без флага = вечные попытки каждый цикл.
"""

from __future__ import annotations

import enum
import logging
import time

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Дольше этого ждать в worker-е не будем: цикл и так придёт снова через минуту,
# а держать поток в sleep на несколько минут дороже, чем отложить сообщение.
MAX_RETRY_AFTER_SECONDS = 30
# Тексты Bot API, означающие, что адресата больше нет. Правило то же, что в
# bot/notifier.py: сверяем по тексту, потому что под общий HTTP 400 попадают и
# наши собственные ошибки в запросе.
GONE_CHAT_MARKERS = (
    "chat not found",
    "user is deactivated",
    "bot was kicked",
    "peer_id_invalid",
)


class SendOutcome(enum.Enum):
    """Исход отправки с точки зрения «повторять ли»."""

    DELIVERED = "delivered"
    # Адресат недоступен навсегда: заблокировал бота, удалил аккаунт. Повтор
    # бессмыслен — разовые сообщения помечаем отправленными.
    UNDELIVERABLE = "undeliverable"
    # Временная беда: 429, сеть, 5xx. Флаг ставить нельзя — потеряем сообщение.
    FAILED = "failed"


def _is_gone_chat(description: str) -> bool:
    text = description.lower()
    return any(marker in text for marker in GONE_CHAT_MARKERS)


def _retry_after(response: httpx.Response) -> int | None:
    """Сколько ждать по требованию Telegram (parameters.retry_after)."""
    try:
        payload = response.json()
    except ValueError:
        return None
    value = (payload.get("parameters") or {}).get("retry_after")
    return value if isinstance(value, int) else None


def _describe(response: httpx.Response) -> str:
    try:
        return str(response.json().get("description", ""))
    except ValueError:
        return response.text


def send_message(chat_id: int, text: str) -> SendOutcome:
    """Отправка одного сообщения; см. SendOutcome про трактовку результата."""
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        return SendOutcome.FAILED
    payload: dict[str, object] = {"chat_id": chat_id, "text": text}
    # Этот путь минует aiogram (и его session-middleware), поэтому защиту
    # ставим здесь сами — правило про whitelist общее с ботом.
    if settings.protect_content_for(chat_id):
        payload["protect_content"] = True

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in (1, 2):
        try:
            response = httpx.post(url, json=payload, timeout=10.0)
        except httpx.HTTPError:
            logger.exception("failed to send telegram message to %s", chat_id)
            return SendOutcome.FAILED

        if response.status_code == 200:
            return SendOutcome.DELIVERED

        description = _describe(response)
        if response.status_code == 429 and attempt == 1:
            delay = _retry_after(response)
            if delay is not None and delay <= MAX_RETRY_AFTER_SECONDS:
                logger.warning("rate limited for %s, retry after %s s", chat_id, delay)
                time.sleep(delay)
                continue
            logger.warning("rate limited for %s, retry_after=%s — откладываем", chat_id, delay)
            return SendOutcome.FAILED

        if response.status_code == 403 or (
            response.status_code == 400 and _is_gone_chat(description)
        ):
            logger.info("chat %s unreachable: %s", chat_id, description)
            return SendOutcome.UNDELIVERABLE

        # Всё прочее (наш битый запрос, 5xx) — повторяемо. Молча гасить
        # одноразовое сообщение нельзя: лучше шумные ретраи в логах, которые
        # видно, чем потерянный человек, которого не видно.
        logger.error(
            "telegram sendMessage to %s failed: HTTP %s %s",
            chat_id,
            response.status_code,
            description,
        )
        return SendOutcome.FAILED

    return SendOutcome.FAILED
