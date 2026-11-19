"""Вход на сайт кнопкой «Войти через Telegram» (план — `docs/planning/TELEGRAM_LOGIN.md`).

Поток:
1. Сайт: `start_request` → ссылка `t.me/<бот>?start=login_<nonce>`, код на экран
   (2 цифры) и `poll`-токен (остаётся в браузере).
2. Бот: `/start login_<nonce>` → `claim` (запрос закрепляется за этим telegram_id)
   → человек выбирает код из трёх → `answer` (верный — `confirmed`, неверный или
   «Не я» — `denied`).
3. Сайт опрашивает `poll`: при `confirmed` запрос гасится (`consumed`) и
   открывается сессия — cookie ставится тому браузеру, который начал вход.

Почему так, а не ссылкой из бота (`/login`): ссылка открывается в браузере, который
выберет Telegram, а у сайта на экране «Домой» на iPhone свои cookie — туда по
ссылке не войти вовсе. Почему код: без него злоумышленник мог бы начать вход у
себя и прислать жертве ссылку «подтверди» — жертва отдала бы ему свой аккаунт.

Роль и подписка в сессии не хранятся: `site_auth.role_of` считает их по боту на
каждом запросе. Секреты (nonce, poll) — только sha256 в базе.
"""

from __future__ import annotations

import hashlib
import logging
import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import BotUser, SiteLoginRequest
from app.services.site_auth import Viewer, device_label, open_site_session

logger = logging.getLogger(__name__)

DEEP_LINK_PREFIX = "login_"
# Сколько храним отработанные запросы (для разбора), потом удаляем при новом входе.
KEEP_DAYS = 2


class LoginUnavailable(Exception):
    """Вход кнопкой выключен: нет адреса сайта или имени бота."""


@dataclass(slots=True)
class StartedLogin:
    bot_url: str
    code: str
    poll_token: str
    expires_at: datetime


@dataclass(slots=True)
class BotPrompt:
    """Что показать в боте: устройство, время и три кода (один верный)."""

    request_id: int
    device: str | None
    created_at: datetime
    options: list[str]


ClaimResult = BotPrompt | Literal["unknown", "expired", "used"]
AnswerResult = Literal["confirmed", "denied", "expired", "used"]
PollStatus = Literal["pending", "confirmed", "denied", "expired"]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _expired(row: SiteLoginRequest, now: datetime) -> bool:
    return _aware(row.expires_at) <= now


def start_request(session: Session, *, user_agent: str | None) -> StartedLogin:
    settings = get_settings()
    if not settings.site_url or not settings.bot_username:
        raise LoginUnavailable
    now = _now()
    session.execute(delete(SiteLoginRequest).where(SiteLoginRequest.expires_at < now - timedelta(days=KEEP_DAYS)))
    # Параметр start у Telegram: до 64 символов [A-Za-z0-9_-]; token_urlsafe(15) → 20.
    nonce = secrets.token_urlsafe(15)
    poll_token = secrets.token_urlsafe(32)
    code = str(secrets.randbelow(90) + 10)
    expires_at = now + timedelta(minutes=settings.site_tg_login_ttl_minutes)
    session.add(
        SiteLoginRequest(
            nonce_hash=_hash(nonce),
            poll_hash=_hash(poll_token),
            code=code,
            device=device_label(user_agent),
            expires_at=expires_at,
        )
    )
    session.commit()
    bot = settings.bot_username.lstrip("@")
    return StartedLogin(
        bot_url=f"https://t.me/{bot}?start={DEEP_LINK_PREFIX}{nonce}",
        code=code,
        poll_token=poll_token,
        expires_at=expires_at,
    )


def parse_nonce(payload: str | None) -> str | None:
    """`login_<nonce>` из аргумента /start; иначе None."""
    if not payload or not payload.startswith(DEEP_LINK_PREFIX):
        return None
    return payload[len(DEEP_LINK_PREFIX) :].strip() or None


def _decoys(code: str) -> list[str]:
    """Три кода в случайном порядке: верный и два других двузначных."""
    pool = [str(n) for n in range(10, 100) if str(n) != code]
    options = [code, *random.sample(pool, 2)]
    random.shuffle(options)
    return options


def claim(session: Session, nonce: str, telegram_id: int) -> ClaimResult:
    """Бот открыл ссылку входа: закрепить запрос за этим человеком и вернуть подсказку."""
    row = session.scalar(select(SiteLoginRequest).where(SiteLoginRequest.nonce_hash == _hash(nonce)))
    if row is None:
        return "unknown"
    if _expired(row, _now()):
        return "expired"
    if row.status != "pending" or (row.claimed_telegram_id not in (None, telegram_id)):
        return "used"
    row.claimed_telegram_id = telegram_id
    session.commit()
    return BotPrompt(request_id=row.id, device=row.device, created_at=_aware(row.created_at), options=_decoys(row.code))


def answer(session: Session, request_id: int, telegram_id: int, choice: str) -> AnswerResult:
    """Человек нажал код или «Не я». Неверный код — отказ (новый вход — с сайта)."""
    row = session.get(SiteLoginRequest, request_id)
    if row is None or row.claimed_telegram_id != telegram_id:
        return "used"
    now = _now()
    if _expired(row, now):
        return "expired"
    if row.status != "pending":
        return "used"
    user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
    ok = choice == row.code and user is not None
    claimed = session.execute(
        update(SiteLoginRequest)
        .where(SiteLoginRequest.id == row.id, SiteLoginRequest.status == "pending")
        .values(
            status="confirmed" if ok else "denied",
            user_id=user.id if ok and user else None,
            confirmed_at=now if ok else None,
        )
    )
    session.commit()
    if claimed.rowcount != 1:
        return "used"
    logger.info("Вход кнопкой: %s, telegram_id=%s", "подтверждён" if ok else "отклонён", telegram_id)
    return "confirmed" if ok else "denied"


def poll(session: Session, poll_token: str, *, user_agent: str | None) -> tuple[PollStatus, tuple[str, Viewer] | None]:
    """Опрос сайта. `confirmed` — ровно один раз, вместе с новой сессией."""
    row = session.scalar(select(SiteLoginRequest).where(SiteLoginRequest.poll_hash == _hash(poll_token)))
    if row is None:
        return "expired", None
    if row.status == "denied":
        return "denied", None
    if row.status == "pending":
        return ("expired", None) if _expired(row, _now()) else ("pending", None)
    if row.status != "confirmed":
        return "expired", None  # consumed: сессию уже забрал другой опрос
    # Гасим условно: два одновременных опроса не получат две сессии.
    taken = session.execute(
        update(SiteLoginRequest)
        .where(SiteLoginRequest.id == row.id, SiteLoginRequest.status == "confirmed")
        .values(status="consumed")
    )
    if taken.rowcount != 1:
        session.rollback()
        return "expired", None
    user = session.get(BotUser, row.user_id) if row.user_id else None
    if user is None:
        session.commit()
        return "expired", None
    return "confirmed", open_site_session(session, user, user_agent=user_agent, method="button")
