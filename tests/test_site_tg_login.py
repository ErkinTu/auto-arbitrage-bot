"""Вход на сайт кнопкой «Войти через Telegram» (`services/site_tg_login.py`, план — TELEGRAM_LOGIN.md).

Сайт: /auth/tg/start → код на экране и ссылка на бота; бот: человек выбирает код;
сайт: /auth/tg/poll → сессия в этом же браузере. Роль и подписка — из бота.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import BotUser, SiteLoginRequest, UserEvent
from app.services import site_tg_login
from app.services.start_source import parse_source


@pytest.fixture()
def env(monkeypatch):
    from app import models as _models  # noqa: F401 — регистрирует таблицы

    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _dep():
        with Factory() as session:
            yield session

    app.dependency_overrides[get_session] = _dep
    monkeypatch.setattr(get_settings(), "site_url", "https://site.test")
    monkeypatch.setattr(get_settings(), "bot_username", "skupka_test_bot")
    yield TestClient(app, base_url="https://site.test"), Factory
    app.dependency_overrides.clear()


def _user(Factory, tg: int, **kw) -> None:
    with Factory() as s:
        s.add(BotUser(telegram_id=tg, username=f"u{tg}", **{"access_status": "revoked", **kw}))
        s.commit()


def _start(client: TestClient) -> tuple[dict, str]:
    started = client.post("/api/v1/auth/tg/start")
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["bot_url"].startswith("https://t.me/skupka_test_bot?start=login_")
    nonce = site_tg_login.parse_nonce(body["bot_url"].split("start=", 1)[1])
    return body, nonce


def _poll(client: TestClient, token: str):
    return client.post("/api/v1/auth/tg/poll", json={"poll_token": token})


def test_full_flow_logs_in_this_browser_once(env) -> None:
    client, Factory = env
    _user(Factory, 100)
    body, nonce = _start(client)
    assert _poll(client, body["poll_token"]).json() == {"status": "pending", "me": None}

    with Factory() as s:
        prompt = site_tg_login.claim(s, nonce, 100)
    assert body["code"] in prompt.options and len(set(prompt.options)) == 3
    with Factory() as s:
        assert site_tg_login.answer(s, prompt.request_id, 100, body["code"]) == "confirmed"

    done = _poll(client, body["poll_token"])
    assert done.json()["status"] == "confirmed"
    assert done.json()["me"]["user"]["telegram_id"] == 100 and done.json()["me"]["role"] == "user"
    cookie = done.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert client.get("/api/v1/auth/me").json()["user"]["telegram_id"] == 100
    # Второй опрос сессию не даёт (одноразово).
    again = _poll(client, body["poll_token"])
    assert again.json()["status"] == "expired" and "set-cookie" not in again.headers
    with Factory() as s:
        events = s.scalars(select(UserEvent.payload).where(UserEvent.event == "site_login")).all()
    assert events == [{"method": "button"}]


def test_role_and_subscription_come_from_bot(env) -> None:
    client, Factory = env
    _user(Factory, 200, subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=10), access_status="subscriber")
    body, nonce = _start(client)
    with Factory() as s:
        prompt = site_tg_login.claim(s, nonce, 200)
        site_tg_login.answer(s, prompt.request_id, 200, body["code"])
    me = _poll(client, body["poll_token"]).json()["me"]
    assert me["role"] == "subscriber" and me["access"]["active"] is True


@pytest.mark.parametrize("choice", ["no", "wrong"])
def test_wrong_code_or_not_me_denies(env, choice) -> None:
    client, Factory = env
    _user(Factory, 300)
    body, nonce = _start(client)
    with Factory() as s:
        prompt = site_tg_login.claim(s, nonce, 300)
        wrong = next(o for o in prompt.options if o != body["code"])
        result = site_tg_login.answer(s, prompt.request_id, 300, "no" if choice == "no" else wrong)
    assert result == "denied"
    assert _poll(client, body["poll_token"]).json()["status"] == "denied"
    with Factory() as s:  # повторное нажатие — «уже использована»
        assert site_tg_login.answer(s, prompt.request_id, 300, body["code"]) == "used"


def test_link_belongs_to_first_opener(env) -> None:
    """Пересланную ссылку не подтвердить чужим аккаунтом."""
    client, Factory = env
    _user(Factory, 400)
    _user(Factory, 401)
    body, nonce = _start(client)
    with Factory() as s:
        prompt = site_tg_login.claim(s, nonce, 400)
        assert site_tg_login.claim(s, nonce, 401) == "used"
        assert site_tg_login.answer(s, prompt.request_id, 401, body["code"]) == "used"
    assert _poll(client, body["poll_token"]).json()["status"] == "pending"


def test_expired_request(env) -> None:
    client, Factory = env
    _user(Factory, 500)
    body, nonce = _start(client)
    with Factory() as s:
        s.execute(update(SiteLoginRequest).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        s.commit()
        assert site_tg_login.claim(s, nonce, 500) == "expired"
    assert _poll(client, body["poll_token"]).json()["status"] == "expired"
    with Factory() as s:
        assert site_tg_login.claim(s, "no-such-nonce", 500) == "unknown"


def test_unknown_poll_token_and_foreign_origin(env) -> None:
    client, _ = env
    assert _poll(client, "x" * 40).json()["status"] == "expired"
    assert client.post("/api/v1/auth/tg/start", headers={"Origin": "https://evil.test"}).status_code == 403


def test_unavailable_without_bot_or_site(env, monkeypatch) -> None:
    client, _ = env
    monkeypatch.setattr(get_settings(), "bot_username", "")
    assert client.post("/api/v1/auth/tg/start").status_code == 503


def test_start_is_rate_limited(env) -> None:
    client, _ = env
    codes = [client.post("/api/v1/auth/tg/start").status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[-1] == 429


def test_secrets_are_hashed_and_nonce_not_a_source(env) -> None:
    client, Factory = env
    body, nonce = _start(client)
    with Factory() as s:
        row = s.scalar(select(SiteLoginRequest))
    assert nonce not in (row.nonce_hash, row.poll_hash) and body["poll_token"] != row.poll_hash
    assert parse_source(f"login_{nonce}") == "site_login"
