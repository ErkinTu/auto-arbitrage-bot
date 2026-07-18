"""Раздельные паузы lalafo: лента — feed-пауза, карточки — detail-пауза,
429 удваивает detail-паузу до конца работы инстанса парсера."""

from typing import Iterator

import httpx
import pytest

from app.core.config import get_settings
from app.parsers.lalafo import LalafoParser


@pytest.fixture()
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr("app.parsers.lalafo.time.sleep", recorded.append)
    return recorded


def _parser(monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]) -> LalafoParser:
    # category_id Toyota уже в кэше класса — HTTP-резолва марки не будет.
    monkeypatch.setattr(LalafoParser, "_category_cache", {"Toyota": 123})
    queue: Iterator[httpx.Response] = iter(responses)
    parser = LalafoParser()
    parser.client = httpx.Client(transport=httpx.MockTransport(lambda request: next(queue)))
    return parser


def test_feed_and_detail_use_their_own_delays(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    settings = get_settings()
    parser = _parser(
        monkeypatch,
        [
            httpx.Response(200, json={"items": [{"id": 1}], "_meta": {"totalCount": 1}}),
            httpx.Response(200, json={"id": 1}),
        ],
    )

    parser.fetch_listings(
        brand="Toyota", model=None, created_period="1d", page=1, size=50, sort_by="newest", order="desc"
    )
    parser.fetch_details(1)

    assert sleeps == [settings.lalafo_feed_delay_seconds, settings.lalafo_detail_delay_seconds]


def test_429_doubles_detail_delay_for_parser_lifetime(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    settings = get_settings()
    parser = _parser(
        monkeypatch,
        [
            httpx.Response(429),
            httpx.Response(200, json={"id": 1}),
            httpx.Response(200, json={"id": 2}),
        ],
    )

    parser.fetch_details(1)
    assert parser._detail_delay == settings.lalafo_detail_delay_seconds * 2

    # Удвоенная пауза действует и на следующие карточки этого цикла.
    parser.fetch_details(2)
    assert sleeps[-1] == settings.lalafo_detail_delay_seconds * 2
