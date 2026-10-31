"""Токен бота не попадает в логи (найдено 01.11.2026 в `docker logs` worker).

`httpx` на INFO пишет URL запроса, а у Telegram токен — часть URL.
"""

from __future__ import annotations

import io
import logging

import httpx
import pytest

from app.core.logging_setup import REDACTED, RedactTokensFilter, redact

TOKEN = "1234567890:AAFakeFakeFakeFakeFakeFakeFakeFake123"  # выдуманный, формат как у настоящего
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


@pytest.fixture()
def captured():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactTokensFilter())
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test.redaction")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield logger, stream
    logger.removeHandler(handler)


def test_redact_hides_token_and_keeps_the_rest() -> None:
    assert redact(URL) == f"https://api.telegram.org/bot{REDACTED}/sendMessage"
    # Обычные числа и хэши фото не трогаем.
    assert redact("listing 213145 hash 383b27e2495ee6f7946e45ddb2e90bfa") == (
        "listing 213145 hash 383b27e2495ee6f7946e45ddb2e90bfa"
    )


def test_httpx_style_record_is_redacted(captured) -> None:
    logger, stream = captured
    # Ровно так пишет httpx: шаблон + URL в аргументах.
    logger.info('HTTP Request: %s %s "%s %d %s"', "POST", httpx.URL(URL), "HTTP/1.1", 200, "OK")
    out = stream.getvalue()
    assert TOKEN not in out
    assert f"bot{REDACTED}/sendMessage" in out


def test_traceback_is_redacted(captured) -> None:
    logger, stream = captured
    request = httpx.Request("POST", URL)
    try:
        raise httpx.HTTPStatusError(
            f"Client error for url '{URL}'", request=request, response=httpx.Response(400, request=request)
        )
    except httpx.HTTPError:
        logger.exception("failed to send telegram message to %s", 1)
    out = stream.getvalue()
    assert TOKEN not in out
    assert "failed to send telegram message to 1" in out
    assert "HTTPStatusError" in out  # трейсбек остался, только без секрета
