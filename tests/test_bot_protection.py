"""Защита контента: protect_content на исходящих + whitelist по telegram id."""

from __future__ import annotations

import asyncio

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from app.bot.protection import ContentProtectionMiddleware
from app.core.config import Settings


def _settings(**kwargs) -> Settings:
    defaults = {"bot_protect_content": True, "bot_share_whitelist_ids": ""}
    return Settings(**{**defaults, **kwargs})


@pytest.fixture
def patch_settings(monkeypatch):
    """Подменяет get_settings и в config, и в модуле middleware."""

    def apply(settings: Settings) -> None:
        monkeypatch.setattr("app.bot.protection.get_settings", lambda: settings)
        monkeypatch.setattr("app.services.telegram.get_settings", lambda: settings)

    return apply


# --- whitelist-разбор -------------------------------------------------------


def test_whitelist_parses_ids_and_ignores_junk():
    settings = _settings(bot_share_whitelist_ids=" 111 , 222,, не_число ,333")
    assert settings.share_whitelist_id_set == frozenset({111, 222, 333})


def test_empty_whitelist_protects_everyone():
    settings = _settings()
    assert settings.protect_content_for(111) is True


def test_whitelisted_id_is_not_protected():
    settings = _settings(bot_share_whitelist_ids="111,222")
    assert settings.protect_content_for(111) is False
    assert settings.protect_content_for(222) is False
    assert settings.protect_content_for(333) is True


def test_feature_flag_off_disables_protection_for_all():
    settings = _settings(bot_protect_content=False, bot_share_whitelist_ids="111")
    assert settings.protect_content_for(111) is False
    assert settings.protect_content_for(999) is False


def test_string_chat_id_from_whitelist_matches():
    """chat_id приходит строкой из БД/JSON — сравнение всё равно числовое."""
    settings = _settings(bot_share_whitelist_ids="111")
    assert settings.protect_content_for("111") is False


def test_non_numeric_chat_id_is_protected():
    settings = _settings(bot_share_whitelist_ids="111")
    assert settings.protect_content_for("@some_channel") is True
    assert settings.protect_content_for(None) is True


# --- session-middleware -----------------------------------------------------


def _run(method, middleware: ContentProtectionMiddleware | None = None):
    """Прогоняет метод через middleware, возвращая его же (запрос наружу не уходит)."""
    captured = {}

    async def make_request(bot, sent_method):
        captured["method"] = sent_method
        return None

    middleware = middleware or ContentProtectionMiddleware()
    asyncio.run(middleware(make_request, bot=None, method=method))
    return captured["method"]


def test_send_message_gets_protected(patch_settings):
    patch_settings(_settings())
    assert _run(SendMessage(chat_id=555, text="hi")).protect_content is True


def test_send_message_to_whitelisted_stays_unprotected(patch_settings):
    patch_settings(_settings(bot_share_whitelist_ids="555"))
    assert _run(SendMessage(chat_id=555, text="hi")).protect_content is False


def test_explicit_protect_content_is_not_overridden(patch_settings):
    """Явное False в коде — осознанное решение вызывающего, не перетираем."""
    patch_settings(_settings())
    method = _run(SendMessage(chat_id=555, text="hi", protect_content=False))
    assert method.protect_content is False


def test_methods_without_protect_content_pass_through(patch_settings):
    """answerCallbackQuery/editMessageText не имеют поля — не должны падать."""
    patch_settings(_settings())
    assert _run(AnswerCallbackQuery(callback_query_id="1")) is not None
    assert _run(EditMessageText(chat_id=555, message_id=1, text="x")) is not None


def test_middleware_is_wired_into_bot_session(monkeypatch, patch_settings):
    """Сквозная проверка регистрации: bot.send_message идёт через middleware."""
    patch_settings(_settings())
    bot = Bot(token="123:AAA")
    bot.session.middleware(ContentProtectionMiddleware())
    captured = {}

    async def fake_make_request(bot_, method, timeout=None):
        captured["method"] = method
        return None

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    asyncio.run(bot.send_message(chat_id=555, text="hi"))
    assert captured["method"].protect_content is True


# --- HTTP-сендер из worker/api ---------------------------------------------


def test_http_sender_sets_protect_content(monkeypatch, patch_settings):
    patch_settings(_settings(telegram_bot_token="token"))
    sent = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    def fake_post(url, json, timeout):
        sent.update(json)
        return FakeResponse()

    monkeypatch.setattr("app.services.telegram.httpx.post", fake_post)
    from app.services.telegram import send_message

    assert send_message(777, "напоминание") is True
    assert sent["protect_content"] is True


def test_http_sender_skips_protection_for_whitelisted(monkeypatch, patch_settings):
    patch_settings(_settings(telegram_bot_token="token", bot_share_whitelist_ids="777"))
    sent = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    def fake_post(url, json, timeout):
        sent.update(json)
        return FakeResponse()

    monkeypatch.setattr("app.services.telegram.httpx.post", fake_post)
    from app.services.telegram import send_message

    assert send_message(777, "напоминание") is True
    assert "protect_content" not in sent
