"""Вход, кто-я и выход. Единственные ручки админки, доступные без сессии."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.admin.deps import current_admin
from app.core.config import get_settings
from app.db.session import get_session
from app.schemas import AdminLoginRequest, AdminMe
from app.services.admin_auth import (
    SESSION_COOKIE,
    AdminIdentity,
    LoginError,
    exchange_login_token,
    revoke_session,
    too_many_attempts,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/auth", tags=["admin"])


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/exchange", response_model=AdminMe)
def exchange(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> AdminIdentity:
    """Меняет одноразовый токен из ссылки на сессию в httpOnly-куке."""
    if too_many_attempts(_client_key(request)):
        logger.warning("Админ-вход: слишком много попыток с %s", _client_key(request))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много попыток входа. Попробуйте через несколько минут.",
        )

    try:
        raw, identity = exchange_login_token(session, payload.token)
    except LoginError as exc:
        # Причина — только в лог: снаружи «истекла» и «не существует» должны быть
        # неразличимы, иначе перебор получает подсказку.
        logger.info("Админ-вход отклонён (%s) с %s", exc, _client_key(request))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ссылка недействительна. Отправьте боту /admin ещё раз.",
        ) from exc

    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=settings.admin_session_ttl_hours * 3600,
        httponly=True,
        # Secure не отключаем даже для локальной разработки: браузеры считают
        # http://localhost защищённым контекстом, так что dev не ломается, а
        # прод не рискует уехать без флага.
        secure=True,
        samesite="strict",
        path="/",
    )
    return identity


@router.get("/me", response_model=AdminMe)
def me(identity: AdminIdentity = Depends(current_admin)) -> AdminIdentity:
    """Кто вошёл. На ней же стоит guard фронта: 401 — уводим на /login."""
    return identity


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> None:
    """Выход. Без Depends(current_admin) намеренно: выйти можно всегда.

    Требовать живую сессию, чтобы её погасить, — значит оставить протухшую куку
    висеть в браузере, а на просроченной сессии вернуть 401 вместо выхода.
    """
    revoke_session(session, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
