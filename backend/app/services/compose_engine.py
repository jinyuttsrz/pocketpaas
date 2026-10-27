"""多容器 Compose 编排引擎（基于 docker-py）。

不依赖宿主机的 docker compose CLI，直接复用平台已有的 Docker 客户端：
- 构建 / 拉取每个服务的镜像；
- 为应用创建独立网络与命名卷；
- 按 depends_on 拓扑顺序创建并启动容器；
- 支持 healthcheck、资源限制、容器加固与停止 / 重启 / 清理。
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass
from typing import Callable

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from app.core.config import settings
from app.services import builder_service, docker_service, security_service
from app.services.compose_service import ComposePlan, ComposeServiceSpec, VolumeMount


class ComposeEngineError(RuntimeError):
    """多容器编排失败。"""


@dataclass
class DeployedService:
    name: str
    image: str
    container_id: str
    container_name: str
    status: str


def _safe(value: str, limit: int = 40) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-._").lower()
    return (text or "svc")[:limit]


def project_labels(app) -> dict[str, str]:
    return {
        docker_service.LABEL_MANAGED: "true",
        docker_service.LABEL_APP_ID: str(app.id),
        docker_service.LABEL_APP_NAME: app.name,
    }


def network_name(app_id: int, slot: str = "blue") -> str:
    """发布槽位网络：blue 沿用旧网络名，其余槽位使用独立网络避免 DNS 别名冲突。"""
    if slot == "blue":
        return f"paas-{app_id}-net"
    return f"paas-{app_id}-net-{_safe(slot, 12)}"


def volume_name(app_id: int, source: str) -> str:
    return f"paas-{app_id}-{_safe(source, 40)}"


def anon_volume_name(app_id: int, service: str, index: int) -> str:
    return f"paas-{app_id}-{_safe(service, 24)}-anon-{index}"


def service_image_tag(app, spec: ComposeServiceSpec) -> str:
    return f"{settings.IMAGE_PREFIX}/{app.subdomain}-{_safe(spec.name)}:latest"


def service_container_name(app, spec: ComposeServiceSpec) -> str:
    """主入口沿用旧容器名；其他公开服务与内部服务按服务名区分。"""
    if spec.is_primary:
        return docker_service.container_name_for(app.id, app.name)
    return docker_service.container_name_for(app.id, f"{app.name}-{spec.name}")


def slot_container_name(app, spec: ComposeServiceSpec, slot: str = "blue") -> str:
    """槽位容器名：blue 保持兼容命名，其余版本槽位追加后缀。"""
    base = service_container_name(app, spec)
    if slot == "blue":
        return base
    return f"{base}-{_safe(slot, 12)}"


def mount_source(app_id: int, spec: ComposeServiceSpec, mount: VolumeMount, index: int) -> str:
    if mount.source:
        return volume_name(app_id, mount.source)
    return anon_volume_name(app_id, spec.name, index)


def _restart_policy(spec: ComposeServiceSpec) -> dict:
    text = (spec.restart or "unless-stopped").strip()
    if text.startswith("on-failure"):
        parts = text.split(":", 1)
        retries = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0
        return {"Name": "on-failure", "MaximumRetryCount": retries}
    if text in ("no", "always", "unless-stopped"):
        return {"Name": text, "MaximumRetryCount": 0}
    return {"Name": "unless-stopped", "MaximumRetryCount": 0}


def _prepare_dockerfile(spec: ComposeServiceSpec, port: int) -> tuple[str, list[str]]:
    """返回 (dockerfile 相对路径, 日志行)。"""
    context = spec.build_context
    if context is None:
        raise ComposeEngineError(f"服务 {spec.name} 缺少构建上下文")
    logs: list[str] = []
    dockerfile = spec.build_dockerfile
    existing = context / (dockerfile or "Dockerfile")
    regenerate = dockerfile is None or (
        dockerfile == "Dockerfile" and builder_service.is_platform_generated(existing)
    )
    if regenerate:
        profile = builder_service.detect(context, check_compose=False)
        content = builder_service.generate(context, profile, port)
        builder_service.write_dockerfile(context, content)
        logs.append(f"未提供可复用的 Dockerfile，已按 {profile.project_type} 类型生成（端口 {port}）")
        dockerfile = "Dockerfile"
    else:
        security_service.scan_dockerfile(context, dockerfile)
    return dockerfile, logs


def build_images(
    app,
    plan: ComposePlan,
    on_log: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """构建 / 拉取所有服务镜像，返回 服务名 → 镜像引用。"""
    images: dict[str, str] = {}

    def emit(spec: ComposeServiceSpec, line: str) -> None:
        if on_log:
            on_log(f"[{spec.name}] {line}")

    for spec in plan.services:
        if spec.needs_build:
            tag = service_image_tag(app, spec)
            port = spec.container_port or app.container_port or 8000
            dockerfile, logs = _prepare_dockerfile(spec, port)
            for line in logs:
                emit(spec, line)
            emit(spec, f"构建镜像 {tag}（context={spec.build_context}，dockerfile={dockerfile}）")
            result = docker_service.build_image(
                context_dir=str(spec.build_context),
                image_tag=tag,
                dockerfile=dockerfile,
                on_log=lambda line: emit(spec, line),
            )
            if not result.success:
                raise ComposeEngineError(f"服务 {spec.name} 镜像构建失败：{result.error}")
            images[spec.name] = tag
        else:
            image = spec.image
            if not image:
                raise ComposeEngineError(f"服务 {spec.name} 未声明镜像")
            emit(spec, f"确保镜像可用：{image}")
            docker_service.pull_image(image)
            images[spec.name] = image
    return images


def _ensure_network(cli: docker.DockerClient, app, slot: str = "blue"):
    name = network_name(app.id, slot)
    existing = cli.networks.list(names=[name])
    if existing:
        return name
    cli.networks.create(
        name,
        driver="bridge",
        check_duplicate=True,
        labels={**project_labels(app), "paas.slot": slot},
    )
    return name


def _ensure_volume(cli: docker.DockerClient, app, name: str):
    try:
        return cli.volumes.get(name)
    except NotFound:
        return cli.volumes.create(name=name, labels=project_labels(app))


def _ensure_volumes(cli: docker.DockerClient, app, plan: ComposePlan) -> None:
    for spec in plan.services:
        for index, mount in enumerate(spec.volumes):
            if mount.tmpfs:
                continue
            _ensure_volume(cli, app, mount_source(app.id, spec, mount, index))


def _remove_app_containers(cli: docker.DockerClient, app_id: int) -> None:
    containers = cli.containers.list(
        all=True, filters={"label": f"{docker_service.LABEL_APP_ID}={app_id}"}
    )
    for container in containers:
        with contextlib.suppress(Exception):
            container.remove(force=True)


def _container_kwargs(app, spec, image, host_port, network, cli, slot: str = "blue") -> dict:
    ports = {}
    if spec.is_public and host_port and spec.container_port:
        ports[f"{spec.container_port}/tcp"] = host_port

    environment = dict(spec.environment)
    command = spec.command
    if spec.is_primary:
        environment.update(app.env_map)
        command = app.command or spec.command

    if spec.is_primary:
        # 主入口配额以应用配额为准：构建时用 compose 值初始化，
        # 之后控制台调整配额可直接覆盖，不会被 compose 文件重新盖回去。
        mem_limit = app.mem_limit
        cpu_quota = app.cpu_quota
    else:
        mem_limit = spec.mem_limit or settings.DEFAULT_MEM_LIMIT
        cpu_quota = (
            spec.cpu_quota
            if spec.cpu_quota is not None
            else settings.DEFAULT_CPU_QUOTA
        )
    period = 100_000
    quota = int(cpu_quota * period)

    labels = {str(k): str(v) for k, v in spec.labels.items()}
    labels.update(project_labels(app))
    labels["paas.service"] = spec.name
    labels["paas.slot"] = slot
    labels["paas.primary"] = "true" if spec.is_primary else "false"

    volumes: dict[str, dict] = {}
    tmpfs: dict[str, str] = {}
    for index, mount in enumerate(spec.volumes):
        if mount.tmpfs:
            tmpfs[mount.target] = ""
            continue
        source = mount_source(app.id, spec, mount, index)
        volumes[source] = {"bind": mount.target, "mode": mount.mode or "rw"}

    kwargs: dict = {
        "image": image,
        "name": slot_container_name(app, spec, slot),
        "hostname": spec.name,
        "detach": True,
        "environment": environment,
        "labels": labels,
        "mem_limit": mem_limit,
        "memswap_limit": mem_limit,
        "cpu_period": period,
        "cpu_quota": quota,
        "restart_policy": _restart_policy(spec),
        "network": network,
        "networking_config": cli.api.create_networking_config(
            {
                network: cli.api.create_endpoint_config(aliases=[spec.name]),
            }
        ),
    }
    if ports:
        kwargs["ports"] = ports
    if command is not None:
        kwargs["command"] = command
    if spec.entrypoint is not None:
        kwargs["entrypoint"] = spec.entrypoint
    if spec.healthcheck is not None:
        kwargs["healthcheck"] = spec.healthcheck
    if volumes:
        kwargs["volumes"] = volumes
    if tmpfs:
        kwargs["tmpfs"] = tmpfs
    if spec.working_dir:
        kwargs["working_dir"] = spec.working_dir
    if spec.user:
        kwargs["user"] = spec.user
    if spec.read_only:
        kwargs["read_only"] = True
    if spec.init is not None:
        kwargs["init"] = spec.init
    if spec.tty is not None:
        kwargs["tty"] = spec.tty
    if spec.stdin_open is not None:
        kwargs["stdin_open"] = spec.stdin_open
    if spec.stop_signal:
        kwargs["stop_signal"] = spec.stop_signal
    kwargs.update(security_service.container_security_kwargs())
    return kwargs


def _wait_running(container: Container, service: str) -> None:
    deadline = time.time() + settings.COMPOSE_START_TIMEOUT
    while time.time() < deadline:
        container.reload()
        status = container.status
        if status == "running":
            return
        if status in ("exited", "dead"):
            logs = ""
            with contextlib.suppress(Exception):
                raw = container.logs(tail=40)
                logs = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            raise ComposeEngineError(
                f"服务 {service} 启动后退出（{status}）：{logs.strip()[:1200]}"
            )
        time.sleep(0.5)
    raise ComposeEngineError(f"服务 {service} 在 {settings.COMPOSE_START_TIMEOUT}s 内未进入运行状态")


def _wait_healthy(container: Container, service: str) -> None:
    deadline = time.time() + settings.COMPOSE_HEALTH_TIMEOUT
    while time.time() < deadline:
        container.reload()
        state = (container.attrs.get("State") or {})
        health = state.get("Health") or {}
        status = health.get("Status")
        if status == "healthy":
            return
        if status == "unhealthy":
            raise ComposeEngineError(f"服务 {service} 健康检查失败")
        time.sleep(1)
    raise ComposeEngineError(
        f"服务 {service} 在 {settings.COMPOSE_HEALTH_TIMEOUT}s 内未变为 healthy"
    )


def prune_slots(app, keep: set[str]) -> None:
    """删除不在 keep 集合中的旧槽位容器与网络（防止版本堆积）。"""
    keep = {slot for slot in keep if slot}
    with docker_service.client() as cli:
        for container in cli.containers.list(
            all=True, filters={"label": f"{docker_service.LABEL_APP_ID}={app.id}"}
        ):
            slot = container.labels.get("paas.slot") or "blue"
            if slot not in keep:
                with contextlib.suppress(Exception):
                    container.remove(force=True)
        for network in cli.networks.list(
            filters={"label": f"{docker_service.LABEL_APP_ID}={app.id}"}
        ):
            labels = (network.attrs or {}).get("Labels") or {}
            slot = labels.get("paas.slot") or "blue"
            if slot not in keep:
                with contextlib.suppress(Exception):
                    network.remove()


def _remove_slot_containers(cli: docker.DockerClient, app, plan: ComposePlan, slot: str) -> None:
    """删除目标槽位中可能残留的同名容器（两次发布前的旧槽位）。"""
    for spec in plan.services:
        name = slot_container_name(app, spec, slot)
        try:
            cli.containers.get(name).remove(force=True)
        except NotFound:
            continue
        except Exception:
            continue


def _remove_slot_network(cli: docker.DockerClient, app, slot: str) -> None:
    for network in cli.networks.list(names=[network_name(app.id, slot)]):
        with contextlib.suppress(Exception):
            network.remove()


def deploy_plan(
    app,
    plan: ComposePlan,
    images: dict[str, str],
    host_ports: dict[str, int],
    slot: str = "blue",
    on_log: Callable[[str], None] | None = None,
) -> dict[str, DeployedService]:
    """在指定发布槽位启动整个 compose 项目，返回每个服务的部署结果。

    蓝绿发布时新旧槽位使用独立网络与容器名，旧槽位容器保持运行，待路由切换后再停止。
    """
    specs = plan.service_map
    for spec in plan.services:
        for dep, condition in spec.depends_on.items():
            if condition == "service_healthy" and specs[dep].healthcheck is None:
                raise ComposeEngineError(
                    f"服务 {spec.name} 依赖 {dep} 健康，但 {dep} 未声明 healthcheck"
                )

    deployed: dict[str, DeployedService] = {}
    created: list[Container] = []

    def emit(line: str) -> None:
        if on_log:
            on_log(line)

    try:
        with docker_service.client(timeout=max(settings.COMPOSE_START_TIMEOUT, 60)) as cli:
            _remove_slot_containers(cli, app, plan, slot)
            _remove_slot_network(cli, app, slot)
            network = _ensure_network(cli, app, slot)
            _ensure_volumes(cli, app, plan)

            for spec in plan.start_order():
                image = images.get(spec.name) or spec.image
                if not image:
                    raise ComposeEngineError(f"服务 {spec.name} 没有可用镜像")
                emit(f"[{spec.name}] 在 {slot} 槽位创建并启动容器")
                container = cli.containers.create(
                    **_container_kwargs(
                        app,
                        spec,
                        image,
                        host_ports.get(spec.name),
                        network,
                        cli,
                        slot,
                    )
                )
                created.append(container)
                container.start()
                _wait_running(container, spec.name)
                for dep, condition in spec.depends_on.items():
                    if condition == "service_healthy":
                        _wait_healthy(
                            cli.containers.get(
                                slot_container_name(app, specs[dep], slot)
                            ),
                            dep,
                        )
                info = docker_service.get(container.name)
                deployed[spec.name] = DeployedService(
                    name=spec.name,
                    image=image,
                    container_id=info.id,
                    container_name=info.name,
                    status=info.status,
                )
    except (DockerException, APIError, ImageNotFound) as exc:
        _cleanup_created(app, created)
        with contextlib.suppress(Exception):
            with docker_service.client() as cli:
                _remove_slot_network(cli, app, slot)
        raise ComposeEngineError(f"compose 部署失败：{exc}") from exc
    except Exception:
        _cleanup_created(app, created)
        with contextlib.suppress(Exception):
            with docker_service.client() as cli:
                _remove_slot_network(cli, app, slot)
        raise
    return deployed


def remove_slot(app, plan: ComposePlan, slot: str) -> None:
    """删除指定槽位的容器与网络（部署失败回滚 / 蓝绿切换后清理）。"""
    with docker_service.client() as cli:
        _remove_slot_containers(cli, app, plan, slot)
        _remove_slot_network(cli, app, slot)


def _cleanup_created(app, containers: list[Container]) -> None:
    for container in containers:
        with contextlib.suppress(Exception):
            container.remove(force=True)


def stop_plan(app, services) -> None:
    for service in services:
        if not service.container_name:
            continue
        with contextlib.suppress(docker_service.ContainerNotFoundError):
            docker_service.stop(service.container_name)


def restart_plan(app, services) -> None:
    errors = []
    for service in services:
        if not service.container_name:
            continue
        try:
            docker_service.restart(service.container_name)
        except docker_service.ContainerNotFoundError:
            errors.append(service.name)
    if errors:
        raise ComposeEngineError(f"以下服务容器不存在，请重新部署：{', '.join(errors)}")


def remove_plan(app, services, *, remove_volumes: bool = False) -> None:
    with docker_service.client() as cli:
        _remove_app_containers(cli, app.id)
        for network in cli.networks.list(
            filters={"label": f"{docker_service.LABEL_APP_ID}={app.id}"}
        ):
            with contextlib.suppress(Exception):
                network.remove()
        if remove_volumes:
            for volume in cli.volumes.list(
                filters={"label": f"{docker_service.LABEL_APP_ID}={app.id}"}
            ):
                with contextlib.suppress(Exception):
                    volume.remove(force=True)


def stop_refs(refs: list[str]) -> None:
    """停止一组容器（蓝绿切换后停旧槽位）。"""
    for ref in refs:
        if not ref:
            continue
        with contextlib.suppress(docker_service.ContainerNotFoundError):
            docker_service.stop(ref)


def start_refs(refs: list[str]) -> None:
    """启动一组容器（回滚时恢复旧槽位）。"""
    missing: list[str] = []
    for ref in refs:
        if not ref:
            continue
        try:
            docker_service.start(ref)
        except docker_service.ContainerNotFoundError:
            missing.append(ref)
    if missing:
        raise ComposeEngineError(f"以下回滚容器不存在：{', '.join(missing)}")


def runtime_status(app, services) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for service in services:
        if not service.container_name:
            result[service.name] = {"status": "missing", "container": None}
            continue
        try:
            info = docker_service.get(service.container_name)
            result[service.name] = {"status": info.status, "container": info}
        except docker_service.ContainerNotFoundError:
            result[service.name] = {"status": "missing", "container": None}
    return result
