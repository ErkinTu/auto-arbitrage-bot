"""Вход в админ-панель: обмен одноразовой ссылки на сессию и её проверка.

Две стороны одного контура. Ссылку выдаёт бот (`bot/db.py::issue_admin_login_token`),
здесь она гасится и превращается в сессию. Общего HTTP между ботом и API нет —
оба ходят в БД напрямую, как и весь остальной проект.

Инварианты, ради которых этот файл существует:
  * секретов в базе нет — и ссылка, и сессия хранятся как sha256;
  * ссылка срабатывает ровно один раз, даже если два запроса пришли разом;
  * любая неясность — отказ: истёкшее, погашенное, чужое, снятый флаг админа.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import AdminLoginToken, AdminSession, BotUser

logger = logging.getLogger(__name__)

SESSION_COOKIE = "admin_session"
# Как часто освежаем last_seen_at: чаще смысла нет, а запись на каждый GET есть.
LAST_SEEN_REFRESH = timedelta(minutes=1)


class LoginError(Exception):
    """Обмен не состоялся. Причину наружу не раскрываем — только в лог."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite в тестах теряет tz у DateTime(timezone=True) — выравниваем."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# --- Ограничение перебора ----------------------------------------------------
# В процессе, а не в БД: контейнер `api` один, а писать в базу на каждую попытку
# входа — значит дать простой способ раздуть её мусором. 32 байта энтропии
# перебрать всё равно нельзя; лимит нужен, чтобы шум не забивал логи.
_ATTEMPTS: dict[str, list[float]] = {}
_ATTEMPT_WINDOW_SECONDS = 300
_ATTEMPT_LIMIT = 10


def too_many_attempts(client: str) -> bool:
    now = time.monotonic()
    recent = [item for item in _ATTEMPTS.get(client, []) if now - item < _ATTEMPT_WINDOW_SECONDS]
    recent.append(now)
    _ATTEMPTS[client] = recent
    # Словарь не чистим по таймеру: ключей ровно столько, сколько адресов
    # ломилось за пять минут, и каждый список обрезается при следующем заходе.
    return len(recent) > _ATTEMPT_LIMIT


@dataclass(slots=True)
class AdminIdentity:
    """Кто вошёл. ORM-объект наружу не отдаём — роутеру нужны только эти поля."""

    telegram_id: int
    username: str | None
    first_name: str | None
    session_expires_at: datetime


def exchange_login_token(session: Session, token: str) -> tuple[str, AdminIdentity]:
    """Гасит ссылку и заводит сессию. Возвращает (raw-токен сессии, кто вошёл).

    Погашение — условный UPDATE с `used_at IS NULL` в условии: два одновременных
    запроса с одной ссылкой дадут rowcount 1 и 0, и второй уйдёт с отказом.
    Проверка «выбрал → потом записал» здесь бы не спасла.
    """
    settings = get_settings()
    if not token:
        raise LoginError("empty token")

    row = session.scalar(
        select(AdminLoginToken).where(AdminLoginToken.token_hash == _hash(token))
    )
    if row is None:
        raise LoginError("unknown token")

    now = _now()
    if row.used_at is not None:
        raise LoginError("token already used")
    expires_at = _aware(row.expires_at)
    if expires_at is None or expires_at <= now:
        raise LoginError("token expired")

    claimed = session.execute(
        update(AdminLoginToken)
        .where(AdminLoginToken.id == row.id, AdminLoginToken.used_at.is_(None))
        .values(used_at=now)
    )
    if claimed.rowcount != 1:
        raise LoginError("token claimed concurrently")

    user = session.get(BotUser, row.user_id)
    # Флаг могли снять уже после выдачи ссылки — решает состояние на момент входа.
    if user is None or not user.is_superuser:
        raise LoginError("not a superuser anymore")

    # Заодно подчищаем протухшие сессии этого админа: единственное место, где
    # они гарантированно попадаются на глаза, а отдельный джоб ради этого лишний.
    session.execute(
        delete(AdminSession).where(
            AdminSession.user_id == user.id, AdminSession.expires_at <= now
        )
    )

    raw = secrets.token_urlsafe(32)
    session_expires_at = now + timedelta(hours=settings.admin_session_ttl_hours)
    session.add(
        AdminSession(user_id=user.id, token_hash=_hash(raw), expires_at=session_expires_at)
    )
    session.commit()
    logger.info("Админ-вход: сессия открыта, telegram_id=%s", user.telegram_id)
    return raw, AdminIdentity(
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        session_expires_at=session_expires_at,
    )


def resolve_session(session: Session, raw: str | None) -> AdminIdentity | None:
    """Кто стоит за кукой. None — на любое сомнение, вызывающий отвечает 401."""
    if not raw:
        return None

    row = session.scalar(select(AdminSession).where(AdminSession.token_hash == _hash(raw)))
    if row is None:
        return None

    now = _now()
    expires_at = _aware(row.expires_at)
    if expires_at is None or expires_at <= now:
        # Мусор убираем сразу: истёкшая сессия больше ни для чего не нужна.
        session.delete(row)
        session.commit()
        return None

    user = session.get(BotUser, row.user_id)
    if user is None or not user.is_superuser:
        # Снятый флаг обрывает уже открытую сессию — иначе разжалованный админ
        # ходил бы в панель до конца суток.
        session.delete(row)
        session.commit()
        return None

    last_seen = _aware(row.last_seen_at)
    if last_seen is None or now - last_seen > LAST_SEEN_REFRESH:
        row.last_seen_at = now
        session.commit()

    return AdminIdentity(
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        session_expires_at=expires_at,
    )


def revoke_session(session: Session, raw: str | None) -> None:
    """Выход. Молчит, если куки нет или она уже недействительна."""
    if not raw:
        return
    session.execute(delete(AdminSession).where(AdminSession.token_hash == _hash(raw)))
    session.commit()
