"""Docker Compose 解析与部署规格。

两个使用层级：
- 单服务：沿用平台现有的单容器部署链路（兼容 Plan A）；
- 多服务：解析为 ``ComposePlan``，由 ``compose_engine`` 逐服务编排。

安全边界：
- 拒绝 privileged、host 网络、宿主 bind mount、docker.sock 等突破隔离的字段；
- 命名卷、内部网络、env_file 等可用字段做路径与来源校验；
- 未实现字段不生效，并在构建日志中提示。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

COMPOSE_FILE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yml",
    "docker-compose.yaml",
)

_TOP_LEVEL_KEYS = {
    "version",
    "name",
    "services",
    "networks",
    "volumes",
    "x-paas",
}

_DANGEROUS_KEYS: dict[str, str] = {
    "privileged": "不允许使用 privileged 模式",
    "network_mode": "不允许使用 host / service / container 网络模式",
    "pid": "不允许共享 PID 命名空间",
    "ipc": "不允许共享 IPC 命名空间",
    "uts": "不允许共享 UTS 命名空间",
    "userns_mode": "不允许自定义用户命名空间",
    "cgroup_parent": "不允许指定 cgroup 父级",
    "devices": "不允许直通宿主设备",
    "device_cgroup_rules": "不允许自定义设备规则",
    "cap_add": "不允许额外添加 capability",
    "security_opt": "不允许覆盖容器安全选项",
    "group_add": "不允许追加宿主用户组",
    "volumes_from": "暂不支持 volumes_from",
    "secrets": "暂不支持 secrets",
    "configs": "暂不支持 configs",
    "sysctls": "暂不支持自定义 sysctls",
    "runtime": "不允许指定自定义运行时",
    "isolation": "不允许指定隔离级别",
}

_REJECTED_KEYS: dict[str, str] = {
    "extends": "暂不支持 compose extends",
    "scale": "暂不支持 scale",
    "profiles": "暂不支持 profiles，请移除或使用独立 compose 文件",
    "platform": "暂不支持指定运行架构",
    "build.args": "暂不支持 build.args，请在 Dockerfile 中处理构建参数",
    "build.target": "暂不支持多阶段构建的 build.target",
    "build.cache_from": "暂不支持 build.cache_from",
    "build.cache_to": "暂不支持 build.cache_to",
    "build.secrets": "暂不支持 build.secrets",
    "build.ssh": "暂不支持 build.ssh",
    "build.platform": "暂不支持指定构建平台",
}

_IGNORED_NOTICES: dict[str, str] = {
    "restart": "重启策略将按服务声明或平台默认值执行",
    "container_name": "容器名由平台统一生成",
    "hostname": "容器主机名由平台统一生成",
    "labels": "标签会保留并叠加平台标签",
    "logging": "日志驱动由平台统一配置",
    "stop_grace_period": "停止宽限期会尽量保留",
    "stop_signal": "停止信号会尽量保留",
    "init": "init 进程会尽量保留",
    "tty": "TTY 由平台按需开启",
    "stdin_open": "标准输入由平台按需开启",
    "read_only": "只读根文件系统会尽量保留",
    "cpuset": "CPU 绑定未生效",
    "mem_reservation": "内存预留未生效",
    "shm_size": "共享内存大小未生效",
    "ulimits": "ulimit 未生效",
    "extra_hosts": "额外主机映射未生效",
    "dns": "DNS 配置未生效",
    "dns_search": "DNS 搜索域未生效",
    "links": "容器链接未生效",
    "external_links": "外部链接未生效",
    "pull_policy": "镜像拉取由平台统一处理",
}

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_MAX_ITEMS = 50
_ENV_MAX_VALUE = 1024
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ns|us|ms|s|m|h)")
_DURATION_UNITS = {"ns": 1, "us": 1000, "ms": 1000_000, "s": 1000_000_000,
                   "m": 60 * 1000_000_000, "h": 3600 * 1000_000_000}


class ComposeError(ValueError):
    """compose 文件不合法或超出支持范围。"""


@dataclass
class VolumeMount:
    """一个卷挂载：命名卷 / 匿名卷 / tmpfs。"""

    target: str
    source: str | None = None
    mode: str | None = None
    anonymous: bool = False
    tmpfs: bool = False


@dataclass
class ComposeServiceSpec:
    """单个 compose 服务的部署规格。"""

    name: str
    image: str | None = None
    build_context: Path | None = None
    build_dockerfile: str | None = None
    published_ports: list[int] = field(default_factory=list)
    exposed_ports: list[int] = field(default_factory=list)
    container_port: int | None = None
    is_public: bool = False
    # 多公开服务中的主入口：沿用应用级访问地址与旧容器名
    is_primary: bool = False
    environment: dict[str, str] = field(default_factory=dict)
    env_files: list[Path] = field(default_factory=list)
    command: str | list[str] | None = None
    entrypoint: str | list[str] | None = None
    mem_limit: str | None = None
    cpu_quota: float | None = None
    depends_on: dict[str, str] = field(default_factory=dict)
    healthcheck: dict | None = None
    restart: str | None = None
    volumes: list[VolumeMount] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    working_dir: str | None = None
    user: str | None = None
    read_only: bool = False
    init: bool | None = None
    tty: bool | None = None
    stdin_open: bool | None = None
    stop_signal: str | None = None
    stop_timeout: int | None = None
    x_paas: dict = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)

    @property
    def needs_build(self) -> bool:
        return self.build_context is not None


@dataclass
class ComposePlan:
    """整个 compose 文件解析后的部署计划。"""

    compose_path: Path
    services: list[ComposeServiceSpec]
    public_service: str | None = None
    public_services: list[str] = field(default_factory=list)
    volumes: dict[str, dict] = field(default_factory=dict)
    networks: dict[str, dict] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)

    @property
    def service_map(self) -> dict[str, ComposeServiceSpec]:
        return {s.name: s for s in self.services}

    @property
    def is_multi(self) -> bool:
        return len(self.services) > 1

    @property
    def single_service(self) -> ComposeServiceSpec | None:
        return self.services[0] if len(self.services) == 1 else None

    def get(self, name: str) -> ComposeServiceSpec:
        for service in self.services:
            if service.name == name:
                return service
        raise ComposeError(f"服务不存在：{name}")

    @property
    def public(self) -> ComposeServiceSpec | None:
        if not self.public_service:
            return None
        return self.get(self.public_service)

    @property
    def publics(self) -> list[ComposeServiceSpec]:
        """全部公开服务（第一个为主入口）。"""
        return [self.get(name) for name in self.public_services]

    def start_order(self) -> list[ComposeServiceSpec]:
        """按 depends_on 拓扑排序；存在环时抛 ComposeError。"""
        specs = self.service_map
        visited: dict[str, int] = {}  # 0 未访问 / 1 访问中 / 2 完成
        order: list[ComposeServiceSpec] = []

        def visit(name: str) -> None:
            state = visited.get(name, 0)
            if state == 1:
                raise ComposeError(f"depends_on 存在循环依赖：{name}")
            if state == 2:
                return
            visited[name] = 1
            for dep in specs[name].depends_on:
                visit(dep)
            visited[name] = 2
            order.append(specs[name])

        for name in specs:
            visit(name)
        return order

    # ---- 单服务兼容属性（原 Plan A 构建分支使用） ----
    @property
    def service_name(self) -> str:
        single = self.single_service
        if single is None:
            raise ComposeError("多服务 compose 不支持单服务属性")
        return single.name

    @property
    def image(self) -> str | None:
        return self.single_service.image if self.single_service else None

    @property
    def build_context(self) -> Path | None:
        return self.single_service.build_context if self.single_service else None

    @property
    def build_dockerfile(self) -> str | None:
        return self.single_service.build_dockerfile if self.single_service else None

    @property
    def container_port(self) -> int | None:
        return self.single_service.container_port if self.single_service else None

    @property
    def environment(self) -> dict[str, str]:
        return self.single_service.environment if self.single_service else {}

    @property
    def command(self) -> str | None:
        single = self.single_service
        if single is None or single.command is None:
            return None
        if isinstance(single.command, list):
            return shlex.join(single.command)
        return single.command

    @property
    def mem_limit(self) -> str | None:
        return self.single_service.mem_limit if self.single_service else None

    @property
    def cpu_quota(self) -> float | None:
        return self.single_service.cpu_quota if self.single_service else None

    @property
    def needs_build(self) -> bool:
        return bool(self.single_service and self.single_service.needs_build)


def find(src: Path) -> Path | None:
    """在源码根目录查找 compose 文件。"""
    if not src.exists() or not src.is_dir():
        return None
    for name in COMPOSE_FILE_NAMES:
        candidate = src / name
        if candidate.is_file():
            return candidate
    return None


def load(src: Path, compose_path: Path | None = None) -> ComposePlan:
    """解析 compose 文件，返回可部署计划。"""
    path = compose_path or find(src)
    if path is None:
        raise ComposeError("未找到 docker-compose.yml / compose.yaml")

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise ComposeError(f"compose 文件读取失败：{exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ComposeError(f"compose YAML 解析失败：{exc}") from exc

    if not isinstance(data, dict):
        raise ComposeError("compose 文件内容必须是一个映射（mapping）")

    services_raw = data.get("services")
    if not isinstance(services_raw, dict) or not services_raw:
        raise ComposeError("compose 文件缺少 services 或 services 为空")
    services_raw = {str(key): value for key, value in services_raw.items()}

    top_x = data.get("x-paas")
    if top_x is None:
        top_x = {}
    if not isinstance(top_x, dict):
        raise ComposeError("顶层 x-paas 必须是映射")

    notices = _check_top_level(data)
    volumes = _parse_top_volumes(data.get("volumes"))
    networks = _parse_top_networks(data.get("networks"))

    specs: list[ComposeServiceSpec] = []
    for name, service in services_raw.items():
        if not isinstance(service, dict):
            raise ComposeError(f"服务 {name} 的定义必须是映射")
        specs.append(_parse_service(name, service, src, top_x))

    plan = ComposePlan(
        compose_path=path,
        services=specs,
        volumes=volumes,
        networks=networks,
        notices=notices,
    )
    _select_public(plan, top_x)
    _validate_depends(plan)
    _single_mode_guard(plan)

    for spec in specs:
        prefix = f"[{spec.name}] " if plan.is_multi else ""
        plan.notices.extend(prefix + notice for notice in spec.notices)
    plan.notices.extend(_network_notice(specs))
    return plan


def _check_top_level(data: dict) -> list[str]:
    notices: list[str] = []
    for key in data:
        if key in _TOP_LEVEL_KEYS:
            continue
        if key in ("secrets", "configs"):
            raise ComposeError(f"暂不支持顶层 {key}")
        notices.append(f"顶层字段 {key} 未生效")
    if data.get("name"):
        notices.append("顶层 name 未生效，项目名由平台生成")
    return notices


def _parse_top_volumes(value: Any) -> dict[str, dict]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ComposeError("顶层 volumes 必须是映射")
    result: dict[str, dict] = {}
    for name, config in value.items():
        name = str(name)
        config = config or {}
        if not isinstance(config, dict):
            raise ComposeError(f"卷 {name} 的定义必须是映射")
        if config.get("external"):
            raise ComposeError(f"卷 {name} 不允许使用 external")
        if config.get("name"):
            raise ComposeError(f"卷 {name} 不允许自定义 name")
        if config.get("driver_opts"):
            raise ComposeError(f"卷 {name} 不允许 driver_opts（可能挂载宿主路径）")
        result[name] = dict(config)
    return result


def _parse_top_networks(value: Any) -> dict[str, dict]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ComposeError("顶层 networks 必须是映射")
    result: dict[str, dict] = {}
    for name, config in value.items():
        name = str(name)
        config = config or {}
        if not isinstance(config, dict):
            raise ComposeError(f"网络 {name} 的定义必须是映射")
        if config.get("external"):
            raise ComposeError(f"网络 {name} 不允许使用 external")
        if config.get("name"):
            raise ComposeError(f"网络 {name} 不允许自定义 name")
        if config.get("driver_opts"):
            raise ComposeError(f"网络 {name} 不允许 driver_opts")
        result[name] = dict(config)
    return result


def _parse_service(name: str, service: dict, src: Path, top_x: dict) -> ComposeServiceSpec:
    spec = ComposeServiceSpec(name=name)

    for key, reason in _DANGEROUS_KEYS.items():
        if key not in service:
            continue
        value = service.get(key)
        if value is None or value is False or value == [] or value == {}:
            continue
        raise ComposeError(f"服务 {name} 的字段 {key} 不受支持：{reason}")

    for key, reason in _REJECTED_KEYS.items():
        if "." in key:
            head, sub = key.split(".", 1)
            value = service.get(head)
            if isinstance(value, dict) and sub in value:
                raise ComposeError(f"服务 {name} 的字段 {key} 不受支持：{reason}")
        elif key in service:
            raise ComposeError(f"服务 {name} 的字段 {key} 不受支持：{reason}")

    _parse_image_and_build(spec, service, src)
    if not spec.image and spec.build_context is None:
        raise ComposeError(f"服务 {name} 需要至少提供 image 或 build")

    spec.x_paas = service.get("x-paas") if isinstance(service.get("x-paas"), dict) else {}
    _parse_ports(spec, service)
    _parse_env(spec, service, src)
    _parse_command(spec, service)
    _parse_limits(spec, service)
    _parse_depends(spec, service)
    spec.healthcheck = _parse_healthcheck(service.get("healthcheck"), name)
    spec.restart = _parse_restart(service.get("restart"))
    _parse_volumes(spec, service, src)
    _parse_networks(spec, service)
    _parse_scalars(spec, service)

    deploy = service.get("deploy")
    if isinstance(deploy, dict):
        replicas = deploy.get("replicas")
        if replicas not in (None, 1, "1"):
            raise ComposeError(f"服务 {name} 暂不支持多副本部署（deploy.replicas > 1）")
        for key in deploy:
            if key in ("resources", "replicas"):
                continue
            spec.notices.append(f"字段 deploy.{key} 未生效")

    for key in service:
        if key in {
            "image", "build", "ports", "expose", "environment", "env_file",
            "command", "entrypoint", "mem_limit", "cpus", "deploy", "x-paas",
            "depends_on", "healthcheck", "restart", "volumes", "networks",
            "working_dir", "user", "read_only", "init", "tty", "stdin_open",
            "stop_signal", "stop_grace_period", "labels",
        }:
            continue
        notice = _IGNORED_NOTICES.get(str(key))
        if notice:
            spec.notices.append(f"字段 {key} 未生效：{notice}")
        else:
            spec.notices.append(f"字段 {key} 未生效（当前版本未支持）")
    return spec


def _parse_image_and_build(spec: ComposeServiceSpec, service: dict, src: Path) -> None:
    image = service.get("image")
    if image is not None:
        if not isinstance(image, str) or not image.strip():
            raise ComposeError(f"服务 {spec.name} 的 image 必须是非空字符串")
        spec.image = image.strip()

    build = service.get("build")
    if build is None:
        return

    if isinstance(build, str):
        context_raw, dockerfile = build, None
    elif isinstance(build, dict):
        for key, reason in _REJECTED_KEYS.items():
            if not key.startswith("build."):
                continue
            sub = key.split(".", 1)[1]
            if sub in build:
                raise ComposeError(f"服务 {spec.name} 的字段 {key} 不受支持：{reason}")
        context_raw = build.get("context", ".")
        dockerfile = build.get("dockerfile")
    else:
        raise ComposeError(f"服务 {spec.name} 的 build 必须是路径字符串或映射")

    if not isinstance(context_raw, str) or not context_raw.strip():
        raise ComposeError(f"服务 {spec.name} 的 build.context 必须是非空字符串")

    base = src.resolve()
    context = (base / context_raw).resolve()
    if base != context and base not in context.parents:
        raise ComposeError(f"服务 {spec.name} 的 build.context 超出源码目录：{context_raw}")
    if not context.is_dir():
        raise ComposeError(f"服务 {spec.name} 的 build.context 不存在或不是目录：{context_raw}")
    spec.build_context = context

    if dockerfile is not None:
        if not isinstance(dockerfile, str) or not dockerfile.strip():
            raise ComposeError(f"服务 {spec.name} 的 build.dockerfile 必须是非空字符串")
        df_rel = dockerfile.strip()
        df_path = (context / df_rel).resolve()
        if context != df_path and context not in df_path.parents:
            raise ComposeError(f"服务 {spec.name} 的 build.dockerfile 超出构建上下文：{df_rel}")
        if not df_path.is_file():
            raise ComposeError(f"服务 {spec.name} 的 build.dockerfile 不存在：{df_rel}")
        spec.build_dockerfile = df_rel
    elif (context / "Dockerfile").is_file():
        spec.build_dockerfile = "Dockerfile"


def _parse_ports(spec: ComposeServiceSpec, service: dict) -> None:
    ports = service.get("ports")
    if ports is not None:
        if not isinstance(ports, list):
            raise ComposeError(f"服务 {spec.name} 的 ports 必须是列表")
        for entry in ports:
            target, published, protocol = _parse_port_entry(entry, spec.name)
            if protocol != "tcp":
                spec.notices.append(f"已忽略非 TCP 端口：{entry}")
                continue
            if target is None:
                continue
            spec.published_ports.append(target)
            if published is not None:
                spec.notices.append(
                    f"compose 中的宿主端口 {published} 由平台重新分配，避免与宿主冲突"
                )

    expose = service.get("expose")
    if expose is not None:
        if not isinstance(expose, list):
            raise ComposeError(f"服务 {spec.name} 的 expose 必须是列表")
        for entry in expose:
            port = _to_port(entry)
            if port is not None:
                spec.exposed_ports.append(port)

    x_port = _x_paas_port(spec.x_paas)
    if x_port is not None:
        spec.container_port = x_port
    elif spec.published_ports:
        unique = sorted(set(spec.published_ports))
        spec.container_port = unique[0]
        if len(unique) > 1:
            spec.notices.append(
                f"compose 暴露了多个端口 {unique}，平台仅对外发布 {unique[0]}"
            )
    elif spec.exposed_ports:
        spec.container_port = spec.exposed_ports[0]
    elif spec.build_context and spec.build_dockerfile:
        df_port = _port_from_dockerfile(spec.build_context, spec.build_dockerfile)
        if df_port is not None:
            spec.container_port = df_port
            spec.notices.append(
                f"compose 未声明端口，容器端口取自 Dockerfile 的 EXPOSE {df_port}"
            )


def _parse_env(spec: ComposeServiceSpec, service: dict, src: Path) -> None:
    env: dict[str, str] = {}

    env_files = service.get("env_file")
    if env_files is not None:
        entries = [env_files] if isinstance(env_files, str) else env_files
        if not isinstance(entries, list):
            raise ComposeError(f"服务 {spec.name} 的 env_file 必须是字符串或列表")
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                raise ComposeError(f"服务 {spec.name} 的 env_file 路径不合法")
            path = _resolve_env_file(src, entry, spec.name)
            spec.env_files.append(path)
            env.update(_read_env_file(path, spec.name))

    raw = service.get("environment")
    if raw is not None:
        if isinstance(raw, dict):
            items = raw.items()
        elif isinstance(raw, list):
            pairs = []
            for item in raw:
                if not isinstance(item, str):
                    raise ComposeError(f"服务 {spec.name} 的 environment 列表项必须是字符串")
                if "=" not in item:
                    spec.notices.append(f"环境变量 {item} 未设置值，已跳过")
                    continue
                key, value = item.split("=", 1)
                pairs.append((key, value))
            items = pairs
        else:
            raise ComposeError(f"服务 {spec.name} 的 environment 必须是映射或列表")

        for key, value in items:
            key = str(key)
            if value is None:
                spec.notices.append(f"环境变量 {key} 未设置值，已跳过")
                continue
            if not _ENV_KEY_RE.match(key):
                raise ComposeError(
                    f"服务 {spec.name} 的环境变量名不合法：{key}"
                    "（仅允许字母/数字/下划线，且不以数字开头）"
                )
            text = str(value)
            if len(text) > _ENV_MAX_VALUE:
                raise ComposeError(
                    f"服务 {spec.name} 的环境变量 {key} 值过长（上限 {_ENV_MAX_VALUE} 字符）"
                )
            env[key] = text

    if len(env) > _ENV_MAX_ITEMS:
        raise ComposeError(f"服务 {spec.name} 的环境变量最多 {_ENV_MAX_ITEMS} 项")
    spec.environment = env


def _resolve_env_file(src: Path, raw: str, service: str) -> Path:
    base = src.resolve()
    path = (base / raw).resolve()
    if base != path and base not in path.parents:
        raise ComposeError(f"服务 {service} 的 env_file 超出源码目录：{raw}")
    if not path.is_file():
        raise ComposeError(f"服务 {service} 的 env_file 不存在：{raw}")
    return path


def _read_env_file(path: Path, service: str) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        raise ComposeError(f"服务 {service} 的 env_file 读取失败：{exc}") from exc
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].strip()
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if _ENV_KEY_RE.match(key):
            result[key] = value
    return result


def _parse_command(spec: ComposeServiceSpec, service: dict) -> None:
    command = service.get("command")
    if command is not None:
        if isinstance(command, str):
            spec.command = command.strip() or None
        elif isinstance(command, list):
            spec.command = [str(item) for item in command]
        else:
            raise ComposeError(f"服务 {spec.name} 的 command 必须是字符串或列表")

    entrypoint = service.get("entrypoint")
    if entrypoint is not None:
        if isinstance(entrypoint, str):
            spec.entrypoint = entrypoint
        elif isinstance(entrypoint, list):
            spec.entrypoint = [str(item) for item in entrypoint]
        else:
            raise ComposeError(f"服务 {spec.name} 的 entrypoint 必须是字符串或列表")


def _parse_limits(spec: ComposeServiceSpec, service: dict) -> None:
    mem = service.get("mem_limit")
    cpu = service.get("cpus")
    deploy = service.get("deploy")
    if isinstance(deploy, dict):
        resources = deploy.get("resources")
        if isinstance(resources, dict):
            limits = resources.get("limits")
            if isinstance(limits, dict):
                mem = mem if mem is not None else limits.get("memory")
                cpu = cpu if cpu is not None else limits.get("cpus")
    if mem is not None:
        if isinstance(mem, bool):
            raise ComposeError(f"服务 {spec.name} 的 mem_limit 不合法")
        if isinstance(mem, int):
            spec.mem_limit = f"{mem}b"
        else:
            text = str(mem).strip()
            if not text:
                raise ComposeError(f"服务 {spec.name} 的 mem_limit 不能为空")
            spec.mem_limit = text
    if cpu is not None:
        try:
            value = float(cpu)
        except (TypeError, ValueError) as exc:
            raise ComposeError(f"服务 {spec.name} 的 cpus 必须是数字：{cpu}") from exc
        if value <= 0:
            raise ComposeError(f"服务 {spec.name} 的 cpus 必须大于 0")
        spec.cpu_quota = value


def _parse_depends(spec: ComposeServiceSpec, service: dict) -> None:
    raw = service.get("depends_on")
    if raw is None:
        return
    if isinstance(raw, list):
        spec.depends_on = {str(name): "service_started" for name in raw}
        return
    if isinstance(raw, dict):
        for name, config in raw.items():
            condition = "service_started"
            if isinstance(config, dict):
                condition = str(config.get("condition") or "service_started")
            elif config is not None:
                condition = str(config)
            if condition not in ("service_started", "service_healthy"):
                raise ComposeError(
                    f"服务 {spec.name} 的 depends_on 条件暂不支持：{condition}"
                )
            spec.depends_on[str(name)] = condition
        return
    raise ComposeError(f"服务 {spec.name} 的 depends_on 必须是列表或映射")


def _parse_healthcheck(raw: Any, service: str) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ComposeError(f"服务 {service} 的 healthcheck 必须是映射")
    if raw.get("disable"):
        return None
    test = raw.get("test")
    if isinstance(test, str):
        test = ["CMD-SHELL", test]
    elif isinstance(test, list):
        test = [str(item) for item in test]
    else:
        raise ComposeError(f"服务 {service} 的 healthcheck.test 不合法")
    if test and str(test[0]).upper() == "NONE":
        return None

    result: dict[str, Any] = {"test": test}
    for key in ("interval", "timeout", "start_period"):
        if key in raw:
            ns = _duration_ns(raw.get(key), service)
            if ns is not None:
                result[key] = ns
    if "retries" in raw:
        try:
            result["retries"] = int(raw["retries"])
        except (TypeError, ValueError) as exc:
            raise ComposeError(f"服务 {service} 的 healthcheck.retries 不合法") from exc
    return result


def _parse_restart(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text


def _parse_volumes(spec: ComposeServiceSpec, service: dict, src: Path) -> None:
    raw = service.get("volumes")
    if raw is None:
        return
    if not isinstance(raw, list):
        raise ComposeError(f"服务 {spec.name} 的 volumes 必须是列表")
    for entry in raw:
        if isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) == 1:
                target = parts[0].strip()
                if not target.startswith("/"):
                    raise ComposeError(f"服务 {spec.name} 的卷目标必须是绝对路径：{entry}")
                spec.volumes.append(VolumeMount(target=target, anonymous=True))
                continue
            if len(parts) not in (2, 3):
                raise ComposeError(f"服务 {spec.name} 的卷格式不合法：{entry}")
            source = parts[0].strip()
            target = parts[1].strip()
            mode = parts[2].strip() if len(parts) == 3 else None
            if not target.startswith("/"):
                raise ComposeError(f"服务 {spec.name} 的卷目标必须是绝对路径：{entry}")
            if source.startswith(("/", ".", "~")) or ":" in source:
                raise ComposeError(
                    f"服务 {spec.name} 不允许挂载宿主目录：{entry}（仅支持命名卷）"
                )
            spec.volumes.append(VolumeMount(source=source, target=target, mode=mode))
            continue

        if isinstance(entry, dict):
            mount_type = str(entry.get("type") or "volume")
            target = str(entry.get("target") or "")
            if not target.startswith("/"):
                raise ComposeError(f"服务 {spec.name} 的卷目标必须是绝对路径：{entry}")
            if mount_type == "bind":
                raise ComposeError(
                    f"服务 {spec.name} 不允许挂载宿主目录：{entry}（仅支持命名卷）"
                )
            if mount_type == "tmpfs":
                spec.volumes.append(VolumeMount(target=target, tmpfs=True))
                continue
            if mount_type != "volume":
                raise ComposeError(f"服务 {spec.name} 的卷类型不支持：{mount_type}")
            source = entry.get("source")
            read_only = bool(entry.get("read_only"))
            mode = "ro" if read_only else None
            if source:
                spec.volumes.append(
                    VolumeMount(source=str(source), target=target, mode=mode)
                )
            else:
                spec.volumes.append(VolumeMount(target=target, anonymous=True))
            continue

        raise ComposeError(f"服务 {spec.name} 的 volumes 条目不合法：{entry}")


def _parse_networks(spec: ComposeServiceSpec, service: dict) -> None:
    raw = service.get("networks")
    if raw is None:
        return
    if isinstance(raw, list):
        spec.networks = [str(item) for item in raw]
        return
    if isinstance(raw, dict):
        spec.networks = [str(item) for item in raw]
        return
    raise ComposeError(f"服务 {spec.name} 的 networks 必须是列表或映射")


def _parse_scalars(spec: ComposeServiceSpec, service: dict) -> None:
    working_dir = service.get("working_dir")
    if working_dir is not None:
        spec.working_dir = str(working_dir)
    user = service.get("user")
    if user is not None:
        spec.user = str(user)
    spec.read_only = bool(service.get("read_only") or False)
    init = service.get("init")
    spec.init = bool(init) if init is not None else None
    tty = service.get("tty")
    spec.tty = bool(tty) if tty is not None else None
    stdin_open = service.get("stdin_open")
    spec.stdin_open = bool(stdin_open) if stdin_open is not None else None
    stop_signal = service.get("stop_signal")
    if stop_signal is not None:
        spec.stop_signal = str(stop_signal)
    if service.get("stop_grace_period") is not None:
        spec.notices.append("字段 stop_grace_period 未生效：停止超时由平台统一设置")

    labels = service.get("labels")
    if isinstance(labels, dict):
        spec.labels = {str(k): str(v) for k, v in labels.items()}
    elif isinstance(labels, list):
        parsed = {}
        for item in labels:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                parsed[key] = value
        spec.labels = parsed
    elif labels is not None:
        raise ComposeError(f"服务 {spec.name} 的 labels 格式不合法")


def _select_public(plan: ComposePlan, top_x: dict) -> None:
    """解析公开服务集合：主入口保持旧域名，其余公开服务获得独立子域名。"""
    public_raw = top_x.get("public")
    primary_raw = top_x.get("primary")
    explicit: list[str] = []
    if isinstance(public_raw, (list, tuple)):
        explicit = [str(item) for item in public_raw]
    elif public_raw is not None:
        explicit = [str(public_raw)]
    if primary_raw is not None:
        primary_name = str(primary_raw)
        if primary_name not in explicit:
            explicit.insert(0, primary_name)

    flagged = [
        spec.name
        for spec in plan.services
        if spec.x_paas.get("public") is True or spec.x_paas.get("primary") is True
    ]

    if explicit:
        for name in explicit:
            if name not in plan.service_map:
                raise ComposeError(f"x-paas.public 指向不存在的服务：{name}")
        duplicated = [name for name in set(explicit) if explicit.count(name) > 1]
        if duplicated:
            raise ComposeError(f"x-paas.public 重复声明服务：{', '.join(duplicated)}")
        for name in flagged:
            if name not in explicit:
                raise ComposeError(
                    f"服务 {name} 声明了 x-paas.public，但顶层 x-paas.public 未包含它"
                )
        extras = [
            spec.name
            for spec in plan.services
            if spec.published_ports and spec.name not in explicit
        ]
        if extras:
            raise ComposeError(
                f"服务 {', '.join(extras)} 声明了 ports，但未被 x-paas.public 收录；"
                "请将其加入公开服务列表或移除 ports"
            )
        public_names = explicit
    elif flagged:
        extras = [
            spec.name
            for spec in plan.services
            if spec.published_ports and spec.name not in flagged
        ]
        if extras:
            raise ComposeError(
                f"服务 {', '.join(extras)} 声明了 ports，但未被 x-paas.public 标记；"
                "请补充标记或移除 ports"
            )
        public_names = flagged
    else:
        public_names = [spec.name for spec in plan.services if spec.published_ports]
        if len(plan.services) == 1:
            public_names = [plan.services[0].name]
        elif not public_names:
            raise ComposeError(
                "多服务 compose 至少需要一个带 ports 的公开服务，"
                "或用 x-paas.public 指定"
            )

    # 去重并保持声明顺序
    ordered: list[str] = []
    for name in public_names:
        if name not in ordered:
            ordered.append(name)
    public_names = ordered

    if primary_raw is not None:
        primary = str(primary_raw)
    elif isinstance(public_raw, str):
        primary = public_raw
    else:
        primary = public_names[0]
    if primary not in public_names:
        raise ComposeError(f"主入口 {primary} 必须包含在公开服务中")

    for spec in plan.services:
        spec.is_public = spec.name in public_names
        spec.is_primary = spec.name == primary

    plan.public_services = public_names
    plan.public_service = primary

    public_spec = plan.get(primary)
    for spec in plan.publics:
        if spec.container_port is not None:
            continue
        if spec is public_spec:
            spec.notices.append("公开服务未声明端口，沿用新建应用时填写的容器端口")
        else:
            raise ComposeError(
                f"公开服务 {spec.name} 未声明 ports / expose / x-paas.port，无法分配访问端口"
            )

    x_port = _x_paas_port(public_spec.x_paas) or _x_paas_port(top_x)
    if x_port is not None:
        if public_spec.container_port and x_port != public_spec.container_port:
            plan.notices.append(
                f"x-paas.port={x_port} 覆盖主入口服务的容器端口 {public_spec.container_port}"
            )
        public_spec.container_port = x_port

    plan.public_services = public_names
    plan.public_service = primary
    if len(public_names) > 1:
        plan.notices.append(
            f"检测到 {len(public_names)} 个公开服务：{', '.join(public_names)}（主入口 {primary}）"
        )


def _validate_depends(plan: ComposePlan) -> None:
    names = set(plan.service_map)
    for spec in plan.services:
        for dep in spec.depends_on:
            if dep == spec.name:
                raise ComposeError(f"服务 {spec.name} 不能依赖自身")
            if dep not in names:
                raise ComposeError(f"服务 {spec.name} 依赖不存在的服务：{dep}")
    plan.start_order()  # 触发循环依赖检测


def _single_mode_guard(plan: ComposePlan) -> None:
    """单服务仍走旧单容器链路，拒绝该链路无法表达的字段。"""
    if plan.is_multi:
        return
    spec = plan.services[0]
    unsupported = []
    if spec.entrypoint is not None:
        unsupported.append("entrypoint")
    if spec.volumes:
        unsupported.append("volumes")
    if spec.working_dir:
        unsupported.append("working_dir")
    if spec.user:
        unsupported.append("user")
    if spec.read_only:
        unsupported.append("read_only")
    if spec.init is not None:
        unsupported.append("init")
    if spec.tty is not None:
        unsupported.append("tty")
    if spec.stdin_open is not None:
        unsupported.append("stdin_open")
    if spec.stop_signal:
        unsupported.append("stop_signal")
    if unsupported:
        raise ComposeError(
            "单服务 compose 暂不支持 "
            + "、".join(unsupported)
            + "；如需使用这些字段，请拆成多服务或联系平台升级"
        )


def _network_notice(specs: list[ComposeServiceSpec]) -> list[str]:
    custom = [spec for spec in specs if spec.networks]
    if not custom:
        return []
    return ["服务自定义 networks 将合并为应用内网络，不保留多网络隔离"]


def _parse_port_entry(entry: Any, service: str) -> tuple[int | None, int | None, str]:
    if isinstance(entry, dict):
        protocol = str(entry.get("protocol") or "tcp").lower()
        target = _to_port(entry.get("target"))
        published = _to_port(entry.get("published"))
        return target, published, protocol

    text = str(entry).strip()
    protocol = "tcp"
    if "/" in text:
        text, protocol = text.rsplit("/", 1)
        protocol = protocol.lower()
    parts = text.split(":")
    target = _to_port(parts[-1])
    published = _to_port(parts[-2]) if len(parts) >= 2 else None
    return target, published, protocol


def _to_port(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 65535 else None
    text = str(value).strip()
    if "-" in text:
        raise ComposeError(f"暂不支持端口范围：{text}")
    if not text.isdigit():
        return None
    port = int(text)
    return port if 1 <= port <= 65535 else None


def _x_paas_port(value: Any) -> int | None:
    if not isinstance(value, dict) or "port" not in value:
        return None
    return _to_port(value.get("port"))


def _port_from_dockerfile(context: Path, dockerfile: str) -> int | None:
    path = (context / dockerfile).resolve()
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for line in text.splitlines():
        match = re.match(r"\s*EXPOSE\s+(.+)", line, re.IGNORECASE)
        if not match:
            continue
        for token in match.group(1).split():
            token = token.split("/")[0]
            if token.isdigit() and 1 <= int(token) <= 65535:
                return int(token)
    return None


def _duration_ns(value: Any, service: str) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    matches = _DURATION_RE.findall(text)
    if not matches:
        try:
            return int(float(text) * 1_000_000_000)
        except ValueError as exc:
            raise ComposeError(f"服务 {service} 的时长格式不合法：{value}") from exc
    total = 0.0
    for number, unit in matches:
        total += float(number) * _DURATION_UNITS[unit]
    return int(total)


