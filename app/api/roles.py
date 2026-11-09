"""Роли сайта как зависимости FastAPI.

Гейт объявляется на РОУТЕРЕ (`dependencies=[Depends(require_subscriber)]`), а не
на ручке — тогда новую ручку нельзя завести незащищённой (тот же приём, что у
`admin/deps.py`). Отказ: 401 — не вошёл, 403 — вошёл, но роли мало. Фронт по
коду понимает, что показать: «войдите через бота» или «нужна подписка».
"""

from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.services.site_auth import SESSION_COOKIE, Viewer, resolve_viewer, role_at_least


def current_viewer(
    sk_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    session: Session = Depends(get_session),
) -> Viewer:
    """Кто смотрит. Никогда не падает: без сессии — гость."""
    return resolve_viewer(session, sk_session)


def _require(minimum: str):
    def dependency(viewer: Viewer = Depends(current_viewer)) -> Viewer:
        if role_at_least(viewer.role, minimum):
            return viewer
        if viewer.role == "guest":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Войдите через Telegram-бота")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Доступно с подпиской")

    dependency.__name__ = f"require_{minimum}"
    return dependency


require_user = _require("user")
require_subscriber = _require("subscriber")
require_admin = _require("admin")
