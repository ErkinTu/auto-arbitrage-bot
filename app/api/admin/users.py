"""Пользователи бота для панели. Только чтение."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.admin.deps import current_admin
from app.db.session import get_session
from app.schemas import AdminUserDetail, AdminUserPage
from app.services.admin_users import MAX_LIMIT, get_user, list_users

# Гейт объявлен на роутере, а не на каждой ручке: так новую ручку физически
# нельзя добавить незащищённой — про Depends просто негде забыть.
router = APIRouter(
    prefix="/api/v1/admin/users",
    tags=["admin"],
    dependencies=[Depends(current_admin)],
)


@router.get("", response_model=AdminUserPage)
def users_page(
    status_filter: str | None = Query(default=None, alias="status"),
    query: str | None = Query(default=None, alias="q", max_length=64),
    limit: int = Query(default=20, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> dict:
    return list_users(
        session,
        status=status_filter,
        query=query,
        limit=limit,
        offset=offset,
    )


@router.get("/{user_id}", response_model=AdminUserDetail)
def user_detail(user_id: int, session: Session = Depends(get_session)) -> dict:
    try:
        return get_user(session, user_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
