"""应用编排服务：协调数据库记录与 Docker 容器生命周期。

M2 阶段聚焦"给定镜像 → 分配端口 → 起容器 → 记录状态"这条主链路。
M3 会在此基础上补齐"上传代码 → 构建镜像"的前置环节。
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.models.models import App, BuildLog, Deployment, Route, User
from app.schemas.app_schema import AppCreate, DeployIn
from app.services import (
    builder_service,
    docker_service,
    port_allocator,
    resource_service,
    route_service,
    security_service,
    source_service,
)

logger = logging.getLogger("paas.app_service")


class AppError(RuntimeError):
    """业务异常（映射为 4xx）。"""


class AppNotFound(AppError):
    """应用不存在或当前用户无权访问（映射为 404，不泄露他人应用的存在性）。"""


def _slugify(name: str) -> str:
    """生成合法子域名：仅小写字母、数字与中划线。"""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "app"


def _is_admin(user: User | None) -> bool:
    """管理员可跨用户查看 / 操作全部应用。"""
    return user is not None and user.role == "admin"


def list_apps(db: Session, user: User | None = None) -> list[App]:
    """应用列表：普通用户仅见自己的应用，管理员见全部。

    user 为 None 表示未开启鉴权（单租户），此时返回全部。
    """
    stmt = select(App).options(selectinload(App.owner)).order_by(App.id)
    if user is not None and not _is_admin(user):
        stmt = stmt.where(App.owner_id == user.id)
    return list(db.execute(stmt).scalars().all())


def get_app(db: Session, app_id: int, user: User | None = None) -> App:
    """按 id 取应用，并做属主可见性校验。

    非属主（且非管理员）访问时同样抛「应用不存在」，避免泄露他人应用的存在性。
    """
    stmt = select(App).options(selectinload(App.owner)).where(App.id == app_id)
    if user is not None and not _is_admin(user):
        stmt = stmt.where(App.owner_id == user.id)
    app = db.execute(stmt).scalar_one_or_none()
    if app is None:
        raise AppNotFound(f"应用不存在：id={app_id}")
    return app


def create_app(db: Session, payload: AppCreate, owner: User | None = None) -> App:
    """创建应用记录（此时尚未起容器，状态为 created）。

    多租户：应用归属 owner；应用名仅在 owner 范围内唯一（跨用户可重名），
    子域名因对外寻址仍需全局唯一，冲突时拒绝。
    """
    # M7：应用名白名单校验（字符集 / 长度 / 保留字），防注入容器名与 Nginx 配置
    try:
        security_service.validate_app_name(payload.name)
    except security_service.SecurityError as exc:
        raise AppError(str(exc)) from exc
    subdomain = _slugify(payload.name)
    dup = select(App).where(App.name == payload.name)
    if owner is not None:
        dup = dup.where(App.owner_id == owner.id)
    if db.execute(dup).scalar_one_or_none():
        raise AppError(f"应用名已存在：{payload.name}")
    if db.execute(select(App).where(App.subdomain == subdomain)).scalar_one_or_none():
        raise AppError(f"子域名已被占用：{subdomain}")

    app = App(
        name=payload.name,
        owner_id=owner.id if owner is not None else None,
        subdomain=subdomain,
        source_type=payload.source_type,
        git_url=payload.git_url,
        image=payload.image,
        container_port=payload.container_port,
        mem_limit=payload.mem_limit,
        cpu_quota=payload.cpu_quota,
        env=json.dumps(payload.env, ensure_ascii=False) if payload.env else None,
        command=payload.command,
        status="created",
    )
    db.add(app)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(f"应用创建失败（唯一约束冲突）：{payload.name}") from exc
    db.refresh(app)
    return app


def _rollback_failed_deploy(db: Session, app: App, container_name: str) -> None:
    """部署失败回滚：清理残留容器 + 摘除可能已写入的路由，状态置 failed。

    保证失败后不残留"半启动"的容器与悬空路由，下次部署可从干净状态重来。
    """
    try:
        docker_service.remove(container_name, force=True)
    except docker_service.ContainerNotFoundError:
        pass
    except docker_service.DockerUnavailableError:
        pass
    if settings.ENABLE_NGINX:
        try:
            route_service.remove_route(app.id)
        except route_service.RouteError:
            pass
    app.status = "failed"
    db.commit()


def deploy_app(
    db: Session, app_id: int, payload: DeployIn | None = None, user: User | None = None
) -> App:
    """部署应用：分配端口 → 启动容器 → 落库部署记录。

    M2 阶段要求 app.image 已存在（直接指定镜像）；
    M3 接入镜像构建后，此处会先构建再启动。
    """
    app = get_app(db, app_id, user)

    # 重新部署（reassign_port=True）时允许对运行中的应用直接重建：
    # 旧容器会在下方被强制移除，宿主端口也将重新分配。
    reassign_port = bool(payload and payload.reassign_port)
    if app.status == "running" and not reassign_port:
        raise AppError(f"应用已在运行中：{app.name}，如需变更请先 stop")

    image = (payload.image if payload and payload.image else None) or app.image
    if not image:
        raise AppError("未指定镜像：请先上传源码并构建，或提供 image 字段")

    # 容器配置：优先用本次请求的覆盖值，否则沿用应用上持久化的配置
    env = (payload.env if payload else None) or (app.env_map or None)
    command = (payload.command if payload else None) or app.command

    # 分配宿主机端口（M8：原子预留，进程内锁 + 立即落库，消除并发竞态）
    host_port = port_allocator.reserve_for_app(db, app, force_new=reassign_port)

    # 若存在同名残留容器（如失败重试），先清理
    container_name = docker_service.container_name_for(app.id, app.name)
    try:
        docker_service.remove(container_name, force=True)
    except docker_service.ContainerNotFoundError:
        pass

    app.status = "building"
    db.commit()

    # M7/M8：端口竞态重试——预留后仍可能被宿主其它进程抢占，
    # 最多换端口重试 MAX_PORT_RETRY 次，全部失败才判部署失败。
    last_conflict: Exception | None = None
    info = None
    for attempt in range(settings.MAX_PORT_RETRY + 1):
        try:
            info = docker_service.run_container(
                app_id=app.id,
                app_name=app.name,
                image=image,
                container_port=app.container_port,
                host_port=host_port,
                mem_limit=app.mem_limit,
                cpu_quota=app.cpu_quota,
                env=env,
                command=command,
            )
            break
        except docker_service.PortConflictError as exc:
            last_conflict = exc
            try:
                docker_service.remove(container_name, force=True)
            except (docker_service.ContainerNotFoundError, docker_service.DockerUnavailableError):
                pass
            if attempt >= settings.MAX_PORT_RETRY:
                break
            # 换一个端口重新预留后再试
            host_port = port_allocator.reserve_for_app(db, app, force_new=True)
        except docker_service.DockerUnavailableError as exc:
            # M7：失败回滚——清理可能残留的容器，状态置 failed，不留半启动态
            _rollback_failed_deploy(db, app, container_name)
            raise AppError(str(exc)) from exc

    if info is None:
        _rollback_failed_deploy(db, app, container_name)
        raise AppError(f"端口分配失败（已重试 {settings.MAX_PORT_RETRY} 次）：{last_conflict}")

    app.image = image
    app.host_port = host_port
    app.status = "running"

    deployment = Deployment(
        app_id=app.id,
        image_tag=image,
        build_status="success",
        container_id=info.id,
        container_name=info.name,
        port=host_port,
        mem_limit=app.mem_limit,
        cpu_quota=app.cpu_quota,
    )
    db.add(deployment)
    db.commit()
    db.refresh(app)

    # M4：部署成功后同步动态路由（Nginx server 块渲染 → 校验 → 热载 → 落库）
    # M7：容器已成功起停并落库，路由同步失败不应让整个部署回滚/500——
    #     数据面（容器）已就绪，仅控制面路由未更新，降级为告警，保留 running。
    try:
        sync_route(db, app)
    except AppError as exc:
        logger.warning("路由同步失败（容器已运行，可稍后在详情页重试部署）：%s", exc)
    return app


def sync_route(db: Session, app: App) -> Route | None:
    """部署后同步路由：ENABLE_NGINX 关闭时不做任何事（本地端口直连模式）。

    重建部署导致 host_port 变化时，conf 会被覆盖更新，路由始终指向最新 upstream。
    """
    if not settings.ENABLE_NGINX or not app.host_port:
        return None
    domain = route_service.domain_for(app.subdomain)
    try:
        ssl = route_service.apply_route(
            app_id=app.id, domain=domain, upstream_port=app.host_port
        )
    except route_service.RouteError as exc:
        raise AppError(f"路由配置失败：{exc}") from exc
    except OSError as exc:
        # 写 conf 无权限 / 磁盘等系统级错误：归一为业务异常，交由上层降级处理
        raise AppError(f"路由配置写入失败：{exc}") from exc

    route = db.execute(select(Route).where(Route.app_id == app.id)).scalar_one_or_none()
    if route is None:
        route = Route(app_id=app.id, domain=domain, upstream_port=app.host_port)
        db.add(route)
    route.domain = domain
    route.upstream_port = app.host_port
    route.ssl_enabled = ssl
    db.commit()
    db.refresh(route)
    return route


def get_route(db: Session, app_id: int, user: User | None = None) -> Route | None:
    """查询应用当前路由（未部署或未启用 Nginx 时为 None）。"""
    app = get_app(db, app_id, user)
    if not settings.ENABLE_NGINX:
        return None
    return db.execute(select(Route).where(Route.app_id == app.id)).scalar_one_or_none()


def image_tag_for(app: App) -> str:
    """构建产物镜像标签：{IMAGE_PREFIX}/{subdomain}:latest。"""
    return f"{settings.IMAGE_PREFIX}/{app.subdomain}:latest"


def ingest_source_zip(db: Session, app_id: int, zip_path, user: User | None = None) -> App:
    """接收上传的 zip 代码包，解压到工作区并识别项目类型。"""
    app = get_app(db, app_id, user)
    try:
        src = source_service.ingest_zip(app.id, zip_path)
    finally:
        source_service.cleanup_zip(zip_path)

    app.source_type = "upload"
    app.source_path = str(src)
    db.commit()
    db.refresh(app)
    return app


def ingest_source_git(db: Session, app_id: int, git_url: str, user: User | None = None) -> App:
    """克隆 Git 仓库到工作区。"""
    app = get_app(db, app_id, user)
    # M7：SSRF 防护——拒绝指向本机 / 内网 / 云元数据的仓库地址
    try:
        security_service.validate_git_url(git_url)
    except security_service.SecurityError as exc:
        raise AppError(str(exc)) from exc
    src = source_service.ingest_git(app.id, git_url)
    app.source_type = "git"
    app.git_url = git_url
    app.source_path = str(src)
    db.commit()
    db.refresh(app)
    return app


def build_app(
    db: Session, app_id: int, container_port: int | None = None, user: User | None = None
) -> tuple[App, Deployment, str | None]:
    """构建应用镜像：识别项目类型 → 生成 Dockerfile → docker build。

    构建日志逐行落库到 build_logs，构建产物标签记录在 app.image。

    容器端口优先由上传包自动识别（paas.json → Dockerfile EXPOSE → 类型默认），
    识别成功时返回第三个元素作为提示文案，供接口层回传给前端弹窗提示。

    返回 (app, deployment, port_notice)，便于接口层回传构建摘要。
    """
    from pathlib import Path

    app = get_app(db, app_id, user)
    if not app.source_path:
        raise AppError("尚未上传源码，请先调用 /apps/{id}/source 上传代码包或配置 Git")

    src = Path(app.source_path)
    if not src.exists():
        raise AppError(f"源码目录不存在（可能已被清理）：{app.source_path}")

    # 1) 识别项目类型
    try:
        profile = builder_service.detect(src)
    except (ValueError, FileNotFoundError) as exc:
        raise AppError(str(exc)) from exc

    # 2) 自动读取上传包推断容器端口（paas.json → Dockerfile EXPOSE → 类型默认），
    #    优先于用户填写值，避免"填的端口"与"应用真实监听端口"不一致导致页面打不开
    resolution = builder_service.resolve_container_port(src, profile)
    if resolution:
        port = resolution.port
        reason = builder_service.PORT_SOURCE_LABELS[resolution.source]
        port_notice = f"已自动识别容器端口 {resolution.port}（依据：{reason}）"
        if resolution.port != app.container_port:
            port_notice += f"，原填写的 {app.container_port} 已被覆盖"
    else:
        port = container_port or app.container_port
        port_notice = None

    # M7：用户自带 Dockerfile 危险指令扫描（特权 / 挂载宿主敏感路径 / docker.sock 等）
    try:
        security_service.scan_dockerfile(src)
    except security_service.SecurityError as exc:
        raise AppError(str(exc)) from exc

    # 3) 生成 Dockerfile（custom 时沿用用户自带）
    if not profile.has_dockerfile:
        content = builder_service.generate(src, profile, port)
        builder_service.write_dockerfile(src, content)

    image_tag = image_tag_for(app)

    # 4) 建部署记录（先落 pending，再逐步更新状态）
    app.status = "building"
    app.project_type = profile.project_type
    app.container_port = port
    deployment = Deployment(
        app_id=app.id,
        image_tag=image_tag,
        build_status="building",
        mem_limit=app.mem_limit,
        cpu_quota=app.cpu_quota,
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)

    # 4) 执行构建，日志逐行落库
    log_buffer: list[str] = []

    def _on_log(line: str) -> None:
        log_buffer.append(line)

    result = docker_service.build_image(
        context_dir=str(src), image_tag=image_tag, on_log=_on_log
    )

    # 端口自动识别的提示写入构建日志首行，便于在控制台追溯端口来源
    log_lines = ([f"⚙ {port_notice}"] if port_notice else []) + log_buffer
    db.add_all(
        [BuildLog(deployment_id=deployment.id, log_line=line) for line in log_lines]
    )

    if not result.success:
        deployment.build_status = "failed"
        db.add(BuildLog(deployment_id=deployment.id, log_line=f"[BUILD FAILED] {result.error}"))
        app.status = "failed"
        db.commit()
        db.refresh(deployment)
        raise AppError(f"镜像构建失败：{result.error}")

    # 5) 构建成功
    deployment.build_status = "success"
    app.image = image_tag
    app.status = "created"  # 构建完成但未起容器，等待 deploy
    db.commit()
    db.refresh(app)
    db.refresh(deployment)
    return app, deployment, port_notice


def build_logs(db: Session, deployment_id: int, limit: int = 500) -> list[BuildLog]:
    """查询某次部署的构建日志。"""
    stmt = (
        select(BuildLog)
        .where(BuildLog.deployment_id == deployment_id)
        .order_by(BuildLog.id)
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def latest_deployment(db: Session, app_id: int) -> Deployment | None:
    """取应用最近一次部署记录（含构建与起容器两类记录）。"""
    stmt = (
        select(Deployment)
        .where(Deployment.app_id == app_id)
        .order_by(Deployment.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def latest_build_deployment(db: Session, app_id: int) -> Deployment | None:
    """取最近一次「有构建日志」的部署记录。

    build 与 deploy 会各写一条 deployments，取 id 最大者会命中起容器记录
    （无构建日志），故这里按是否存在 build_logs 过滤。
    """
    stmt = (
        select(Deployment)
        .where(
            Deployment.app_id == app_id,
            Deployment.id.in_(select(BuildLog.deployment_id).distinct()),
        )
        .order_by(Deployment.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def stop_app(db: Session, app_id: int, user: User | None = None) -> App:
    app = get_app(db, app_id, user)
    if not app.host_port:
        raise AppError("应用尚未部署，无需停止")
    try:
        docker_service.stop(docker_service.container_name_for(app.id, app.name))
    except docker_service.ContainerNotFoundError:
        pass
    app.status = "stopped"
    db.commit()
    db.refresh(app)
    return app


def restart_app(db: Session, app_id: int, user: User | None = None) -> App:
    app = get_app(db, app_id, user)
    if not app.host_port:
        raise AppError("应用尚未部署，无法重启")
    try:
        docker_service.restart(docker_service.container_name_for(app.id, app.name))
    except docker_service.ContainerNotFoundError:
        raise AppError("容器不存在，请重新部署") from None
    app.status = "running"
    db.commit()
    db.refresh(app)
    return app


def update_quota(
    db: Session, app_id: int, mem_limit: str, cpu_quota: float, user: User | None = None
) -> App:
    """调整配额：运行中在线生效（cgroup 更新），否则仅更新记录。

    调整前先对照宿主真实资源校验（内存格式/上限、CPU 核数），
    校验不通过（error 级）直接拒绝，避免分配超出宿主能力的配额。
    """
    app = get_app(db, app_id, user)
    check = resource_service.check_resources(
        db, None, mem_limit, cpu_quota, exclude_app_id=app.id
    )
    for field in ("mem", "cpu"):
        if check[field]["level"] == "error":
            raise AppError(check[field]["message"])
    if app.status == "running":
        try:
            docker_service.update_resources(
                docker_service.container_name_for(app.id, app.name), mem_limit, cpu_quota
            )
        except docker_service.ContainerNotFoundError:
            pass
    app.mem_limit = mem_limit
    app.cpu_quota = cpu_quota
    db.commit()
    db.refresh(app)
    return app


def update_app_config(
    db: Session,
    app_id: int,
    env: dict[str, str] | None,
    command: str | None,
    user: User | None = None,
) -> App:
    """更新应用持久化容器配置（环境变量 / 启动命令）。

    仅写库：运行中的容器要下次重新部署才会带上新配置，
    避免"改了配置但容器没变"的认知偏差。
    """
    app = get_app(db, app_id, user)
    _validate_env(env)
    app.env = json.dumps(env, ensure_ascii=False) if env else None
    app.command = (command or "").strip() or None
    db.commit()
    db.refresh(app)
    return app


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_MAX_ITEMS = 50
_ENV_MAX_VALUE = 1024


def _validate_env(env: dict[str, str] | None) -> None:
    """环境变量校验：变量名合法、值长度受限、条目数受限。"""
    if not env:
        return
    if len(env) > _ENV_MAX_ITEMS:
        raise AppError(f"环境变量最多 {_ENV_MAX_ITEMS} 项")
    for key, value in env.items():
        if not _ENV_KEY_RE.match(str(key)):
            raise AppError(f"环境变量名不合法：{key}（仅允许字母/数字/下划线，且不以数字开头）")
        if len(str(value)) > _ENV_MAX_VALUE:
            raise AppError(f"环境变量 {key} 的值过长（上限 {_ENV_MAX_VALUE} 字符）")


def delete_app(db: Session, app_id: int, user: User | None = None) -> None:
    """销毁应用：清理路由 → 停容器 → 删容器 → 删记录。"""
    app = get_app(db, app_id, user)
    # M4：先摘除 Nginx 路由，避免出现指向已销毁应用的悬空域名
    if settings.ENABLE_NGINX:
        try:
            route_service.remove_route(app.id)
        except route_service.RouteError as exc:
            raise AppError(f"路由清理失败，已中止销毁：{exc}") from exc
    try:
        docker_service.remove(docker_service.container_name_for(app.id, app.name), force=True)
    except docker_service.ContainerNotFoundError:
        pass
    db.delete(app)
    db.commit()


def app_runtime(db: Session, app_id: int, user: User | None = None) -> dict:
    """查询应用对应的容器实时状态。"""
    app = get_app(db, app_id, user)
    try:
        info = docker_service.get(docker_service.container_name_for(app.id, app.name))
    except docker_service.ContainerNotFoundError:
        return {"app_id": app.id, "container": None, "stats": None}
    return {
        "app_id": app.id,
        "container": info,
        "stats": docker_service.stats(info.name),
    }
