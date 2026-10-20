"""容器运行时服务：基于 docker-py 封装容器全生命周期管理。

M2 能力：
- 创建 / 启动 / 停止 / 重启 / 删除容器
- 资源限制：内存（mem_limit）、CPU（nano_cpus）
- 统一 label 标记归属，便于按平台维度查询
"""

from __future__ import annotations

import contextlib
import os
import socket
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from app.core.config import settings

# 平台托管容器统一标签（用于隔离"平台创建"与"宿主机其它容器"）
LABEL_MANAGED = "paas.managed"
LABEL_APP_ID = "paas.app_id"
LABEL_APP_NAME = "paas.app_name"

# 指向一个"空凭证"的 docker 配置目录，规避宿主机凭据助手差异
_NEUTRAL_DOCKER_CONFIG = Path(tempfile.gettempdir()) / "paas_docker_config"


def _neutral_docker_config() -> str:
    """返回一个干净的 docker 配置目录，并让 docker-py 使用它。

    平台起容器 / 构建镜像都不依赖私有仓库凭据，而宿主机 ~/.docker/config.json
    若声明了 credsStore（如 Docker Desktop 的 desktop.exe），在 WSL / Linux 下
    调用凭据助手会直接报错，导致 build 失败。统一指向空配置即可规避。
    """
    _NEUTRAL_DOCKER_CONFIG.mkdir(parents=True, exist_ok=True)
    cfg = _NEUTRAL_DOCKER_CONFIG / "config.json"
    if not cfg.exists():
        cfg.write_text("{}", encoding="utf-8")
    return str(_NEUTRAL_DOCKER_CONFIG)


class DockerUnavailableError(RuntimeError):
    """Docker Daemon 不可用。"""


class ContainerNotFoundError(RuntimeError):
    """容器不存在。"""


class PortConflictError(RuntimeError):
    """宿主端口被占用，需重新分配端口后重试。"""


@dataclass
class DockerStatus:
    available: bool
    server_version: str | None = None
    api_version: str | None = None
    containers_running: int = 0
    images: int = 0
    error: str | None = None


@dataclass
class ContainerInfo:
    """容器状态快照，供 API 层序列化。"""

    id: str
    name: str
    image: str
    status: str
    state: str
    app_id: int | None = None
    app_name: str | None = None
    host_port: int | None = None
    container_port: int | None = None
    mem_limit: int | None = None
    nano_cpus: int | None = None
    started_at: str | None = None
    labels: dict[str, str] = field(default_factory=dict)


@contextlib.contextmanager
def client(timeout: int = 10) -> Iterator[docker.DockerClient]:
    """获取 Docker 客户端（上下文管理器，确保连接释放）。"""
    # 必须在构造客户端前生效：docker-py 初始化时会读取该环境变量加载 config.json
    os.environ.setdefault("DOCKER_CONFIG", _neutral_docker_config())
    cli = docker.DockerClient(base_url=settings.DOCKER_HOST, timeout=timeout)
    try:
        yield cli
    finally:
        cli.close()


def probe() -> DockerStatus:
    """探测 Docker Daemon 是否可用，用于健康检查接口。"""
    try:
        with client() as cli:
            version = cli.version()
            info = cli.info()
            return DockerStatus(
                available=True,
                server_version=version.get("Version"),
                api_version=version.get("ApiVersion"),
                containers_running=info.get("ContainersRunning", 0),
                images=info.get("Images", 0),
            )
    except DockerException as exc:
        return DockerStatus(available=False, error=str(exc))
    except Exception as exc:  # 兜底，避免健康检查因环境问题直接 500
        return DockerStatus(available=False, error=f"{type(exc).__name__}: {exc}")


def container_name_for(app_id: int, name: str) -> str:
    """容器命名规范：paas-{app_id}-{name}。"""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-")
    return f"paas-{app_id}-{safe}"[:120]


def ensure_network() -> str | None:
    """确保内网隔离网桥存在（M7）。

    所有受管容器接入专用 bridge 网络，并关闭容器间互通（enable_icc=false），
    使不同应用的容器彼此网络隔离，仅能经宿主端口 / Nginx 对外暴露。
    开关关闭时返回 None（容器走默认 bridge）。
    """
    if not settings.ENABLE_NETWORK_ISOLATION:
        return None
    name = settings.PAAS_NETWORK_NAME
    try:
        with client() as cli:
            existing = cli.networks.list(names=[name])
            if existing:
                return name
            cli.networks.create(
                name,
                driver="bridge",
                check_duplicate=True,
                options={
                    # 关闭容器间互通：应用之间默认不可直接访问
                    "com.docker.network.bridge.enable_icc": "false",
                    "com.docker.network.bridge.name": "br-paas",
                },
                labels={LABEL_MANAGED: "true"},
            )
            return name
    except DockerException:
        # 网络创建失败不阻塞部署：降级为默认网络（记录交给调用方容忍）
        return None


def _build_labels(app_id: int, app_name: str) -> dict[str, str]:
    return {
        LABEL_MANAGED: "true",
        LABEL_APP_ID: str(app_id),
        LABEL_APP_NAME: app_name,
    }


def _to_info(container: Container) -> ContainerInfo:
    """将 docker-py 的 Container 转为轻量快照。"""
    container.reload()
    attrs = container.attrs or {}
    host_config = attrs.get("HostConfig", {}) or {}
    net_ports = (attrs.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
    labels = attrs.get("Config", {}).get("Labels", {}) or {}

    host_port: int | None = None
    container_port: int | None = None
    for cport, bindings in net_ports.items():
        container_port = int(cport.split("/")[0])
        if bindings:
            host_port = int(bindings[0]["HostPort"])
        break

    mem_limit = host_config.get("Memory") or None
    nano_cpus = host_config.get("NanoCpus") or None
    # 在线 update 走 CFS：NanoCpus 可能为 0，此时用 CpuQuota/CpuPeriod 换算回 nano 口径
    if not nano_cpus:
        cfs_quota = host_config.get("CpuQuota") or 0
        cfs_period = host_config.get("CpuPeriod") or 0
        if cfs_quota and cfs_period:
            nano_cpus = int(cfs_quota / cfs_period * 1_000_000_000)
    app_id_raw = labels.get(LABEL_APP_ID)

    return ContainerInfo(
        id=container.id[:12],
        name=container.name,
        image=(attrs.get("Config", {}) or {}).get("Image", ""),
        status=container.status,
        state=(attrs.get("State", {}) or {}).get("Status", container.status),
        app_id=int(app_id_raw) if app_id_raw and app_id_raw.isdigit() else None,
        app_name=labels.get(LABEL_APP_NAME),
        host_port=host_port,
        container_port=container_port,
        mem_limit=mem_limit,
        nano_cpus=nano_cpus,
        started_at=(attrs.get("State", {}) or {}).get("StartedAt"),
        labels=labels,
    )


def run_container(
    *,
    app_id: int,
    app_name: str,
    image: str,
    container_port: int,
    host_port: int,
    mem_limit: str | None = None,
    cpu_quota: float | None = None,
    env: dict[str, str] | None = None,
    command: list[str] | str | None = None,
) -> ContainerInfo:
    """创建并启动一个受管容器。

    - 端口映射：host_port -> container_port
    - 资源限制：mem_limit（如 "256m"）、cpu_quota（1.0 = 1 核）
    - 打上平台标签，便于后续按应用维度检索

    CPU 用 CFS 的 cpu_period + cpu_quota 表达，而非 nano_cpus：
    两者互斥，且只有 CFS 口径能在容器运行期通过 /update 在线调整。
    """
    mem = mem_limit or settings.DEFAULT_MEM_LIMIT
    cpus = cpu_quota if cpu_quota is not None else settings.DEFAULT_CPU_QUOTA
    period = 100_000
    quota = int(cpus * period)

    kwargs: dict = {
        "image": image,
        "name": container_name_for(app_id, app_name),
        "detach": True,
        "ports": {f"{container_port}/tcp": host_port},
        "mem_limit": mem,
        # M7：memswap_limit == mem_limit 即禁用 swap（swap 额度为 0）。
        # cgroup v2 已移除 memory.swappiness，靠该口径使内存上限成为硬约束。
        "memswap_limit": mem,
        "cpu_period": period,
        "cpu_quota": quota,
        "labels": _build_labels(app_id, app_name),
        "restart_policy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
    }
    # M7：容器运行时加固（no-new-privileges / cap_drop / pids_limit / 禁 swap）
    from app.services import security_service

    kwargs.update(security_service.container_security_kwargs())
    # M7：接入内网隔离网桥（容器间默认不可互通）
    network = ensure_network()
    if network:
        kwargs["network"] = network
    if env:
        kwargs["environment"] = env
    if command:
        kwargs["command"] = command

    try:
        with client() as cli:
            container = cli.containers.run(**kwargs)
            return _to_info(container)
    except ImageNotFound as exc:
        raise DockerUnavailableError(f"镜像不存在：{image}（请先 docker pull）") from exc
    except APIError as exc:
        # 端口被占用：抛专用异常，交由上层换端口重试
        msg = str(exc).lower()
        if "port is already allocated" in msg or "address already in use" in msg:
            # 清理可能已创建但未启动成功的同名容器，避免下次 409 冲突
            with contextlib.suppress(Exception):
                remove(kwargs["name"], force=True)
            raise PortConflictError(f"宿主端口 {host_port} 已被占用") from exc
        raise DockerUnavailableError(f"容器创建失败：{exc}") from exc
    except DockerException as exc:
        raise DockerUnavailableError(f"容器创建失败：{exc}") from exc


def _get_container(cli: docker.DockerClient, ref: str) -> Container:
    try:
        return cli.containers.get(ref)
    except NotFound as exc:
        raise ContainerNotFoundError(f"容器不存在：{ref}") from exc


def stream_logs(ref: str, tail: int = 200) -> Iterator[str]:
    """流式读取容器 stdout/stderr（M5 WebSocket 日志推送）。

    - tail 条为历史回放，之后 follow 实时追加（docker logs 原生行为）；
    - 生成器生命周期覆盖 client 连接，容器停止/被删时流自然结束；
    - timeout=None：长连接永不超时，由消费端控制断开。
    - 注：docker-py 7.x 高层 Container.logs() 无 decode 参数，需手动解码。
    """
    with client(timeout=None) as cli:
        container = _get_container(cli, ref)
        try:
            for chunk in container.logs(stream=True, follow=True, tail=tail):
                if chunk:
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode("utf-8", errors="replace")
                    yield chunk
        except (NotFound, DockerException):
            # 容器被销毁或连接中断：视为流结束
            return


def get(ref: str) -> ContainerInfo:
    with client() as cli:
        return _to_info(_get_container(cli, ref))


def inspect_state(ref: str) -> dict:
    """读取容器的详细运行态（供一键诊断使用）。

    相比 ContainerInfo，这里补充了退出码 / 错误信息 / 重启次数 / OOM 标记，
    用于解释"容器为什么没跑起来"这类排障问题。
    """
    with client() as cli:
        container = _get_container(cli, ref)
        attrs = container.attrs or {}
        state = attrs.get("State", {}) or {}
        return {
            "status": container.status,
            "state": state.get("Status"),
            "running": bool(state.get("Running")),
            "exit_code": state.get("ExitCode"),
            "error": (state.get("Error") or "").strip(),
            "oom_killed": bool(state.get("OOMKilled")),
            "restart_count": int(attrs.get("RestartCount") or 0),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
        }


def tail_logs(ref: str, tail: int = 20) -> list[str]:
    """读取容器最近若干行日志（非跟随），供诊断摘要展示。"""
    with client() as cli:
        container = _get_container(cli, ref)
        raw = container.logs(tail=max(tail, 1), stream=False)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return [line for line in raw.splitlines() if line.strip()]


def start(ref: str) -> ContainerInfo:
    with client() as cli:
        container = _get_container(cli, ref)
        container.start()
        return _to_info(container)


def stop(ref: str, timeout: int = 10) -> ContainerInfo:
    with client() as cli:
        container = _get_container(cli, ref)
        container.stop(timeout=timeout)
        return _to_info(container)


def restart(ref: str, timeout: int = 10) -> ContainerInfo:
    with client() as cli:
        container = _get_container(cli, ref)
        container.restart(timeout=timeout)
        return _to_info(container)


def remove(ref: str, force: bool = True) -> None:
    """删除容器（默认强制删除运行中的容器）。"""
    with client() as cli:
        container = _get_container(cli, ref)
        container.remove(force=force)


def update_resources(ref: str, mem_limit: str, cpu_quota: float) -> ContainerInfo:
    """在线调整资源配额（Docker 支持不停机更新 cgroup 限制）。

    注意：`Container.update()` 走的是 /containers/{id}/update 接口，
    该接口用 CFS 的 `cpu_period` + `cpu_quota` 表达 CPU 上限，
    而不是创建时的 `nano_cpus`（1 核 = 1e9 只在 create 生效）。
    cpu_quota(µs) = 核数 × cpu_period，默认 period = 100000µs = 100ms。
    """
    period = 100_000
    quota = int(cpu_quota * period)
    with client() as cli:
        container = _get_container(cli, ref)
        container.update(cpu_period=period, cpu_quota=quota, mem_limit=mem_limit)
        return _to_info(container)


def list_managed(all_states: bool = True) -> list[ContainerInfo]:
    """列出所有由本平台托管的容器。"""
    with client() as cli:
        containers = cli.containers.list(
            all=all_states, filters={"label": f"{LABEL_MANAGED}=true"}
        )
        return [_to_info(c) for c in containers]


def stats(ref: str) -> dict:
    """获取容器瞬时资源占用（M5 监控中心会在此基础上做定时采样）。"""
    with client() as cli:
        container = _get_container(cli, ref)
        raw = container.stats(stream=False)
        return _parse_stats(raw)


def _parse_stats(raw: dict) -> dict:
    """解析 docker stats 原始结构，计算 CPU 百分比与内存占用。"""
    cpu_percent = 0.0
    try:
        cpu_stats = raw.get("cpu_stats", {}) or {}
        pre_cpu = raw.get("precpu_stats", {}) or {}
        cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - pre_cpu.get(
            "cpu_usage", {}
        ).get("total_usage", 0)
        system_delta = cpu_stats.get("system_cpu_usage", 0) - pre_cpu.get("system_cpu_usage", 0)
        online_cpus = cpu_stats.get("online_cpus") or len(
            cpu_stats.get("cpu_usage", {}).get("percpu_usage", []) or [1]
        )
        if system_delta > 0 and cpu_delta > 0:
            cpu_percent = (cpu_delta / system_delta) * online_cpus * 100.0
    except Exception:
        cpu_percent = 0.0

    mem_usage = 0
    mem_limit = 0
    cache = 0
    try:
        mem = raw.get("memory_stats", {}) or {}
        mem_usage = mem.get("usage", 0)
        mem_limit = mem.get("limit", 0)
        cache = (mem.get("stats", {}) or {}).get("inactive_file", 0)
    except Exception:
        pass

    # 网络累计收发字节：累加所有网络接口（容器通常仅 eth0 / bridge）
    net_rx = 0
    net_tx = 0
    try:
        for iface in (raw.get("networks") or {}).values():
            net_rx += int(iface.get("rx_bytes", 0) or 0)
            net_tx += int(iface.get("tx_bytes", 0) or 0)
    except Exception:
        pass

    return {
        "cpu_percent": round(cpu_percent, 2),
        "mem_usage": mem_usage,
        "mem_limit": mem_limit,
        "mem_percent": round((mem_usage - cache) / mem_limit * 100, 2) if mem_limit else 0.0,
        "net_rx": net_rx,
        "net_tx": net_tx,
    }


# ---------------------------------------------------------------------------
# M3：镜像构建
# ---------------------------------------------------------------------------


@dataclass
class BuildResult:
    """一次 docker build 的结果。"""

    success: bool
    image_tag: str
    error: str | None = None
    log_lines: int = 0


def build_image(
    *,
    context_dir: str,
    image_tag: str,
    dockerfile: str = "Dockerfile",
    on_log: Callable[[str], None] | None = None,
    timeout: int | None = None,
) -> BuildResult:
    """在指定上下文目录执行 docker build，并逐行回调构建日志。

    - context_dir：构建上下文（源码目录）
    - image_tag  ：构建产物镜像标签，如 paas/app-1:latest
    - on_log     ：每产生一行输出即回调，用于实时回传前端 / 落库
    """
    limit = timeout or settings.BUILD_TIMEOUT
    collected: list[str] = []

    def _emit(line: str) -> None:
        collected.append(line)
        if on_log:
            on_log(line)

    try:
        with client(timeout=limit) as cli:
            # 低层 API 才能拿到流式输出（build 的 stream 需要 decode + json 解析）
            stream = cli.api.build(
                path=context_dir,
                dockerfile=dockerfile,
                tag=image_tag,
                rm=True,
                forcerm=True,
                decode=True,
            )
            for chunk in stream:
                if "stream" in chunk:
                    _emit(chunk["stream"].rstrip("\n"))
                elif "error" in chunk:
                    _emit(f"[ERROR] {chunk['error'].rstrip()}")
                    return BuildResult(False, image_tag, chunk["error"].strip(), len(collected))
                elif "status" in chunk:
                    text = chunk["status"]
                    if chunk.get("progress"):
                        text = f"{text} {chunk['progress']}"
                    _emit(text)
    except DockerException as exc:
        _emit(f"[ERROR] {exc}")
        return BuildResult(False, image_tag, str(exc), len(collected))

    return BuildResult(True, image_tag, None, len(collected))


def remove_image(image_tag: str, force: bool = True) -> None:
    """删除构建产物镜像（构建失败清理 / 应用销毁时使用）。"""
    try:
        with client() as cli:
            cli.images.remove(image=image_tag, force=force)
    except NotFound:
        pass
    except DockerException:
        pass


def image_exists(image_tag: str) -> bool:
    """判断本地是否仍存在指定镜像（供历史版本回滚前校验）。"""
    if not image_tag:
        return False
    try:
        with client() as cli:
            cli.images.get(image_tag)
        return True
    except ImageNotFound:
        return False
    except DockerException:
        # Daemon 不可用等异常交由调用方的部署流程处理，这里只回答"镜像是否在"
        return True


# ---------------------------------------------------------------------------
# Web 终端：交互式 docker exec 会话（TTY + stdin 双向流）
# ---------------------------------------------------------------------------

# 读取超时（秒）：recv 带超时轮询，保证会话关闭时读线程能自行退出
_EXEC_READ_TIMEOUT = 1.0


class ExecSession:
    """一次交互式 docker exec 会话（TTY）。

    生命周期由调用方（WebSocket 处理器）管理：创建 → 读写 → close()。
    注意：Docker 没有"结束 exec"的接口，仅关闭连接不会回收 exec 进程
    （实测容器内会残留 sh），因此 close() 必须先发 Ctrl+C 再发 exit 让登录 shell 自行退出。
    """

    def __init__(self, cli: docker.DockerClient, exec_id: str, raw) -> None:
        self._cli = cli
        self._exec_id = exec_id
        self._raw = raw
        self._sock = getattr(raw, "_sock", None) or raw
        self._sock.settimeout(_EXEC_READ_TIMEOUT)
        self._closed = False

    @property
    def exec_id(self) -> str:
        return self._exec_id

    def read(self) -> bytes:
        """阻塞读取一块输出（最多 4096 字节）。

        会话关闭、对端结束或无数据可读时返回 b""，调用方据此结束读取循环。
        """
        while not self._closed:
            try:
                data = self._sock.recv(4096)
            except (socket.timeout, TimeoutError):
                # 读超时属正常空转：继续等待，便于及时响应 close()
                continue
            except OSError:
                return b""
            if not data:
                return b""
            return data
        return b""

    def write(self, data: bytes) -> bool:
        """写入键盘输入；会话已关闭或写入失败返回 False。"""
        if self._closed:
            return False
        try:
            self._sock.sendall(data)
            return True
        except OSError:
            return False

    def resize(self, height: int, width: int) -> bool:
        """调整 TTY 尺寸（尽力而为：须在 exec_start 之后调用，失败只返回 False）。"""
        if self._closed:
            return False
        try:
            self._cli.api.exec_resize(self._exec_id, height=height, width=width)
            return True
        except DockerException:
            return False

    def close(self) -> None:
        """结束会话并释放连接（幂等）。"""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            # 先中断可能仍在跑的前台命令，再让登录 shell 退出，避免容器内残留进程
            self._sock.sendall(b"\x03")
            time.sleep(0.15)
            self._sock.sendall(b"exit\n")
            time.sleep(0.25)
        for obj in (self._raw, self._sock):
            with contextlib.suppress(Exception):
                obj.close()
        with contextlib.suppress(Exception):
            self._cli.close()


def open_shell(ref: str, shell: str = "/bin/sh") -> ExecSession:
    """在指定容器上打开一个交互式 TTY 会话（Web 终端用）。

    用 /bin/sh 而非 bash：精简镜像（alpine/busybox、demo 基础镜像）通常没有 bash，
    POSIX sh 则几乎必然存在。
    """
    os.environ.setdefault("DOCKER_CONFIG", _neutral_docker_config())
    cli = docker.DockerClient(base_url=settings.DOCKER_HOST, timeout=None)
    try:
        container = cli.containers.get(ref)
        # 容器须处于运行态，否则 exec 无法挂载 TTY
        container.reload()
        if container.status != "running":
            raise ContainerNotFoundError(f"容器未运行，无法打开终端：{ref}")
        exec_id = cli.api.exec_create(
            container.id, [shell], stdin=True, tty=True, user="root"
        )["Id"]
        raw = cli.api.exec_start(exec_id, tty=True, socket=True)
    except NotFound as exc:
        with contextlib.suppress(Exception):
            cli.close()
        raise ContainerNotFoundError(f"容器不存在：{ref}") from exc
    except ContainerNotFoundError:
        with contextlib.suppress(Exception):
            cli.close()
        raise
    except DockerException as exc:
        with contextlib.suppress(Exception):
            cli.close()
        raise DockerUnavailableError(f"终端会话创建失败：{exc}") from exc
    return ExecSession(cli, exec_id, raw)
