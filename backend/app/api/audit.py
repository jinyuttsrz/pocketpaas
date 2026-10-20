"""操作审计日志接口。

- `GET /api/audit/logs`    查询审计记录（普通用户仅见自己的操作，管理员见全量）
- `GET /api/audit/actions` 可筛选的动作字典（供前端下拉）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_user
from app.models.models import AuditLog, User
from app.schemas.audit_schema import ACTION_LABELS, AuditActionOut, AuditLogOut

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/actions", response_model=list[AuditActionOut], summary="审计动作字典")
def list_actions(user: User | None = Depends(require_user)) -> list[AuditActionOut]:
    return [AuditActionOut(action=k, label=v) for k, v in ACTION_LABELS.items()]


@router.get("/logs", response_model=list[AuditLogOut], summary="审计日志（多租户过滤）")
def list_logs(
    action: str | None = Query(None, description="按动作筛选，如 app.deploy"),
    target_type: str | None = Query(None, description="按对象类型筛选：app / user / session"),
    username: str | None = Query(None, description="按操作人用户名模糊匹配（管理员）"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> list[AuditLog]:
    """返回审计记录。

    多租户隔离：非管理员被强制限定为"自己的操作"，无法通过参数越权查看他人记录。
    """
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    is_admin = bool(user and user.role == "admin")
    if user is not None and not is_admin:
        stmt = stmt.where(AuditLog.user_id == user.id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if username and (is_admin or user is None):
        stmt = stmt.where(AuditLog.username.like(f"%{username}%"))
    stmt = stmt.limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all())
