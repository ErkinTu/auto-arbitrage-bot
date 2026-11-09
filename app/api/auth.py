"""Вход на сайт: обмен ссылки из бота на сессию, кто я, выход."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.roles import current_viewer
from app.db.session import get_session
from app.schemas import AdminLoginRequest, SiteMe
from app.services.admin_auth import too_many_attempts
from app.services.site_auth import (
    SESSION_COOKIE,
    LoginError,
    Viewer,
    access_of,
    exchange_login_token,
    revoke_session,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _me(viewer: Viewer) -> dict:
    user = viewer.user
    if user is None:
        return {"role": "guest", "user": None, "access": None}
    access = access_of(user)
    return {
        "role": viewer.role,
        "user": {"telegram_id": user.telegram_id, "username": user.username, "first_name": user.first_name},
        "access": {"status": access.status, "active": access.active, "expires_at": access.expires_at},
        "session_expires_at": viewer.session_expires_at,
    }


@router.post("/exchange", response_model=SiteMe)
def exchange(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> dict:
    client = request.client.host if request.client else "unknown"
    if too_many_attempts(client):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Слишком много попыток входа")
    try:
        raw, viewer = exchange_login_token(session, payload.token)
    except LoginError as exc:
        logger.info("Вход на сайт отклонён (%s) с %s", exc, client)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Ссылка недействительна. Отправьте боту /login ещё раз."
        ) from exc
    max_age = max(0, int((viewer.session_expires_at - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=max_age,
        httponly=True,
        secure=True,
        # Lax, не Strict: человек приходит на сайт по ссылкам из Telegram, и
        # Strict показывал бы ему гостевой сайт до первого клика. POST с чужих
        # сайтов Lax всё равно не пропускает — CSRF закрыт.
        samesite="lax",
        path="/",
    )
    return _me(viewer)


@router.get("/me", response_model=SiteMe)
def me(viewer: Viewer = Depends(current_viewer)) -> dict:
    """Роль посетителя. Гость — 200 с role=guest, не 401: гостю сайт открыт."""
    return _me(viewer)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, session: Session = Depends(get_session)) -> None:
    revoke_session(session, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
