"""Гейт админ-панели: что пускает и что обязано отказать.

Тесты здесь — не про фичу, а про границу: за ней боевая база, и «работает у
меня» тут не аргумент. Проверяем ровно отказы: истёкшая ссылка, повторное
использование, снятый флаг админа, чужая кука.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.admin.auth import router as auth_router
from app.db.base import Base
from app.db.session import get_session
from app.models import AdminLoginToken, AdminSession, BotUser
from app.services.admin_auth import _hash, exchange_login_token, resolve_session

ADMIN_TG = 500_100_200


@pytest.fixture()
def env() -> tuple[TestClient, sessionmaker]:
    import app.models  # noqa: F401
    from fastapi import FastAPI

    # StaticPool обязателен: у sqlite «:memory:» каждая связь — своя пустая база,
    # и create_all остался бы в другой, чем запрос из TestClient.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

    app = FastAPI()
    app.include_router(auth_router)

    def _session_dep():
        with Factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session_dep

    with Factory() as session:
        session.add(BotUser(telegram_id=ADMIN_TG, username="boss", is_superuser=True))
        session.commit()

    # base_url обязательно https: кука сессии помечена Secure, и по http клиент
    # её просто не отправит обратно — тест «вошёл и хожу» молча стал бы «вошёл и
    # везде 401». Прод у панели всё равно только по HTTPS.
    return TestClient(app, base_url="https://admin.test"), Factory


def _issue(Factory: sessionmaker, *, token: str, minutes: int = 5, used: bool = False) -> None:
    now = datetime.now(timezone.utc)
    with Factory() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == ADMIN_TG))
        session.add(
            AdminLoginToken(
                user_id=user.id,
                token_hash=_hash(token),
                expires_at=now + timedelta(minutes=minutes),
                used_at=now if used else None,
            )
        )
        session.commit()


def test_valid_link_opens_a_session(env) -> None:
    client, Factory = env
    _issue(Factory, token="good-one")

    response = client.post("/api/v1/admin/auth/exchange", json={"token": "good-one"})

    assert response.status_code == 200
    assert response.json()["telegram_id"] == ADMIN_TG
    cookie = response.cookies.get("admin_session")
    assert cookie
    # В базе — только хэш: дамп не должен давать готовую сессию.
    with Factory() as session:
        assert session.scalar(select(AdminSession).where(AdminSession.token_hash == cookie)) is None
        assert session.scalar(select(AdminSession).where(AdminSession.token_hash == _hash(cookie)))


def test_me_needs_a_session(env) -> None:
    client, _ = env
    assert client.get("/api/v1/admin/auth/me").status_code == 401


def test_me_rejects_a_made_up_cookie(env) -> None:
    client, _ = env
    client.cookies.set("admin_session", "definitely-not-a-real-session")
    assert client.get("/api/v1/admin/auth/me").status_code == 401


def test_link_works_exactly_once(env) -> None:
    client, Factory = env
    _issue(Factory, token="once")

    assert client.post("/api/v1/admin/auth/exchange", json={"token": "once"}).status_code == 200
    client.cookies.clear()
    second = client.post("/api/v1/admin/auth/exchange", json={"token": "once"})

    assert second.status_code == 401


def test_expired_link_is_refused(env) -> None:
    client, Factory = env
    _issue(Factory, token="stale", minutes=-1)

    assert client.post("/api/v1/admin/auth/exchange", json={"token": "stale"}).status_code == 401


def test_unknown_token_is_refused(env) -> None:
    client, _ = env
    assert client.post("/api/v1/admin/auth/exchange", json={"token": "nope"}).status_code == 401


def test_dropping_the_flag_kills_a_live_session(env) -> None:
    """Разжалованный админ не должен доходить до конца суток внутри панели."""
    client, Factory = env
    _issue(Factory, token="live")
    client.post("/api/v1/admin/auth/exchange", json={"token": "live"})
    assert client.get("/api/v1/admin/auth/me").status_code == 200

    with Factory() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == ADMIN_TG))
        user.is_superuser = False
        session.commit()

    assert client.get("/api/v1/admin/auth/me").status_code == 401


def test_logout_invalidates_the_session(env) -> None:
    client, Factory = env
    _issue(Factory, token="bye")
    client.post("/api/v1/admin/auth/exchange", json={"token": "bye"})

    assert client.post("/api/v1/admin/auth/logout").status_code == 204
    assert client.get("/api/v1/admin/auth/me").status_code == 401


def test_logout_without_a_session_is_not_an_error(env) -> None:
    client, _ = env
    assert client.post("/api/v1/admin/auth/logout").status_code == 204


def test_link_of_a_non_admin_is_refused(env) -> None:
    """Флаг могли снять уже после выдачи ссылки — решает момент входа."""
    client, Factory = env
    _issue(Factory, token="demoted")
    with Factory() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == ADMIN_TG))
        user.is_superuser = False
        session.commit()

    assert client.post("/api/v1/admin/auth/exchange", json={"token": "demoted"}).status_code == 401


def test_expired_session_stops_working(env) -> None:
    client, Factory = env
    _issue(Factory, token="short")
    client.post("/api/v1/admin/auth/exchange", json={"token": "short"})

    with Factory() as session:
        row = session.scalars(select(AdminSession)).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()

    assert client.get("/api/v1/admin/auth/me").status_code == 401
    with Factory() as session:
        assert session.scalars(select(AdminSession)).all() == []


def test_resolve_session_ignores_empty_input() -> None:
    """Пустая кука не должна превращаться в поиск по базе."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        assert resolve_session(session, None) is None
        assert resolve_session(session, "") is None


def test_concurrent_exchange_claims_the_link_once(env) -> None:
    """Второй одновременный обмен обязан уйти ни с чем (условный UPDATE)."""
    client, Factory = env
    _issue(Factory, token="race")

    with Factory() as first, Factory() as second:
        exchange_login_token(first, "race")
        with pytest.raises(Exception):
            exchange_login_token(second, "race")
