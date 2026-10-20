"""用户管理接口（仅管理员）。

- `GET    /api/users`             用户列表（管理员全量）
- `POST   /api/users`             新建用户（可指定角色）
- `PATCH  /api/users/{id}`        修改显示名 / 角色 / 启用状态
- `PUT    /api/users/{id}/password` 重置口令
- `DELETE /api/users/{id}`        删除用户

管理员可跨用户管理全部账号与应用；普通用户访问一律 403。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import client_ip, require_admin
from app.models.models import User
from app.schemas.auth_schema import (
    AdminUserCreateIn,
    AdminUserResetPasswordIn,
    AdminUserUpdateIn,
    MessageOut,
    UserOut,
)
from app.services import audit_service, auth_service
from app.services.auth_service import AuthError

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_admin)])


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _log_user_action(
    db: Session,
    request: Request,
    operator: User | None,
    action: str,
    target_id: int,
    target_name: str | None,
) -> None:
    """补记一条用户管理审计。"""
    audit_service.record(
        db,
        user=operator,
        action=action,
        target_type="user",
        target_id=target_id,
        target_name=target_name,
        client_ip=client_ip(request),
    )
    db.commit()


@router.get("", response_model=list[UserOut], summary="用户列表（管理员）")
def list_users(db: Session = Depends(get_db)) -> list:
    return auth_service.list_users(db)


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED, summary="新建用户")
def create_user(
    payload: AdminUserCreateIn,
    request: Request,
    operator: User | None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        user = auth_service.create_user(
            db,
            payload.username,
            payload.password,
            display_name=payload.display_name,
            role=payload.role,
        )
    except AuthError as exc:
        raise _bad_request(exc) from exc
    _log_user_action(db, request, operator, "user.create", user.id, user.username)
    return user


@router.patch("/{user_id}", response_model=UserOut, summary="修改用户（显示名 / 角色 / 启用状态）")
def update_user(
    user_id: int,
    payload: AdminUserUpdateIn,
    request: Request,
    operator: User | None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        user = auth_service.update_user(
            db,
            user_id,
            display_name=payload.display_name,
            role=payload.role,
            is_active=payload.is_active,
        )
    except AuthError as exc:
        raise _bad_request(exc) from exc
    _log_user_action(db, request, operator, "user.update", user.id, user.username)
    return user


@router.put(
    "/{user_id}/password",
    response_model=MessageOut,
    summary="重置用户口令",
)
def reset_password(
    user_id: int,
    payload: AdminUserResetPasswordIn,
    request: Request,
    operator: User | None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        auth_service.reset_password(db, user_id, payload.password)
    except AuthError as exc:
        raise _bad_request(exc) from exc
    _log_user_action(db, request, operator, "user.reset_password", user_id, None)
    return MessageOut(message="口令已重置，该用户需重新登录")


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="删除用户",
)
def delete_user(
    user_id: int,
    request: Request,
    operator: User | None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> None:
    try:
        auth_service.delete_user(db, user_id, operator_id=operator.id if operator else None)
    except AuthError as exc:
        raise _bad_request(exc) from exc
    _log_user_action(db, request, operator, "user.delete", user_id, None)
