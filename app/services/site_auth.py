"""Вход на сайт через Telegram-бота и роли посетителя.

Роли (решение владельца 09.11.2026), по возрастанию:

  guest       — не вошёл: лента, карточки с оценкой рынка, страница авто;
  user        — вошёл через бота, доступа нет: то же (+ личное — позже);
  subscriber  — активный доступ, ровно как у пушей бота: член группы, оплата
                или триал. Открывает графики и экраны аналитики;
  admin       — `bot_users.is_superuser`: всё + база пользователей, платежи.

Роль НЕ хранится в сессии — считается на каждом запросе по `bot_users`:
кончилась подписка или сняли флаг админа — действует сразу.

Ссылка входа и сессия устроены как у админки (`admin_auth.py`): в базе только
sha256, ссылка одноразовая (условный UPDATE), любая неясность — отказ.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import BotUser, SiteLoginToken, SiteSession

logger = logging.getLogger(__name__)

SESSION_COOKIE = "sk_session"
LAST_SEEN_REFRESH = timedelta(minutes=5)

Role = Literal["guest", "user", "subscriber", "admin"]
_RANK: dict[str, int] = {"guest": 0, "user": 1, "subscriber": 2, "admin": 3}


def role_at_least(role: str, minimum: str) -> bool:
    return _RANK.get(role, 0) >= _RANK[minimum]


class LoginError(Exception):
    """Обмен не состоялся; причина — только в лог."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# --- роль ---------------------------------------------------------------------


@dataclass(slots=True)
class Access:
    """Чем открыт доступ подписчика — для экрана профиля."""

    status: str  # member | subscriber | trial | revoked
    active: bool
    expires_at: datetime | None


def access_of(user: BotUser, now: datetime | None = None) -> Access:
    """Активный доступ по данным в базе — те же три источника, что у бота
    (`bot/db.py::decide_access_status`): группа → оплата → триал.

    Членство в группе бот синхронизирует в `access_status='member'` при каждом
    обращении; сайт Telegram не дёргает — берёт последнее известное.
    """
    now = now or _now()
    sub = _aware(user.subscription_expires_at)
    trial = _aware(user.trial_expires_at)
    # 'member' — дефолт колонки: без отметки проверки бот его не подтверждал
    # (путь /start при выключенном гейте), и раздавать по нему доступ нельзя.
    if user.access_status == "member" and user.last_access_check_at is not None:
        return Access("member", True, None)
    if sub is not None and sub > now:
        return Access("subscriber", True, sub)
    if trial is not None and trial > now:
        return Access("trial", True, trial)
    return Access("revoked", False, max(filter(None, [sub, trial]), default=None))


def role_of(user: BotUser | None, now: datetime | None = None) -> Role:
    if user is None:
        return "guest"
    if user.is_superuser:
        return "admin"
    return "subscriber" if access_of(user, now).active else "user"


# --- ссылка входа ---------------------------------------------------------------


def issue_login_token(session: Session, telegram_id: int) -> str | None:
    """Ссылка входа для пользователя бота; None — сайт выключен или юзера нет.

    Прежние неиспользованные ссылки гасим: новая просьба почти всегда значит
    «старая потерялась».
    """
    settings = get_settings()
    if not settings.site_url:
        return None
    user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
    if user is None:
        return None
    now = _now()
    session.execute(
        update(SiteLoginToken)
        .where(SiteLoginToken.user_id == user.id, SiteLoginToken.used_at.is_(None))
        .values(used_at=now)
    )
    token = secrets.token_urlsafe(32)
    session.add(
        SiteLoginToken(
            user_id=user.id,
            token_hash=_hash(token),
            expires_at=now + timedelta(minutes=settings.site_login_token_ttl_minutes),
        )
    )
    session.commit()
    logger.info("Вход на сайт: выдана ссылка пользователю %s", telegram_id)
    return token


@dataclass(slots=True)
class Viewer:
    """Кто смотрит сайт. `user` = None у гостя."""

    role: Role
    user: BotUser | None
    session_expires_at: datetime | None = None
    session_id: int | None = None


GUEST = Viewer(role="guest", user=None)


def session_ttl(user: BotUser) -> timedelta:
    """Админу — короткая сессия (доступ к базе с телефона), остальным — месяц."""
    settings = get_settings()
    if user.is_superuser:
        return timedelta(hours=settings.admin_session_ttl_hours)
    return timedelta(days=settings.site_session_ttl_days)


_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("YaBrowser", "Яндекс Браузер"), ("Firefox/", "Firefox"),
             ("Chrome/", "Chrome"), ("Safari/", "Safari"))
_SYSTEMS = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
            ("Mac OS X", "Mac"), ("Linux", "Linux"))


def device_label(user_agent: str | None) -> str | None:
    """«Chrome · Android» из User-Agent — чтобы человек узнал своё устройство в списке."""
    if not user_agent:
        return None
    ua = user_agent[:400]
    if "Telegram" in ua:
        browser = "Telegram"
    else:
        browser = next((name for key, name in _BROWSERS if key in ua), "Браузер")
    system = next((name for key, name in _SYSTEMS if key in ua), None)
    return f"{browser} · {system}" if system else browser


def _trim_sessions(session: Session, user_id: int, keep_id: int) -> int:
    """Оставить не больше `site_max_sessions` самых свежих входов; вернуть, сколько закрыто."""
    limit = max(1, get_settings().site_max_sessions)
    rows = session.scalars(
        select(SiteSession).where(SiteSession.user_id == user_id).order_by(SiteSession.created_at.desc(), SiteSession.id.desc())
    ).all()
    extra = [row for row in rows if row.id != keep_id][limit - 1 :]
    for row in extra:
        session.delete(row)
    return len(extra)


def exchange_login_token(session: Session, token: str, *, user_agent: str | None = None) -> tuple[str, Viewer]:
    if not token:
        raise LoginError("empty token")
    row = session.scalar(select(SiteLoginToken).where(SiteLoginToken.token_hash == _hash(token)))
    if row is None:
        raise LoginError("unknown token")
    now = _now()
    if row.used_at is not None:
        raise LoginError("token already used")
    expires_at = _aware(row.expires_at)
    if expires_at is None or expires_at <= now:
        raise LoginError("token expired")
    claimed = session.execute(
        update(SiteLoginToken)
        .where(SiteLoginToken.id == row.id, SiteLoginToken.used_at.is_(None))
        .values(used_at=now)
    )
    if claimed.rowcount != 1:
        raise LoginError("token claimed concurrently")
    user = session.get(BotUser, row.user_id)
    if user is None:
        raise LoginError("user gone")

    session.execute(
        delete(SiteSession).where(SiteSession.user_id == user.id, SiteSession.expires_at <= now)
    )
    raw = secrets.token_urlsafe(32)
    session_expires_at = now + session_ttl(user)
    new = SiteSession(user_id=user.id, token_hash=_hash(raw), expires_at=session_expires_at, device=device_label(user_agent))
    session.add(new)
    session.flush()
    closed = _trim_sessions(session, user.id, new.id)
    session.commit()
    if closed:
        logger.info("Вход на сайт: лимит устройств — закрыто старых сессий: %s", closed)
    logger.info("Вход на сайт: сессия открыта, telegram_id=%s", user.telegram_id)
    return raw, Viewer(role=role_of(user, now), user=user, session_expires_at=session_expires_at, session_id=new.id)


def resolve_viewer(session: Session, raw: str | None) -> Viewer:
    """Кто за кукой. Любое сомнение — гость (не ошибка: гостю сайт открыт)."""
    if not raw:
        return GUEST
    row = session.scalar(select(SiteSession).where(SiteSession.token_hash == _hash(raw)))
    if row is None:
        return GUEST
    now = _now()
    expires_at = _aware(row.expires_at)
    if expires_at is None or expires_at <= now:
        session.delete(row)
        session.commit()
        return GUEST
    user = session.get(BotUser, row.user_id)
    if user is None:
        return GUEST
    last_seen = _aware(row.last_seen_at)
    if last_seen is None or now - last_seen > LAST_SEEN_REFRESH:
        row.last_seen_at = now
        session.commit()
    return Viewer(role=role_of(user, now), user=user, session_expires_at=expires_at, session_id=row.id)


def list_sessions(session: Session, viewer: Viewer) -> list[dict]:
    """Устройства пользователя: новые сверху, текущее помечено."""
    if viewer.user is None:
        return []
    rows = session.scalars(
        select(SiteSession).where(SiteSession.user_id == viewer.user.id, SiteSession.expires_at > _now())
        .order_by(SiteSession.created_at.desc(), SiteSession.id.desc())
    ).all()
    return [
        {
            "id": row.id,
            "device": row.device,
            "created_at": row.created_at,
            "last_seen_at": row.last_seen_at,
            "current": row.id == viewer.session_id,
        }
        for row in rows
    ]


def revoke_user_session(session: Session, viewer: Viewer, session_id: int) -> bool:
    """Закрыть своё устройство по id; чужое — False (как будто его нет)."""
    if viewer.user is None:
        return False
    deleted = session.execute(
        delete(SiteSession).where(SiteSession.id == session_id, SiteSession.user_id == viewer.user.id)
    )
    session.commit()
    return bool(deleted.rowcount)


def revoke_other_sessions(session: Session, viewer: Viewer) -> int:
    if viewer.user is None:
        return 0
    deleted = session.execute(
        delete(SiteSession).where(SiteSession.user_id == viewer.user.id, SiteSession.id != viewer.session_id)
    )
    session.commit()
    return int(deleted.rowcount or 0)


def revoke_session(session: Session, raw: str | None) -> None:
    if not raw:
        return
    session.execute(delete(SiteSession).where(SiteSession.token_hash == _hash(raw)))
    session.commit()
