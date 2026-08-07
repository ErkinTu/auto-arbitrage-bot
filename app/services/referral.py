"""Рефералка: `/invite` → deep-link → +N дней за каждые M приглашённых друзей.

Анти-абьюз (иначе механика превращается в бесплатный доступ навсегда):

- **Друг засчитывается только после активации триала**, а не по клику по ссылке:
  клик ничего не стоит, а триал — одноразовый и привязан к telegram_id.
- **Привязка одноразовая и только для новичка**: `referrer_id` ставится, пока
  юзер ещё не активировал триал и реферера у него нет. Перепривязать себя к
  другому пригласившему нельзя.
- **Самоприглашение запрещено** явной проверкой.
- **Начисление идемпотентно**: храним ЧИСЛО выданных бонусов и сравниваем с
  числом заслуженных (`друзей // M`), а не «выдали/не выдали». Поэтому повторный
  вызов после гонки или ретрая ничего не удваивает.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BotUser
from app.services.events import EVENT_REFERRAL_BONUS, log_event
from app.services.subscription import extend_paid_access

INVITE_CODE_LENGTH = 7
# Без 0/o/1/l/i: код диктуют голосом и переписывают руками.
_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_MAX_CODE_ATTEMPTS = 12
DEEP_LINK_PREFIX = "ref_"


@dataclass(slots=True)
class ReferralStats:
    invite_code: str
    invited_total: int  # перешли по ссылке
    qualified: int  # из них активировали триал — только они идут в зачёт
    bonuses_granted: int
    friends_to_next_bonus: int


def parse_deep_link(payload: str | None) -> str | None:
    """Достаёт код из аргумента /start (`ref_ab12cd`); иначе None."""
    if not payload:
        return None
    payload = payload.strip()
    if not payload.startswith(DEEP_LINK_PREFIX):
        return None
    code = payload[len(DEEP_LINK_PREFIX) :].strip().lower()
    return code or None


def ensure_invite_code(session: Session, user: BotUser) -> str:
    """Возвращает код пользователя, выдавая его лениво при первом обращении.

    Лениво — чтобы не бэкфиллить существующую базу и не тратить коды на тех, кто
    рефералкой не пользуется.
    """
    if user.invite_code:
        return user.invite_code
    for _ in range(_MAX_CODE_ATTEMPTS):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))
        taken = session.scalar(select(BotUser.id).where(BotUser.invite_code == code))
        if taken is None:
            user.invite_code = code
            return code
    raise RuntimeError("failed to allocate a unique invite code")


def attach_referrer(session: Session, user: BotUser, code: str) -> BotUser | None:
    """Привязывает пригласившего к новичку. Возвращает реферера или None.

    None — если код чужой/несуществующий, это сам пользователь, реферер уже есть
    или юзер уже не новичок (активировал триал).
    """
    if user.referrer_id is not None or user.trial_started_at is not None:
        return None
    referrer = session.scalar(select(BotUser).where(BotUser.invite_code == code))
    if referrer is None or referrer.id == user.id:
        return None
    user.referrer_id = referrer.id
    return referrer


def _qualified_count(session: Session, referrer_id: int) -> int:
    """Друзья, дошедшие до активации триала."""
    return (
        session.scalar(
            select(func.count())
            .select_from(BotUser)
            .where(BotUser.referrer_id == referrer_id, BotUser.trial_started_at.is_not(None))
        )
        or 0
    )


def grant_due_bonuses(
    session: Session,
    referrer: BotUser,
    *,
    friends_per_bonus: int,
    bonus_days: int,
    now: datetime | None = None,
) -> int:
    """Догоняет начисления до заслуженного уровня. Возвращает число НОВЫХ бонусов.

    Вызывается сразу после активации триала приглашённым, поэтому обязателен
    flush: сессии бота живут с `autoflush=False`, и без него COUNT не увидел бы
    только что проставленный `trial_started_at` — бонус молча не начислялся бы
    ровно на том друге, который его и заработал.
    """
    if friends_per_bonus <= 0 or bonus_days <= 0:
        return 0
    session.flush()
    earned = _qualified_count(session, referrer.id) // friends_per_bonus
    pending = earned - (referrer.referral_bonuses_granted or 0)
    if pending <= 0:
        return 0
    referrer.referral_bonuses_granted = earned
    days = pending * bonus_days
    extend_paid_access(referrer, days, now)
    log_event(session, referrer.id, EVENT_REFERRAL_BONUS, {"bonuses": pending, "days": days})
    return pending


def referral_stats(
    session: Session, user: BotUser, *, friends_per_bonus: int
) -> ReferralStats:
    invited_total = (
        session.scalar(
            select(func.count()).select_from(BotUser).where(BotUser.referrer_id == user.id)
        )
        or 0
    )
    qualified = _qualified_count(session, user.id)
    remainder = qualified % friends_per_bonus if friends_per_bonus > 0 else 0
    return ReferralStats(
        invite_code=ensure_invite_code(session, user),
        invited_total=invited_total,
        qualified=qualified,
        bonuses_granted=user.referral_bonuses_granted or 0,
        friends_to_next_bonus=max(friends_per_bonus - remainder, 0) if friends_per_bonus > 0 else 0,
    )
