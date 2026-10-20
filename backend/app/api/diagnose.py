"""一键诊断接口：对单个应用做端到端体检，返回结构化检查清单。"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import client_ip, require_user
from app.models.models import User
from app.schemas.diagnose_schema import DiagnoseOut
from app.services import app_service, audit_service, diagnose_service
from app.services.app_service import AppError

router = APIRouter(prefix="/apps", tags=["diagnose"])


@router.get("/{app_id}/diagnose", response_model=DiagnoseOut, summary="一键诊断")
def diagnose_app(
    app_id: int,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    """逐项探测应用状态 / 源码 / 镜像 / 容器 / 端口 / 路由 / 部署 / 配额。

    只读探测，不改变任何状态；单项异常不影响其余检查。
    """
    try:
        result = diagnose_service.diagnose_app(db, app_id, user)
    except AppError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    audit_service.record(
        db,
        user=user,
        action="app.diagnose",
        target_type="app",
        target_id=result["app_id"],
        target_name=result["app_name"],
        detail={"level": result["level"], "failed": result["summary"]["failed"]},
        client_ip=client_ip(request),
    )
    db.commit()
    return result
