"""应用管理接口：CRUD + 生命周期 + 配额调整 + 源码上传与镜像构建。

对应实施计划第五节的 REST 草案（M2 / M3 部分）。
"""

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import client_ip, require_user
from app.models.models import User
from app.schemas.app_schema import (
    AppConfigUpdate,
    AppCreate,
    AppOut,
    AppQuotaUpdate,
    BuildIn,
    BuildLogOut,
    BuildResultOut,
    DemoSourceIn,
    DeployIn,
    GitSourceIn,
    MetricOut,
    RouteOut,
    ServiceOut,
)
from app.services import app_service, audit_service, demo_service, metrics_service, watchdog_service
from app.services.app_service import AppError, AppNotFound
from app.services.docker_service import ContainerInfo, ContainerNotFoundError
from app.services.source_service import SourceError
from app.services.file_service import (
    FileError,
    FileNotFound as FileMissingError,
    FileTargetError,
)

router = APIRouter(prefix="/apps", tags=["apps"])


def _log_action(
    db: Session,
    request: Request,
    user: User | None,
    action: str,
    app,
    detail: dict | None = None,
) -> None:
    """补记一条应用操作审计（业务事务已提交，这里再提交一次）。"""
    audit_service.record(
        db,
        user=user,
        action=action,
        target_type="app",
        target_id=app.id,
        target_name=app.name,
        detail=detail,
        client_ip=client_ip(request),
    )
    db.commit()


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, AppNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, AppError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, SourceError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, ContainerNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    # 项目文件管理：目标缺失 404、目标不可用 409、其余文件错误 400
    if isinstance(exc, FileMissingError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, FileTargetError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, FileError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.get("", response_model=list[AppOut], summary="应用列表")
def list_apps(user: User | None = Depends(require_user), db: Session = Depends(get_db)) -> list:
    """普通用户仅返回自己的应用；管理员返回全部。"""
    return app_service.list_apps(db, user)


@router.post("", response_model=AppOut, status_code=status.HTTP_201_CREATED, summary="创建应用")
def create_app(
    payload: AppCreate,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        app = app_service.create_app(db, payload, user)
    except AppError as exc:
        raise _handle(exc) from exc
    _log_action(db, request, user, "app.create", app, detail={"name": app.name})
    return app


@router.get("/{app_id}", response_model=AppOut, summary="应用详情")
def get_app(app_id: int, user: User | None = Depends(require_user), db: Session = Depends(get_db)):
    try:
        return app_service.get_app(db, app_id, user)
    except AppError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{app_id}/deploy", response_model=AppOut, summary="部署（起容器）")
def deploy_app(
    app_id: int,
    request: Request,
    payload: DeployIn | None = None,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        app = app_service.deploy_app(db, app_id, payload, user)
    except AppError as exc:
        raise _handle(exc) from exc
    _log_action(db, request, user, "app.deploy", app, detail={"host_port": app.host_port})
    return app


@router.post("/{app_id}/stop", response_model=AppOut, summary="停止容器")
def stop_app(
    app_id: int,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        app = app_service.stop_app(db, app_id, user)
    except AppError as exc:
        raise _handle(exc) from exc
    _log_action(db, request, user, "app.stop", app)
    return app


@router.post("/{app_id}/restart", response_model=AppOut, summary="重启容器")
def restart_app(
    app_id: int,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        app = app_service.restart_app(db, app_id, user)
    except AppError as exc:
        raise _handle(exc) from exc
    _log_action(db, request, user, "app.restart", app)
    return app


@router.patch("/{app_id}/quota", response_model=AppOut, summary="调整 CPU/内存配额")
def update_quota(
    app_id: int,
    payload: AppQuotaUpdate,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        app = app_service.update_quota(db, app_id, payload.mem_limit, payload.cpu_quota, user)
    except AppError as exc:
        raise _handle(exc) from exc
    _log_action(
        db,
        request,
        user,
        "app.quota",
        app,
        detail={"mem_limit": app.mem_limit, "cpu_quota": app.cpu_quota},
    )
    return app


@router.patch("/{app_id}/config", response_model=AppOut, summary="更新环境变量 / 启动命令")
def update_config(
    app_id: int,
    payload: AppConfigUpdate,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    """持久化容器配置（下次重新部署生效）。"""
    try:
        app = app_service.update_app_config(db, app_id, payload.env, payload.command, user)
    except AppError as exc:
        raise _handle(exc) from exc
    _log_action(
        db,
        request,
        user,
        "app.config",
        app,
        detail={"env_keys": sorted(app.env_map.keys()), "command": app.command},
    )
    return app


@router.delete("/{app_id}", status_code=status.HTTP_204_NO_CONTENT, summary="销毁应用")
def delete_app(
    app_id: int,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> None:
    try:
        app = app_service.get_app(db, app_id, user)
        name = app.name  # 删除后记录即消失，先取快照
        app_service.delete_app(db, app_id, user)
    except AppError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    audit_service.record(
        db,
        user=user,
        action="app.delete",
        target_type="app",
        target_id=app_id,
        target_name=name,
        client_ip=client_ip(request),
    )
    db.commit()


@router.get(
    "/{app_id}/services",
    response_model=list[ServiceOut],
    summary="应用内服务列表（多容器 compose）",
)
def list_app_services(
    app_id: int, user: User | None = Depends(require_user), db: Session = Depends(get_db)
) -> list:
    try:
        app = app_service.get_app(db, app_id, user)
    except AppError as exc:
        raise _handle(exc) from exc
    return app.services


@router.post(
    "/{app_id}/rollback",
    response_model=AppOut,
    summary="回滚到上一个蓝绿版本",
)
def rollback_app(
    app_id: int,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        app = app_service.rollback_app(db, app_id, user)
    except AppError as exc:
        raise _handle(exc) from exc
    _log_action(
        db,
        request,
        user,
        "app.rollback",
        app,
        detail={"active_slot": app.active_slot, "previous_slot": app.previous_slot},
    )
    return app


@router.post(
    "/{app_id}/services/{service_name}/restart",
    response_model=AppOut,
    summary="重启应用内的单个服务",
)
def restart_app_service(
    app_id: int,
    service_name: str,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        app = app_service.restart_service(db, app_id, service_name, user)
    except AppError as exc:
        raise _handle(exc) from exc
    _log_action(
        db, request, user, "app.service.restart", app, detail={"service": service_name}
    )
    return app


@router.get("/{app_id}/runtime", summary="容器实时状态与资源占用")
def app_runtime(
    app_id: int,
    service: str | None = None,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = app_service.app_runtime(db, app_id, user, service=service)
    except AppError as exc:
        raise _handle(exc) from exc
    container = result["container"]
    result["container"] = ContainerInfo(**vars(container)) if container else None
    return result


@router.get("/{app_id}/readiness", summary="HTTP 就绪探测状态")
def app_readiness(
    app_id: int, user: User | None = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    """返回最近一次 HTTP 就绪探测结论（只读，不触发实际探测）。

    后台巡检线程每 `HEALTH_CHECK_INTERVAL` 秒探活一次运行中的容器；应用未运行时无探测意义。
    """
    try:
        app = app_service.get_app(db, app_id, user)
    except AppError as exc:
        raise _handle(exc) from exc

    state = watchdog_service.readiness_state(app.id)
    if (app.status or "") != "running":
        state["state"] = "unknown"
        state["detail"] = "应用未运行，暂无就绪探测"
    state["app_name"] = app.name
    return state


# ---------------------------------------------------------------------------
# M3：源码上传与镜像构建
# ---------------------------------------------------------------------------


@router.post("/{app_id}/source", response_model=AppOut, summary="上传源码 zip 代码包")
async def upload_source(
    app_id: int,
    request: Request,
    file: UploadFile = File(..., description="zip 代码包"),
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 代码包")

    # 落盘到临时文件，限制体积后交给 source_service 解压
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    tmp_dir = Path(tempfile.gettempdir())
    tmp_path = tmp_dir / f"paas_upload_{app_id}_{file.filename}"

    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"上传体积超过上限 {settings.MAX_UPLOAD_MB}MB",
                    )
                out.write(chunk)
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    try:
        app = app_service.ingest_source_zip(db, app_id, tmp_path, user)
    except AppError as exc:
        raise _handle(exc) from exc
    except SourceError as exc:
        raise _handle(exc) from exc
    _log_action(db, request, user, "app.source", app, detail={"source_type": "upload"})
    return app


@router.post("/{app_id}/source/git", response_model=AppOut, summary="以 Git 仓库作为源码")
def upload_source_git(
    app_id: int,
    payload: GitSourceIn,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        app = app_service.ingest_source_git(db, app_id, payload.git_url, user)
    except (AppError, SourceError) as exc:
        raise _handle(exc) from exc
    _log_action(db, request, user, "app.source", app, detail={"source_type": "git", "git_url": payload.git_url})
    return app


@router.post("/{app_id}/source/demo", response_model=AppOut, summary="以平台内置示例包作为源码")
def upload_source_demo(
    app_id: int,
    payload: DemoSourceIn,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        demo_path = demo_service.stage_demo(payload.demo)
    except demo_service.DemoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        app = app_service.ingest_source_zip(db, app_id, demo_path, user)
    except (AppError, SourceError) as exc:
        raise _handle(exc) from exc
    _log_action(db, request, user, "app.source", app, detail={"source_type": "demo", "demo": payload.demo})
    return app


@router.post(
    "/{app_id}/build",
    response_model=BuildResultOut,
    summary="构建镜像（支持单服务 compose，自动识别端口）",
)
def build_app(
    app_id: int,
    request: Request,
    payload: BuildIn | None = None,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    port = payload.container_port if payload else None
    try:
        bot_app, deployment, port_notice = app_service.build_app(db, app_id, port, user)
        result = BuildResultOut.model_validate(deployment)
        result.port_notice = port_notice
    except AppError as exc:
        raise _handle(exc) from exc
    except SourceError as exc:
        raise _handle(exc) from exc
    _log_action(db, request, user, "app.build", bot_app, detail={"image": deployment.image_tag})
    return result


@router.get(
    "/{app_id}/build/logs",
    response_model=list[BuildLogOut],
    summary="查询最近一次构建日志",
)
def get_build_logs(
    app_id: int, user: User | None = Depends(require_user), db: Session = Depends(get_db)
):
    try:
        app_service.get_app(db, app_id, user)
    except AppError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    deployment = app_service.latest_build_deployment(db, app_id)
    if deployment is None:
        return []
    return app_service.build_logs(db, deployment.id)


@router.get("/{app_id}/route", response_model=RouteOut, summary="查询应用路由（域名 → 容器端口）")
def get_app_route(
    app_id: int, user: User | None = Depends(require_user), db: Session = Depends(get_db)
):
    try:
        route = app_service.get_route(db, app_id, user)
    except AppError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if route is None:
        raise HTTPException(
            status_code=404,
            detail="路由不存在：应用尚未部署，或未启用 Nginx（ENABLE_NGINX=false）",
        )
    return route


@router.get(
    "/{app_id}/routes",
    response_model=list[RouteOut],
    summary="查询应用全部公开服务路由",
)
def list_app_routes(
    app_id: int, user: User | None = Depends(require_user), db: Session = Depends(get_db)
):
    try:
        return app_service.list_routes(db, app_id, user)
    except AppError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/{app_id}/metrics",
    response_model=list[MetricOut],
    summary="查询应用资源曲线（最近 N 条采样，按时间升序）",
)
def get_app_metrics(
    app_id: int,
    limit: int = 120,
    service: str | None = None,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        app_service.get_app(db, app_id, user)
    except AppError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return metrics_service.list_metrics(db, app_id, service=service, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
