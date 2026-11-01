"""Настройка логов для всех точек входа (бот, worker, CLI) + вырезание токена бота.

Telegram держит токен прямо в URL (`https://api.telegram.org/bot<TOKEN>/…`), а
`httpx` на уровне INFO пишет каждую строку запроса — так боевой токен оседал в
`docker logs` (найдено 01.11.2026). Глушить `httpx` целиком не хотим: по этим
строкам меряем время ответа прокси lalafo (`docs/planning/PUSH_PIPELINE_PLAN.md`).
Поэтому токен вырезается фильтром — и из сообщения, и из текста трейсбека
(`HTTPStatusError` тоже несёт URL).
"""

from __future__ import annotations

import logging
import re

# Токен бота: `<id бота>:<секрет>` (секрет — 35 символов base64url).
_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
REDACTED = "<token>"


def redact(text: str) -> str:
    return _TOKEN_RE.sub(REDACTED, text)


class RedactTokensFilter(logging.Filter):
    """Меняет токен на `<token>` в сообщении и в трейсбеке записи."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        if record.exc_info and not record.exc_text:
            # Formatter берёт готовый exc_text, если он есть, — подставляем чистый.
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_token_filter() -> None:
    """Фильтр без перенастройки логов — для `api` под uvicorn.

    Uvicorn настраивает только свои логгеры, корень пуст: наши исключения идут в
    `logging.lastResort`, а записи `httpx` — туда же или в пустоту. Ставим фильтр
    на оба пути, вывод `api` в остальном не меняется.
    """
    token_filter = RedactTokensFilter()
    for handler in [*logging.getLogger().handlers, logging.lastResort]:
        if handler is not None:
            handler.addFilter(token_filter)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).addFilter(token_filter)


def setup_logging(fmt: str) -> None:
    """basicConfig + фильтр на всех обработчиках корня и на логгерах HTTP-клиентов.

    Фильтр на логгерах `httpx`/`httpcore` — на случай процесса, где корень
    настраивает кто-то другой (uvicorn в `api`): там записи идут в чужие
    обработчики, и фильтр на наших до них бы не дошёл.
    """
    logging.basicConfig(level=logging.INFO, format=fmt)
    token_filter = RedactTokensFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(token_filter)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).addFilter(token_filter)
