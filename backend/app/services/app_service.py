"""应用编排服务：协调数据库记录与 Docker 容器生命周期。

M2 阶段聚焦"给定镜像 → 分配端口 → 起容器 → 记录状态"这条主链路。
M3 会在此基础上补齐"上传代码 → 构建镜像"的前置环节。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.models.models import App, AppService, BuildLog, Deployment, Metric, Route, User
from app.schemas.app_schema import AppCreate, DeployIn
from app.services import (
    builder_service,
    compose_engine,
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


DEFAULT_SERVICE_NAME = "default"


def default_container_name(app: App) -> str:
    """单容器应用沿用的容器名（与历史数据、指标 / 巡检保持一致）。"""
    return docker_service.container_name_for(app.id, app.name)


def ensure_default_service(db: Session, app: App, *, commit: bool = True) -> AppService:
    """确保单容器应用存在 name="default" 的服务记录。"""
    service = db.execute(
        select(AppService).where(
            AppService.app_id == app.id, AppService.name == DEFAULT_SERVICE_NAME
        )
    ).scalar_one_or_none()
    if service is not None:
        return service

    service = AppService(
        app_id=app.id,
        name=DEFAULT_SERVICE_NAME,
        image=app.image,
        container_name=default_container_name(app),
        container_port=app.container_port,
        host_port=app.host_port,
        mem_limit=app.mem_limit,
        cpu_quota=app.cpu_quota,
        status=app.status,
        is_public=True,
    )
    db.add(service)
    if commit:
        db.commit()
        db.refresh(service)
    else:
        db.flush()
    return service


def backfill_app_services(db: Session) -> int:
    """启动时为缺少服务记录的历史应用补一条 default 服务。"""
    apps = db.execute(select(App)).scalars().all()
    created = 0
    for app in apps:
        exists = db.execute(
            select(AppService.id).where(AppService.app_id == app.id).limit(1)
        ).scalar_one_or_none()
        if exists is not None:
            continue
        db.add(
            AppService(
                app_id=app.id,
                name=DEFAULT_SERVICE_NAME,
                image=app.image,
                container_name=default_container_name(app),
                container_port=app.container_port,
                host_port=app.host_port,
                mem_limit=app.mem_limit,
                cpu_quota=app.cpu_quota,
                status=app.status,
                is_public=True,
            )
        )
        created += 1
    if created:
        db.commit()
    return created


def sync_default_service(db: Session, app: App, **fields) -> AppService:
    """把单容器应用的运行信息同步到默认服务记录（不额外提交事务）。"""
    service = ensure_default_service(db, app, commit=False)
    for key, value in fields.items():
        if hasattr(service, key):
            setattr(service, key, value)
    return service


def resolve_service_container(
    app: App, service_name: str | None = None
) -> tuple[AppService | None, str]:
    """解析服务记录与其容器名。

    未指定服务时取公开服务（单容器应用即 default）；没有服务记录的历史应用
    回退到旧容器名，保证兼容。
    """
    services = list(app.services)
    if not services:
        return None, default_container_name(app)

    if service_name:
        target = next((item for item in services if item.name == service_name), None)
        if target is None:
            raise AppError(f"服务不存在：{service_name}")
    else:
        target = next((item for item in services if item.is_public), None) or services[0]

    if target.name == DEFAULT_SERVICE_NAME or target.is_public:
        ref = target.container_name or default_container_name(app)
    else:
        ref = target.container_name or docker_service.container_name_for(
            app.id, f"{app.name}-{target.name}"
        )
    return target, ref


def restart_service(
    db: Session, app_id: int, service_name: str, user: User | None = None
) -> App:
    """重启应用内的单个服务容器（多容器 compose 的排障入口）。"""
    app = get_app(db, app_id, user)
    service, ref = resolve_service_container(app, service_name)
    if service is None:
        raise AppError("应用尚无服务记录，请先部署")
    try:
        docker_service.restart(ref)
    except docker_service.ContainerNotFoundError:
        raise AppError(f"服务 {service.name} 的容器不存在，请重新部署") from None
    service.status = "running"
    if service.is_public:
        app.status = "running"
    db.commit()
    db.refresh(app)
    return app


def list_apps(db: Session, user: User | None = None) -> list[App]:
    """应用列表：普通用户仅见自己的应用，管理员见全部。

    user 为 None 表示未开启鉴权（单租户），此时返回全部。
    """
    stmt = select(App).options(
        selectinload(App.owner), selectinload(App.services)
    ).order_by(App.id)
    if user is not None and not _is_admin(user):
        stmt = stmt.where(App.owner_id == user.id)
    return list(db.execute(stmt).scalars().all())


def get_app(db: Session, app_id: int, user: User | None = None) -> App:
    """按 id 取应用，并做属主可见性校验。

    非属主（且非管理员）访问时同样抛「应用不存在」，避免泄露他人应用的存在性。
    """
    stmt = select(App).options(
        selectinload(App.owner), selectinload(App.services)
    ).where(App.id == app_id)
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
    ensure_default_service(db, app)
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

    # 多服务 compose：重新解析源码，走独立编排链路
    if app.project_type == "compose" and app.source_path:
        from pathlib import Path

        src = Path(app.source_path)
        if src.exists():
            try:
                profile = builder_service.detect(src)
            except (ValueError, FileNotFoundError) as exc:
                raise AppError(str(exc)) from exc
            if profile.compose is not None and profile.compose.is_multi:
                return _deploy_compose_multi(db, app, profile.compose, payload)

    if _is_multi_app(app) and (
        not app.source_path or not Path(app.source_path).exists()
    ):
        raise AppError("多服务应用的源码目录缺失，请重新上传或拉取源码后再部署")

    # 从多服务切回单容器：清理旧的 compose 项目，避免残留容器
    if _is_multi_app(app):
        compose_engine.remove_plan(app, app.services, remove_volumes=False)
        _prune_services(db, app, {DEFAULT_SERVICE_NAME})
        db.commit()

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
    sync_default_service(
        db,
        app,
        image=image,
        container_id=info.id,
        container_name=info.name,
        container_port=app.container_port,
        host_port=host_port,
        mem_limit=app.mem_limit,
        cpu_quota=app.cpu_quota,
        status="running",
        is_public=True,
    )

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


def _public_primary(app: App) -> AppService | None:
    """主入口服务：宿主端口与应用 host_port 一致的那个公开服务。"""
    publics = [service for service in app.services if service.is_public]
    if not publics:
        return None
    return next(
        (service for service in publics if app.host_port and service.host_port == app.host_port),
        publics[0],
    )


def _service_domain(app: App, service: AppService, primary: AppService | None) -> str:
    if service is primary:
        return route_service.domain_for(app.subdomain)
    return route_service.domain_for_service(app.subdomain, service.name)


def sync_routes(db: Session, app: App) -> list[Route]:
    """部署后同步全部公开服务路由：一个 conf 内渲染多个域名。"""
    if not settings.ENABLE_NGINX:
        return []
    primary = _public_primary(app)
    if primary is None:
        return []
    publics = sorted(
        [service for service in app.services if service.is_public and service.host_port],
        key=lambda service: 0 if service is primary else 1,
    )
    if not publics:
        return []

    entries = [
        (service, _service_domain(app, service, primary), int(service.host_port))
        for service in publics
    ]
    domains = [domain for _service, domain, _port in entries]
    if len(set(domains)) != len(domains):
        raise AppError(f"公开服务域名冲突：{', '.join(domains)}")

    other_subdomains = set(
        db.execute(select(App.subdomain).where(App.id != app.id)).scalars()
    )
    other_domains = set(
        db.execute(select(Route.domain).where(Route.app_id != app.id)).scalars()
    )
    for domain in domains:
        if domain.split(".")[0] in other_subdomains or domain in other_domains:
            raise AppError(f"域名冲突：{domain} 已被其他应用占用")

    try:
        ssl = route_service.apply_routes(
            app_id=app.id, routes=[(domain, port) for _s, domain, port in entries]
        )
    except route_service.RouteError as exc:
        raise AppError(f"路由配置失败：{exc}") from exc
    except OSError as exc:
        raise AppError(f"路由配置写入失败：{exc}") from exc

    db.execute(delete(Route).where(Route.app_id == app.id))
    created: list[Route] = []
    for service, domain, port in entries:
        route = Route(
            app_id=app.id,
            domain=domain,
            upstream_port=port,
            service_name=service.name,
            ssl_enabled=ssl,
        )
        db.add(route)
        created.append(route)
    db.commit()
    for route in created:
        db.refresh(route)
    return created


def sync_route(db: Session, app: App) -> Route | None:
    """兼容入口：返回主入口路由。"""
    routes = sync_routes(db, app)
    return routes[0] if routes else None


def get_route(db: Session, app_id: int, user: User | None = None) -> Route | None:
    """查询应用主入口路由（未部署或未启用 Nginx 时为 None）。"""
    routes = list_routes(db, app_id, user)
    if not routes:
        return None
    app = get_app(db, app_id, user)
    domain = route_service.domain_for(app.subdomain)
    return next((route for route in routes if route.domain == domain), routes[0])


def list_routes(db: Session, app_id: int, user: User | None = None) -> list[Route]:
    """查询应用的全部公开路由（多公开服务时有多条）。"""
    app = get_app(db, app_id, user)
    if not settings.ENABLE_NGINX:
        return []
    return list(
        db.execute(
            select(Route).where(Route.app_id == app.id).order_by(Route.id)
        ).scalars().all()
    )


def image_tag_for(app: App) -> str:
    """构建产物镜像标签：{IMAGE_PREFIX}/{subdomain}:latest。"""
    return f"{settings.IMAGE_PREFIX}/{app.subdomain}:latest"


def _build_compose_app(
    db: Session,
    app: App,
    src,
    profile,
    port_override: int | None = None,
) -> tuple[App, Deployment, str | None]:
    """按单服务 compose 计划构建（或直接采用 image），返回构建结果。

    与普通项目的差异：
    - 端口、环境变量、启动命令、配额可由 compose 提供；
    - 若服务只声明 image（无 build），构建阶段只拉取镜像，不再执行 docker build；
    - 若声明 build，则以 compose 的 context / dockerfile 为构建入口。
    """
    plan = profile.compose
    if plan is None:
        raise AppError("compose 解析结果缺失")

    notices = [f"按单服务 compose 解析：服务 {plan.service_name}", *plan.notices]

    # 1) 容器端口：compose ports/expose > 请求覆盖值 > 应用已存值
    port = plan.container_port or port_override or app.container_port
    if plan.container_port and plan.container_port != app.container_port:
        notices.append(
            f"容器端口按 compose 设为 {plan.container_port}（原填写 {app.container_port}）"
        )

    # 2) 资源配额：compose 声明优先，但仍按宿主真实资源校验
    mem_limit = plan.mem_limit or app.mem_limit
    cpu_quota = plan.cpu_quota if plan.cpu_quota is not None else app.cpu_quota
    if plan.mem_limit or plan.cpu_quota is not None:
        check = resource_service.check_resources(
            db, port, mem_limit, cpu_quota, exclude_app_id=app.id
        )
        for field in ("mem", "cpu"):
            if check[field]["level"] == "error":
                raise AppError(check[field]["message"])
        if mem_limit != app.mem_limit or cpu_quota != app.cpu_quota:
            notices.append(f"资源配额按 compose 设为 {mem_limit} / {cpu_quota:g} 核")
    app.mem_limit = mem_limit
    app.cpu_quota = cpu_quota

    # 3) 环境变量与启动命令：compose 为准，控制台补充额外变量
    merged_env = dict(app.env_map)
    if plan.environment:
        overlap = sorted(set(plan.environment) & set(merged_env))
        merged_env.update(plan.environment)
        if overlap:
            notices.append(f"compose 覆盖了控制台同名环境变量：{', '.join(overlap)}")
    _validate_env(merged_env)
    app.env = json.dumps(merged_env, ensure_ascii=False) if merged_env else None
    if plan.command:
        if app.command and app.command != plan.command:
            notices.append("启动命令沿用控制台配置，已忽略 compose.command")
        elif not app.command:
            app.command = plan.command

    # 4) 构建记录
    image_tag = image_tag_for(app) if plan.needs_build else (plan.image or image_tag_for(app))
    app.status = "building"
    app.project_type = "compose"
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

    log_buffer: list[str] = [f"🧩 {notice}" for notice in notices]

    def _on_log(line: str) -> None:
        log_buffer.append(line)

    try:
        if plan.needs_build:
            context = plan.build_context
            if context is None:
                raise AppError("compose 构建上下文缺失")
            dockerfile = plan.build_dockerfile
            existing_df = context / (dockerfile or "Dockerfile")
            regenerate = dockerfile is None or (
                dockerfile == "Dockerfile"
                and builder_service.is_platform_generated(existing_df)
            )
            if regenerate:
                sub_profile = builder_service.detect(context, check_compose=False)
                content = builder_service.generate(context, sub_profile, port)
                builder_service.write_dockerfile(context, content)
                dockerfile = "Dockerfile"
                log_buffer.append(
                    f"compose 未提供可复用的 Dockerfile，已按 {sub_profile.project_type} "
                    f"类型生成（监听端口 {port}）"
                )
            else:
                # 用户自带 Dockerfile：与普通 custom 项目执行同一套危险指令扫描
                security_service.scan_dockerfile(context, dockerfile)
                log_buffer.append(f"使用 compose 构建上下文：{context} / {dockerfile}")
            if plan.image:
                log_buffer.append(f"compose 同时声明 image={plan.image}，构建产物使用平台标签")
            result = docker_service.build_image(
                context_dir=str(context),
                image_tag=image_tag,
                dockerfile=dockerfile,
                on_log=_on_log,
            )
            if not result.success:
                deployment.build_status = "failed"
                app.status = "failed"
                db.add_all(
                    [BuildLog(deployment_id=deployment.id, log_line=line) for line in log_buffer]
                )
                db.add(
                    BuildLog(
                        deployment_id=deployment.id,
                        log_line=f"[BUILD FAILED] {result.error}",
                    )
                )
                db.commit()
                db.refresh(deployment)
                raise AppError(f"镜像构建失败：{result.error}")
        else:
            image = plan.image
            if not image:
                raise AppError("compose 既未声明 build，也未声明 image")
            log_buffer.append(f"使用 compose 指定镜像：{image}")
            docker_service.pull_image(image)
            app.image = image

    except (
        docker_service.DockerUnavailableError,
        security_service.SecurityError,
        ValueError,
        FileNotFoundError,
        OSError,
    ) as exc:
        deployment.build_status = "failed"
        app.status = "failed"
        log_buffer.append(f"[BUILD FAILED] {exc}")
        db.add_all(
            [BuildLog(deployment_id=deployment.id, log_line=line) for line in log_buffer]
        )
        db.commit()
        db.refresh(deployment)
        raise AppError(str(exc)) from exc

    deployment.build_status = "success"
    if plan.needs_build:
        app.image = image_tag
    app.status = "created"  # 构建完成但未起容器，等待 deploy
    sync_default_service(
        db,
        app,
        image=app.image,
        container_port=port,
        mem_limit=app.mem_limit,
        cpu_quota=app.cpu_quota,
        status="created",
        is_public=True,
        container_name=default_container_name(app),
    )
    db.add_all(
        [BuildLog(deployment_id=deployment.id, log_line=line) for line in log_buffer]
    )
    db.commit()
    db.refresh(app)
    db.refresh(deployment)

    notice = "；".join(notices) if notices else None
    return app, deployment, notice


def _effective_limits(app: App, spec) -> tuple[str, float]:
    mem = spec.mem_limit or (
        app.mem_limit if spec.is_public else settings.DEFAULT_MEM_LIMIT
    )
    cpu = (
        spec.cpu_quota
        if spec.cpu_quota is not None
        else (app.cpu_quota if spec.is_public else settings.DEFAULT_CPU_QUOTA)
    )
    return mem, cpu


def _prune_services(db: Session, app: App, keep: set[str]) -> None:
    for service in list(app.services):
        if service.name not in keep:
            # 历史库的 metrics 表没有外键级联，显式清理被移除服务的采样
            db.execute(delete(Metric).where(Metric.service_id == service.id))
            db.delete(service)
    db.flush()


def _build_compose_multi(
    db: Session,
    app: App,
    plan,
    port_override: int | None = None,
) -> tuple[App, Deployment, str | None]:
    """多服务 compose 构建：逐服务构建 / 拉取镜像并生成服务记录。"""
    publics = plan.publics
    if not publics:
        raise AppError("多服务 compose 缺少公开服务（需要 ports 或 x-paas.public）")
    public = plan.public or publics[0]

    notices = [f"按多服务 compose 解析：{len(plan.services)} 个服务", *plan.notices]
    public_port = public.container_port or port_override or app.container_port
    if public.container_port and public.container_port != app.container_port:
        notices.append(
            f"主入口服务容器端口按 compose 设为 {public.container_port}"
            f"（原填写 {app.container_port}）"
        )
    if public.container_port is None:
        notices.append("主入口服务未声明 ports / expose，沿用新建应用时填写的容器端口")

    # 资源校验：逐服务校验 + 全项目汇总不超过宿主
    host = resource_service.host_resources()
    total_mem = 0
    total_cpu = 0.0
    for spec in plan.services:
        mem, cpu = _effective_limits(app, spec)
        if not spec.mem_limit:
            notices.append(f"服务 {spec.name} 未声明 mem_limit，使用 {mem}")
        if spec.cpu_quota is None:
            notices.append(f"服务 {spec.name} 未声明 cpus，使用 {cpu:g} 核")
        check = resource_service.check_resources(
            db, None, mem, cpu, exclude_app_id=app.id
        )
        for field in ("mem", "cpu"):
            if check[field]["level"] == "error":
                raise AppError(f"服务 {spec.name}：{check[field]['message']}")
        total_mem += resource_service.parse_mem_limit(mem) or 0
        total_cpu += cpu
    if host["mem_total"] and total_mem > host["mem_total"]:
        raise AppError("compose 中所有服务的内存上限之和超过宿主物理内存")
    if host["cpu_total"] and total_cpu > host["cpu_total"]:
        raise AppError("compose 中所有服务的 CPU 配额之和超过宿主核心数")

    image_tag = compose_engine.service_image_tag(app, public)
    public_mem, public_cpu = _effective_limits(app, public)
    app.mem_limit = public_mem
    app.cpu_quota = public_cpu
    app.status = "building"
    app.project_type = "compose"
    app.container_port = public_port
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

    log_buffer: list[str] = [f"🧩 {notice}" for notice in notices]

    try:
        images = compose_engine.build_images(app, plan, on_log=log_buffer.append)
    except (
        compose_engine.ComposeEngineError,
        docker_service.DockerUnavailableError,
        security_service.SecurityError,
        ValueError,
        OSError,
    ) as exc:
        deployment.build_status = "failed"
        app.status = "failed"
        log_buffer.append(f"[BUILD FAILED] {exc}")
        db.add_all(
            [BuildLog(deployment_id=deployment.id, log_line=line) for line in log_buffer]
        )
        db.commit()
        db.refresh(deployment)
        raise AppError(str(exc)) from exc

    keep = {spec.name for spec in plan.services}
    _prune_services(db, app, keep)

    existing = {service.name: service for service in app.services}
    public_image = images.get(public.name) or public.image or image_tag
    for spec in plan.services:
        mem, cpu = _effective_limits(app, spec)
        service = existing.get(spec.name)
        if service is None:
            service = AppService(app_id=app.id, name=spec.name)
            db.add(service)
        service.image = images.get(spec.name) or spec.image
        service.container_port = (
            spec.container_port or (public_port if spec.is_primary else None)
        )
        if not service.container_name:
            service.container_name = compose_engine.service_container_name(app, spec)
        service.mem_limit = mem
        service.cpu_quota = cpu
        service.status = "created"
        service.is_public = spec.is_public

    deployment.build_status = "success"
    app.status = "created"
    app.image = public_image
    app.container_port = public_port
    db.add_all(
        [BuildLog(deployment_id=deployment.id, log_line=line) for line in log_buffer]
    )
    db.commit()
    db.refresh(app)
    db.refresh(deployment)
    return app, deployment, "；".join(notices)


def _is_multi_app(app: App) -> bool:
    return app.project_type == "compose" and len(app.services) > 1


def _live_service_status(service: AppService) -> str:
    """读取服务容器的真实状态，用于蓝绿失败时恢复（构建会先把 DB 状态改成 created）。"""
    if not service.container_name:
        return service.status or "created"
    try:
        state = docker_service.get(service.container_name).status
    except (docker_service.ContainerNotFoundError, docker_service.DockerUnavailableError):
        return service.status or "created"
    if state == "running":
        return "running"
    if state == "created":
        return "created"
    return "stopped"


def _deploy_compose_multi(
    db: Session,
    app: App,
    plan,
    payload: DeployIn | None = None,
) -> App:
    """部署多服务 compose：多公开服务分配端口，蓝绿槽位切换并支持回滚。"""
    publics = plan.publics
    if not publics:
        raise AppError("多服务 compose 缺少公开服务")
    primary = plan.public or publics[0]

    rows = {service.name: service for service in app.services}
    missing = [spec.name for spec in plan.services if spec.name not in rows]
    if missing:
        raise AppError(f"服务记录缺失：{', '.join(missing)}，请先重新构建")

    images: dict[str, str] = {}
    for spec in plan.services:
        image = rows[spec.name].image or spec.image
        if not image:
            raise AppError(f"服务 {spec.name} 尚未构建镜像，请先构建")
        images[spec.name] = image

    primary_port = primary.container_port or app.container_port
    if not primary_port:
        raise AppError("主入口服务未声明容器端口，无法部署")

    reassign = bool(payload and payload.reassign_port)
    had_active = any(row.container_id and row.container_name for row in app.services)
    old_statuses = {
        row.name: _live_service_status(row) if had_active else (row.status or "created")
        for row in app.services
    }
    previous_status = app.status
    if had_active and app.status != "failed":
        previous_status = (
            "running" if any(status == "running" for status in old_statuses.values()) else "stopped"
        )
    old_slot = (app.active_slot or "").strip() or ("blue" if had_active else None)
    old_previous_slot = app.previous_slot
    old_app_host_port = app.host_port
    old_refs = {
        name: (row.container_id, row.container_name, row.host_port, old_statuses[name])
        for name, row in rows.items()
    }
    previous_snapshot = {
        name: (
            row.previous_container_id,
            row.previous_container_name,
            row.previous_host_port,
            row.previous_container_port,
            row.previous_image,
            row.previous_status,
        )
        for name, row in rows.items()
    }

    app.container_port = primary_port
    app.status = "building"
    db.commit()

    deployment = Deployment(
        app_id=app.id,
        image_tag=images.get(primary.name),
        build_status="building",
        mem_limit=rows[primary.name].mem_limit,
        cpu_quota=rows[primary.name].cpu_quota,
        strategy="bluegreen" if had_active else "rolling",
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    slot = f"v{deployment.id}"

    host_ports: dict[str, int] = {}
    for spec in plan.services:
        row = rows[spec.name]
        row.previous_container_id = row.container_id
        row.previous_container_name = row.container_name
        row.previous_host_port = row.host_port
        row.previous_container_port = row.container_port
        row.previous_image = row.image
        row.previous_status = row.status
        db.commit()
        if not spec.is_public:
            continue
        # 蓝绿切换必须换新端口，旧槽位仍占用原端口直到切换完成
        host_ports[spec.name] = port_allocator.reserve_for_service(
            db, row, force_new=bool(had_active)
        )

    app.host_port = host_ports[primary.name]
    deployment.slot = slot
    deployment.port = host_ports[primary.name]
    db.commit()

    keep = {slot, old_slot, app.previous_slot}
    with contextlib.suppress(Exception):
        compose_engine.prune_slots(app, keep)

    try:
        deployed = compose_engine.deploy_plan(
            app, plan, images, host_ports, slot=slot, on_log=logger.info
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            compose_engine.remove_slot(app, plan, slot)
        _restore_deploy_snapshot(
            db, app, rows, old_slot, old_previous_slot, old_app_host_port,
            previous_status, old_refs, previous_snapshot,
        )
        deployment.build_status = "failed"
        db.commit()
        raise AppError(f"compose 部署失败：{exc}") from exc

    for spec in plan.services:
        service = rows[spec.name]
        info = deployed.get(spec.name)
        if info is not None:
            service.container_id = info.container_id
            service.container_name = info.container_name
            service.status = "running"
        else:
            service.status = "failed"
        service.image = images.get(spec.name) or spec.image
        service.is_public = spec.is_public
        if not spec.is_public:
            service.host_port = None

    primary_row = rows[primary.name]
    primary_row.host_port = host_ports[primary.name]
    app.image = images.get(primary.name)
    app.host_port = host_ports[primary.name]
    app.status = "running"
    app.active_slot = slot
    app.previous_slot = old_slot
    deployment.container_id = primary_row.container_id
    deployment.container_name = primary_row.container_name
    deployment.image_tag = app.image
    db.commit()

    try:
        sync_routes(db, app)
    except AppError as exc:
        # 路由切换失败：撤下新槽位，恢复旧槽位与旧路由
        with contextlib.suppress(Exception):
            compose_engine.remove_slot(app, plan, slot)
        _restore_deploy_snapshot(
            db, app, rows, old_slot, old_previous_slot, old_app_host_port,
            previous_status, old_refs, previous_snapshot,
        )
        deployment.build_status = "failed"
        deployment.strategy = "bluegreen_failed"
        db.commit()
        raise AppError(f"路由切换失败，已回退旧版本：{exc}") from exc

    # 路由已指向新槽位：停止旧槽位容器（保留以便一键回滚），清理更早的版本
    with contextlib.suppress(Exception):
        compose_engine.stop_refs([ref[1] for ref in old_refs.values() if ref[1]])
    with contextlib.suppress(Exception):
        compose_engine.prune_slots(app, {slot, old_slot})

    deployment.build_status = "success"
    db.commit()
    db.refresh(app)
    return app


def _restore_deploy_snapshot(
    db,
    app,
    rows,
    old_slot,
    old_previous_slot,
    old_app_host_port,
    previous_status,
    old_refs,
    previous_snapshot,
) -> None:
    """蓝绿部署失败时恢复服务与应用的旧快照（含上一版本回滚信息）。"""
    for name, row in rows.items():
        container_id, container_name, host_port, status = old_refs[name]
        row.container_id = container_id
        row.container_name = container_name
        row.host_port = host_port
        row.status = status or "created"
        (
            row.previous_container_id,
            row.previous_container_name,
            row.previous_host_port,
            row.previous_container_port,
            row.previous_image,
            row.previous_status,
        ) = previous_snapshot[name]
    app.host_port = old_app_host_port
    app.status = previous_status or "created"
    app.active_slot = old_slot
    app.previous_slot = old_previous_slot
    db.commit()


def rollback_app(db: Session, app_id: int, user: User | None = None) -> App:
    """一键回滚到上一个蓝绿槽位：启动旧容器 → 切回路由 → 停止当前槽位。"""
    app = get_app(db, app_id, user)
    if not _is_multi_app(app):
        raise AppError("仅多容器 compose 应用支持蓝绿回滚")
    if not app.previous_slot:
        raise AppError("没有可回滚的上一版本")
    rows = list(app.services)
    prev_refs = [row.previous_container_name for row in rows if row.previous_container_name]
    if not prev_refs:
        raise AppError("上一版本容器记录缺失，请重新部署")
    public_rows = [row for row in rows if row.is_public]
    if any(not row.previous_host_port for row in public_rows):
        raise AppError("上一版本端口记录缺失，请重新部署")
    primary = _public_primary(app)
    if primary is None or not primary.previous_host_port:
        raise AppError("上一版本主入口端口缺失，请重新部署")

    active_slot = app.active_slot
    previous_slot = app.previous_slot
    active_host_port = app.host_port
    new_primary_port = primary.previous_host_port

    current_snap = {
        row.id: (
            row.container_id,
            row.container_name,
            row.host_port,
            row.container_port,
            row.image,
            row.status,
        )
        for row in rows
    }
    previous_snap = {
        row.id: (
            row.previous_container_id,
            row.previous_container_name,
            row.previous_host_port,
            row.previous_container_port,
            row.previous_image,
            row.previous_status,
        )
        for row in rows
    }

    try:
        compose_engine.start_refs(prev_refs)
    except compose_engine.ComposeEngineError as exc:
        raise AppError(str(exc)) from exc

    for row in rows:
        current = current_snap[row.id]
        previous = previous_snap[row.id]
        (
            row.container_id,
            row.container_name,
            row.host_port,
            row.container_port,
            row.image,
            row.status,
        ) = (
            previous[0],
            previous[1],
            previous[2],
            previous[3],
            previous[4],
            "running",
        )
        (
            row.previous_container_id,
            row.previous_container_name,
            row.previous_host_port,
            row.previous_container_port,
            row.previous_image,
            row.previous_status,
        ) = current

    app.host_port = new_primary_port
    app.container_port = primary.container_port or app.container_port
    app.image = primary.image
    app.active_slot, app.previous_slot = previous_slot, active_slot
    app.status = "running"
    db.commit()

    try:
        sync_routes(db, app)
    except AppError as exc:
        for row in rows:
            current = current_snap[row.id]
            previous = previous_snap[row.id]
            (
                row.container_id,
                row.container_name,
                row.host_port,
                row.container_port,
                row.image,
                row.status,
            ) = current
            (
                row.previous_container_id,
                row.previous_container_name,
                row.previous_host_port,
                row.previous_container_port,
                row.previous_image,
                row.previous_status,
            ) = previous
        app.host_port = active_host_port
        app.active_slot = active_slot
        app.previous_slot = previous_slot
        db.commit()
        compose_engine.stop_refs(prev_refs)
        raise AppError(f"回滚路由切换失败，已保持当前版本：{exc}") from exc

    # 路由已切回旧槽位：停止先前运行的槽位（保留以便再次正向发布）
    compose_engine.stop_refs(
        [row.previous_container_name for row in rows if row.previous_container_name]
    )
    with contextlib.suppress(Exception):
        compose_engine.prune_slots(app, {app.active_slot, app.previous_slot})

    deployment = Deployment(
        app_id=app.id,
        image_tag=primary.image,
        build_status="success",
        container_id=primary.container_id,
        container_name=primary.container_name,
        port=app.host_port,
        mem_limit=primary.mem_limit,
        cpu_quota=primary.cpu_quota,
        slot=app.active_slot,
        strategy="rollback",
    )
    db.add(deployment)
    db.commit()
    db.refresh(app)
    return app


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

    # 1) 识别项目类型（compose 优先级最高，走独立的单服务构建分支）
    try:
        profile = builder_service.detect(src)
    except (ValueError, FileNotFoundError) as exc:
        raise AppError(str(exc)) from exc

    if profile.project_type == "compose":
        plan = profile.compose
        if plan is None:
            raise AppError("compose 解析结果缺失")
        if plan.is_multi:
            return _build_compose_multi(db, app, plan, container_port)
        return _build_compose_app(db, app, src, profile, container_port)

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
    sync_default_service(
        db,
        app,
        image=image_tag,
        container_port=port,
        mem_limit=app.mem_limit,
        cpu_quota=app.cpu_quota,
        status="created",
        is_public=True,
    )
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
    if _is_multi_app(app):
        refs = [
            ref
            for service in app.services
            for ref in (service.container_name, service.previous_container_name)
            if ref
        ]
        compose_engine.stop_refs(refs)
        for service in app.services:
            if service.container_name:
                service.status = "stopped"
            if service.previous_container_name:
                service.previous_status = "stopped"
        app.status = "stopped"
        db.commit()
        db.refresh(app)
        return app
    try:
        docker_service.stop(docker_service.container_name_for(app.id, app.name))
    except docker_service.ContainerNotFoundError:
        pass
    app.status = "stopped"
    sync_default_service(db, app, status="stopped")
    db.commit()
    db.refresh(app)
    return app


def restart_app(db: Session, app_id: int, user: User | None = None) -> App:
    app = get_app(db, app_id, user)
    if not app.host_port:
        raise AppError("应用尚未部署，无法重启")
    if _is_multi_app(app):
        try:
            compose_engine.restart_plan(app, app.services)
        except compose_engine.ComposeEngineError as exc:
            raise AppError(str(exc)) from exc
        for service in app.services:
            service.status = "running"
        app.status = "running"
        db.commit()
        db.refresh(app)
        return app
    try:
        docker_service.restart(docker_service.container_name_for(app.id, app.name))
    except docker_service.ContainerNotFoundError:
        raise AppError("容器不存在，请重新部署") from None
    app.status = "running"
    sync_default_service(db, app, status="running")
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
        if _is_multi_app(app):
            for service in app.services:
                if not service.is_public or not service.container_name:
                    continue
                try:
                    docker_service.update_resources(
                        service.container_name, mem_limit, cpu_quota
                    )
                except docker_service.ContainerNotFoundError:
                    continue
        else:
            try:
                docker_service.update_resources(
                    docker_service.container_name_for(app.id, app.name), mem_limit, cpu_quota
                )
            except docker_service.ContainerNotFoundError:
                pass
    app.mem_limit = mem_limit
    app.cpu_quota = cpu_quota
    if _is_multi_app(app):
        for service in app.services:
            if service.is_public:
                service.mem_limit = mem_limit
                service.cpu_quota = cpu_quota
    else:
        sync_default_service(db, app, mem_limit=mem_limit, cpu_quota=cpu_quota)
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
    if _is_multi_app(app):
        with contextlib.suppress(Exception):
            compose_engine.remove_plan(app, app.services, remove_volumes=True)
        db.delete(app)
        db.commit()
        return
    try:
        docker_service.remove(docker_service.container_name_for(app.id, app.name), force=True)
    except docker_service.ContainerNotFoundError:
        pass
    db.delete(app)
    db.commit()


def app_runtime(
    db: Session, app_id: int, user: User | None = None, service: str | None = None
) -> dict:
    """查询应用（或指定服务）对应的容器实时状态。"""
    app = get_app(db, app_id, user)
    svc, ref = resolve_service_container(app, service)
    service_name = svc.name if svc is not None else None
    try:
        info = docker_service.get(ref)
    except docker_service.ContainerNotFoundError:
        return {
            "app_id": app.id,
            "service": service_name,
            "container": None,
            "stats": None,
        }
    return {
        "app_id": app.id,
        "service": service_name,
        "container": info,
        "stats": docker_service.stats(info.name),
    }
