"""Гейт админ-панели: одна зависимость на все её ручки.

Держим её отдельным модулем, чтобы новый роутер нельзя было завести «почти
правильно»: подключение к `admin_router` без `Depends(current_admin)` должно
выглядеть очевидной ошибкой при чтении диффа.
"""

from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.services.admin_auth import SESSION_COOKIE, AdminIdentity, resolve_session
from app.services.site_auth import SESSION_COOKIE as SITE_COOKIE
from app.services.site_auth import resolve_viewer


def current_admin(
    admin_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    site_session: str | None = Cookie(default=None, alias=SITE_COOKIE),
    session: Session = Depends(get_session),
) -> AdminIdentity:
    """Кто вошёл, либо 401. Причину отказа наружу не объясняем.

    Fail-safe по умолчанию: `resolve_session` возвращает None на любое сомнение
    (нет куки, истекла, снят флаг админа, строки нет) — здесь это всегда 401.
    """
    identity = resolve_session(session, admin_session)
    if identity is None and site_session:
        # Сайт — один для всех ролей: админ, вошедший через /login, видит
        # админские ручки той же сессией (роль считается по is_superuser сейчас).
        viewer = resolve_viewer(session, site_session)
        if viewer.role == "admin" and viewer.user is not None:
            identity = AdminIdentity(
                telegram_id=viewer.user.telegram_id,
                username=viewer.user.username,
                first_name=viewer.user.first_name,
                session_expires_at=viewer.session_expires_at,
            )
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется вход",
        )
    return identity
