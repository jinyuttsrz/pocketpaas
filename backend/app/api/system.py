"""健康检查与平台信息接口。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_user
from app.models.models import User
from app.schemas.app_schema import ResourceCheckIn
from app.services import app_service, demo_service, host_metrics_service, resource_service
from app.services.docker_service import probe

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict:
    """存活探针：不依赖外部组件，供负载均衡 / 容器编排调用。"""
    return {"status": "ok", "app": settings.APP_NAME}


@router.get("/ready")
def ready() -> dict:
    """就绪探针：附带 Docker 运行时连通性，便于本地排障。"""
    docker_status = probe()
    return {
        "status": "ok" if docker_status.available else "degraded",
        "deploy_mode": settings.DEPLOY_MODE,
        "nginx_enabled": settings.ENABLE_NGINX,
        "base_domain": settings.BASE_DOMAIN or None,
        "use_https": settings.USE_HTTPS,
        "docker": {
            "available": docker_status.available,
            "server_version": docker_status.server_version,
            "api_version": docker_status.api_version,
            "containers_running": docker_status.containers_running,
            "images": docker_status.images,
            "error": docker_status.error,
        },
    }


@router.post(
    "/system/resource-check",
    summary="新建向导：对照宿主真实资源校验端口与配额",
)
def resource_check(
    payload: ResourceCheckIn,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    """确认容器端口 / 内存上限 / CPU 核数时调用，返回逐字段校验结果与资源基线。"""
    # 配额调整会传入 app_id 以排除其旧配额，此处校验该应用确属当前用户（管理员可跨用户）
    if payload.app_id is not None:
        try:
            app_service.get_app(db, payload.app_id, user)
        except app_service.AppError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return resource_service.check_resources(
        db,
        payload.container_port,
        payload.mem_limit,
        payload.cpu_quota,
        exclude_app_id=payload.app_id,
    )


@router.get(
    "/system/host-metrics",
    summary="资源与节点：宿主机实时性能监控（CPU 时序 / 负载 / 内存 / 磁盘 / 容器占用）",
)
def host_metrics(user: User | None = Depends(require_user)) -> dict:
    """返回宿主机 CPU 利用率滚动时序、系统负载、内存、磁盘、运行时长与受管容器占用。"""
    return host_metrics_service.snapshot()


@router.get("/system/demos", summary="内置示例代码包列表")
def list_demos(user: User | None = Depends(require_user)) -> list[dict]:
    """供「模板与指南」页面选择内置示例代码包时展示。"""
    return demo_service.list_demos()
