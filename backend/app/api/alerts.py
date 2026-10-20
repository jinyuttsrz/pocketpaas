"""告警中心接口。

- `GET  /api/alerts`            告警列表（宿主级仅管理员可见，应用级按属主过滤）
- `POST /api/alerts/{id}/ack`   手动确认告警
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_user
from app.models.models import Alert, App, User
from app.schemas.alert_schema import AlertOut
from app.services import alert_service

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut], summary="告警列表")
def list_alerts(
    status_filter: str | None = Query(None, alias="status", description="active / acked / resolved"),
    level: str | None = Query(None, description="warning / critical"),
    limit: int = Query(100, ge=1, le=500),
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> list[Alert]:
    """返回告警记录。

    多租户隔离：宿主级告警（target_type=host）仅管理员可见；
    应用级告警只返回属于当前用户的应用，管理员可见全部。
    """
    stmt = select(Alert).order_by(Alert.id.desc())
    is_admin = bool(user and user.role == "admin")
    if user is not None and not is_admin:
        owned_ids = select(App.id).where(App.owner_id == user.id)
        stmt = stmt.where(
            (Alert.target_type == "app") & Alert.target_id.in_(owned_ids)
        )
    if status_filter:
        stmt = stmt.where(Alert.status == status_filter)
    if level:
        stmt = stmt.where(Alert.level == level)
    stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars().all())


@router.post("/{alert_id}/ack", response_model=AlertOut, summary="确认告警")
def ack_alert(
    alert_id: int,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="告警不存在")
    if user is not None and user.role != "admin":
        # 普通用户只能确认自己应用的告警
        app = db.get(App, alert.target_id) if alert.target_type == "app" else None
        if app is None or app.owner_id != user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作该告警")
    result = alert_service.ack(db, alert_id, operator=user.username if user else "anonymous")
    if result is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该告警已消除，无需确认")
    return result
