"""一键诊断服务：对单个应用做一次端到端体检，输出结构化检查清单。

设计要点：
- 逐项独立 try/except：任一检查抛错只标记该项为 fail，不中断其余检查；
- 全部为只读探测（不启动 / 不重建 / 不改配置），可安全反复触发；
- 面向排障：结论给"问题在哪 + 怎么处理"，把处置入口交回控制台。
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import App
from app.services import app_service, docker_service, route_service

# 检查项状态：通过 / 提示 / 失败 / 跳过
OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

# 从 Nginx conf 中提取 upstream 端口（与 route_service.render_server_block 保持一致）
_PROXY_PASS_RE = re.compile(r"proxy_pass\s+http://127\.0\.0\.1:(\d+)")


@dataclass
class Check:
    """单个检查项的结论。"""

    key: str
    label: str
    status: str
    detail: str
    hint: str | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
        }


def _add(checks: list[Check], key: str, label: str, status: str, detail: str, hint: str | None = None) -> None:
    checks.append(Check(key=key, label=label, status=status, detail=detail, hint=hint))


# ---------------------------------------------------------------------------
# 各检查项
# ---------------------------------------------------------------------------


def _check_app_state(app: App, checks: list[Check]) -> None:
    """① 应用记录与生命周期状态。"""
    status = app.status or "created"
    if status == "failed":
        _add(checks, "app_state", "应用状态", FAIL, "状态为 failed（上次构建或部署失败）", "重新构建并部署")
    elif status == "running":
        _add(checks, "app_state", "应用状态", OK, "状态为 running（容器应处于运行中）")
    elif status == "building":
        _add(checks, "app_state", "应用状态", WARN, "状态为 building（构建进行中）", "等待构建结束后重新诊断")
    else:
        _add(checks, "app_state", "应用状态", WARN, f"状态为 {status}（尚未运行）", "点击部署启动容器")


def _check_source(app: App, checks: list[Check]) -> None:
    """② 源码目录：是否存在、是否为空、是否自带 Dockerfile。"""
    if not app.source_path:
        _add(checks, "source", "源码目录", FAIL, "尚未上传源码或配置 Git 仓库", "在详情页上传代码包或配置 Git 源")
        return
    src = Path(app.source_path)
    if not src.exists() or not src.is_dir():
        _add(checks, "source", "源码目录", FAIL, f"源码目录不存在：{app.source_path}", "重新上传源码（原目录可能已被清理）")
        return
    try:
        entries = [p.name for p in src.iterdir()]
    except OSError as exc:
        _add(checks, "source", "源码目录", FAIL, f"源码目录无法读取：{exc}", "检查工作区目录权限")
        return
    if not entries:
        _add(checks, "source", "源码目录", WARN, f"源码目录为空：{app.source_path}", "重新上传完整的代码包")
        return
    dockerfile = "自带 Dockerfile" if "Dockerfile" in entries else "将按项目类型自动生成 Dockerfile"
    _add(checks, "source", "源码目录", OK, f"{len(entries)} 个顶层条目，{dockerfile}")


def _check_image(app: App, checks: list[Check]) -> None:
    """③ 构建产物镜像是否存在于本地。"""
    if not app.image:
        _add(checks, "image", "镜像", FAIL, "尚未构建镜像", "先构建镜像再部署")
        return
    if docker_service.image_exists(app.image):
        _add(checks, "image", "镜像", OK, f"镜像存在：{app.image}")
    else:
        _add(checks, "image", "镜像", FAIL, f"镜像已不存在：{app.image}", "重新构建并部署")


def _check_container(app: App, checks: list[Check]) -> bool:
    """④ 容器：是否存在、是否运行、异常时附退出码与最近日志。

    返回容器是否处于运行态，供后续端口探测决定是否跳过。
    """
    ref = docker_service.container_name_for(app.id, app.name)
    try:
        state = docker_service.inspect_state(ref)
    except docker_service.ContainerNotFoundError:
        _add(checks, "container", "容器", FAIL, "容器不存在", "部署应用以创建容器")
        return False
    except docker_service.DockerUnavailableError as exc:
        _add(checks, "container", "容器", FAIL, f"Docker 不可用：{exc}", "确认 Docker Daemon 正在运行")
        return False

    if state["running"]:
        extra = f"，重启次数 {state['restart_count']}" if state["restart_count"] else ""
        _add(checks, "container", "容器", OK, f"运行中（{ref}）{extra}")
        return True

    # 未运行：给出可解释的原因，并附最近日志尾部
    code = state["exit_code"]
    oom = "（疑似内存超限被 OOM 终止）" if state["oom_killed"] else ""
    detail = f"未运行：state={state['state']}，退出码 {code}{oom}"
    if state["error"]:
        detail += f"，错误信息：{state['error']}"
    try:
        tail = docker_service.tail_logs(ref, tail=10)
        if tail:
            detail += "；最近日志：" + " | ".join(tail[-5:])
    except Exception:
        pass
    _add(checks, "container", "容器", FAIL, detail, "查看完整日志并重启，或调整启动命令")
    return False


def _check_services(app: App, checks: list[Check]) -> bool:
    """④ 容器检查：多服务 compose 逐服务分组，单容器应用沿用整体检查。

    返回公开服务是否运行，供后续端口探测决定是否跳过。
    """
    services = list(app.services)
    if len(services) <= 1:
        return _check_container(app, checks)

    public_running = False
    for service in services:
        key = f"container:{service.name}"
        label = f"服务 {service.name}" + ("（公开）" if service.is_public else "（内部）")
        try:
            _svc, ref = app_service.resolve_service_container(app, service.name)
        except app_service.AppError:
            ref = service.container_name or ""

        if service.image and not docker_service.image_exists(service.image):
            _add(checks, key, label, FAIL, f"镜像不存在：{service.image}", "重新构建并部署")
            continue
        if not ref:
            _add(checks, key, label, FAIL, "服务尚未分配容器", "重新部署应用以创建容器")
            continue

        try:
            state = docker_service.inspect_state(ref)
        except docker_service.ContainerNotFoundError:
            _add(checks, key, label, FAIL, "容器不存在", "重新部署应用以重建全部服务")
            continue
        except docker_service.DockerUnavailableError as exc:
            _add(checks, key, label, FAIL, f"Docker 不可用：{exc}", "确认 Docker Daemon 正在运行")
            continue

        if state["running"]:
            extra = f"，重启次数 {state['restart_count']}" if state["restart_count"] else ""
            _add(checks, key, label, OK, f"运行中（{ref}）{extra}")
            if service.is_public:
                public_running = True
            continue

        # 未运行：给出可解释的原因，并附最近日志尾部
        code = state["exit_code"]
        oom = "（疑似内存超限被 OOM 终止）" if state["oom_killed"] else ""
        detail = f"未运行：state={state['state']}，退出码 {code}{oom}"
        if state["error"]:
            detail += f"，错误信息：{state['error']}"
        try:
            tail = docker_service.tail_logs(ref, tail=10)
            if tail:
                detail += "；最近日志：" + " | ".join(tail[-5:])
        except Exception:
            pass
        _add(checks, key, label, FAIL, detail, "查看该服务日志后重启，或调整启动命令")
    return public_running


def _check_host_port(app: App, checks: list[Check], running: bool) -> None:
    """⑤ 宿主端口连通性（容器未运行时无法判断，跳过）。"""
    if not running:
        _add(checks, "host_port", "宿主端口", SKIP, "容器未运行，跳过端口探测")
        return
    if not app.host_port:
        _add(checks, "host_port", "宿主端口", FAIL, "未分配宿主端口", "重新部署以分配端口")
        return
    try:
        with socket.create_connection(("127.0.0.1", app.host_port), timeout=1.5):
            pass
        _add(checks, "host_port", "宿主端口", OK, f"127.0.0.1:{app.host_port} 可连接")
    except OSError as exc:
        _add(
            checks,
            "host_port",
            "宿主端口",
            WARN,
            f"127.0.0.1:{app.host_port} 暂不可连接：{exc}",
            "确认应用已在容器内监听该端口（容器端口 ≤> 宿主端口映射正确）",
        )


def _check_route(app: App, checks: list[Check]) -> None:
    """⑥ Nginx 路由配置与当前宿主端口一致性。"""
    if not settings.ENABLE_NGINX:
        _add(checks, "route", "反向代理路由", SKIP, "未启用 Nginx 动态路由（本地端口直连模式）")
        return
    path = route_service.conf_path(app.id)
    if not path.exists():
        _add(checks, "route", "反向代理路由", FAIL, f"路由配置缺失：{path}", "重新部署以生成路由")
        return
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        _add(checks, "route", "反向代理路由", FAIL, f"路由配置无法读取：{exc}", "检查 Nginx 配置目录权限")
        return

    if not app.host_port:
        _add(checks, "route", "反向代理路由", WARN, "应用未分配宿主端口，路由可能指向失效 upstream", "重新部署")
        return
    match = _PROXY_PASS_RE.search(content)
    if match is None:
        _add(checks, "route", "反向代理路由", FAIL, "路由配置未包含 proxy_pass，配置可能已损坏", "重新部署以重写路由")
        return
    upstream = int(match.group(1))
    domain = route_service.domain_for(app.subdomain)
    if upstream != app.host_port:
        _add(
            checks,
            "route",
            "反向代理路由",
            FAIL,
            f"upstream 端口不一致：路由指向 {upstream}，应用当前为 {app.host_port}",
            "重新部署以同步路由",
        )
        return
    if domain not in content:
        _add(checks, "route", "反向代理路由", WARN, f"路由 server_name 未包含当前域名：{domain}", "重新部署以同步域名")
        return
    _add(checks, "route", "反向代理路由", OK, f"{domain} → 127.0.0.1:{upstream}")


def _check_deployment(db: Session, app: App, checks: list[Check]) -> None:
    """⑦ 最近一次部署记录。"""
    deployment = app_service.latest_deployment(db, app.id)
    if deployment is None:
        _add(checks, "deployment", "最近部署", WARN, "尚无部署记录", "执行一次构建并部署")
        return
    build = deployment.build_status or "unknown"
    detail = f"#{deployment.id} {build}，镜像 {deployment.image_tag or '—'}，端口 {deployment.port or '—'}"
    if build == "failed":
        _add(checks, "deployment", "最近部署", FAIL, f"{detail}（构建失败）", "查看构建日志定位失败原因")
    elif build == "success":
        _add(checks, "deployment", "最近部署", OK, detail)
    else:
        _add(checks, "deployment", "最近部署", WARN, detail, "等待构建结束")


def _check_resources(app: App, checks: list[Check], running: bool) -> None:
    """⑧ 资源配额：记录值 vs 运行中容器的实际 cgroup 值。"""
    quota = f"内存 {app.mem_limit}，CPU {app.cpu_quota} 核"
    if not running:
        _add(checks, "resources", "资源配额", SKIP, f"{quota}（容器未运行，无法比对实际值）")
        return
    try:
        info = docker_service.get(docker_service.container_name_for(app.id, app.name))
    except Exception:
        _add(checks, "resources", "资源配额", SKIP, quota)
        return
    expected_nano = int(app.cpu_quota * 1_000_000_000)
    actual_nano = info.nano_cpus or 0
    if actual_nano and abs(actual_nano - expected_nano) > 10_000_000:
        _add(
            checks,
            "resources",
            "资源配额",
            WARN,
            f"运行中容器实际 CPU 配额 {actual_nano / 1e9:.2f} 核，与记录值 {app.cpu_quota} 核不一致",
            "重新部署以应用新配额",
        )
        return
    _add(checks, "resources", "资源配额", OK, quota)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def diagnose_app(db: Session, app_id: int, user=None) -> dict:
    """对应用执行一次完整诊断，返回结构化结论。

    归属校验复用 app_service.get_app（普通用户不可探测他人应用）。
    """
    app = app_service.get_app(db, app_id, user)

    checks: list[Check] = []
    steps = (
        lambda: _check_app_state(app, checks),
        lambda: _check_source(app, checks),
        lambda: _check_image(app, checks),
    )
    for step in steps:
        try:
            step()
        except Exception as exc:  # 单项异常不阻断整体诊断
            _add(checks, "error", "检查异常", FAIL, f"{type(exc).__name__}: {exc}")

    running = False
    try:
        running = _check_services(app, checks)
    except Exception as exc:
        _add(checks, "container", "容器", FAIL, f"检查异常：{type(exc).__name__}: {exc}")

    for step in (
        lambda: _check_host_port(app, checks, running),
        lambda: _check_route(app, checks),
        lambda: _check_deployment(db, app, checks),
        lambda: _check_resources(app, checks, running),
    ):
        try:
            step()
        except Exception as exc:
            _add(checks, "error", "检查异常", FAIL, f"{type(exc).__name__}: {exc}")

    return _summarize(app, checks)


def _summarize(app: App, checks: list[Check]) -> dict:
    """汇总各检查项：统计通过 / 提示 / 失败 / 跳过，给出整体等级与首要建议。"""
    passed = sum(1 for c in checks if c.status == OK)
    warned = sum(1 for c in checks if c.status == WARN)
    failed = sum(1 for c in checks if c.status == FAIL)
    skipped = sum(1 for c in checks if c.status == SKIP)

    if failed:
        level = FAIL
    elif warned:
        level = WARN
    else:
        level = OK

    if level == FAIL:
        first = next(c for c in checks if c.status == FAIL)
        hint = first.hint or f"请先处理：{first.label}"
    elif level == WARN:
        first = next(c for c in checks if c.status == WARN)
        hint = first.hint or f"建议关注：{first.label}"
    else:
        hint = "各项检查均通过" + ("（部分项已跳过）" if skipped else "")

    return {
        "app_id": app.id,
        "app_name": app.name,
        "level": level,
        "checks": [c.as_dict() for c in checks],
        "summary": {
            "level": level,
            "total": len(checks),
            "passed": passed,
            "warned": warned,
            "failed": failed,
            "skipped": skipped,
            "hint": hint,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
