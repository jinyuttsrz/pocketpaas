"""M7 异常自愈巡检：后台线程周期巡检受管服务容器，异常时按策略自愈。

阶段 2 起巡检粒度从「应用」下沉到「服务」：
- 每个 AppService 记录都有独立的容器名与状态，逐服务校正并刷新 AppService.status；
- 公开服务异常（丢失 / 持续崩溃）才把应用置为 failed；
  内部服务（数据库、缓存等）异常只标记该服务 failed，不影响应用整体状态，
  避免内部服务瞬时故障把整个应用永久判死；
- 容器恢复 running 时清零该服务的重启计数，并对公开服务做一次 HTTP 就绪探测。

策略：
- DB 记为 running 但容器实际 exited/dead：未超重启上限则重启并计数，
  超限则判定为"持续崩溃"：公开服务置应用 failed，内部服务仅置服务 failed；
- 容器已不存在：公开服务置应用 failed（需重新部署），内部服务标记 failed；
- HTTP 就绪探针只针对公开服务（内部服务没有宿主端口）：
  连续失败达 HEALTH_HTTP_MAX_FAILS 次自动重启，达 HEALTH_MAX_RESTARTS 次仍不就绪则判定 failed。

重启计数仅存进程内内存（不增 DB 字段）；进程重启后自然归零，符合
"重启计数是运行期保护"的语义。单服务巡检异常不影响其余服务与主循环。
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.models import App, AppService
from app.services import audit_service, docker_service

# 就绪四态
READY = "ready"          # 连接成功且 HTTP < 500
DEGRADED = "degraded"    # 连接成功但 HTTP ≥ 500
UNREADY = "unready"      # 连接被拒 / 超时
UNKNOWN = "unknown"      # 尚未探测 / 未分配端口 / 应用未运行

# (app_id, service_name) -> 连续自动重启次数
_restart_counts: dict[tuple[int, str], int] = {}

# (app_id, service_name) -> HTTP 就绪探测注册表
_ready: dict[tuple[int, str], dict] = {}
_ready_lock = threading.Lock()

# app_id -> 公开服务名（readiness_state 只读该服务）
_public_services: dict[int, str] = {}


def _heal_once() -> None:
    """巡检一轮：遍历所有 running 应用下的服务容器并校正/自愈。"""
    db = SessionLocal()
    try:
        rows = db.execute(
            select(AppService, App)
            .join(App, App.id == AppService.app_id)
            .where(App.status == "running", AppService.container_name.isnot(None))
            .order_by(AppService.app_id, AppService.name)
        ).all()
        targets = [
            (
                app.id,
                app.name,
                service.id,
                service.name,
                service.container_name,
                service.is_public,
                service.host_port,
            )
            for service, app in rows
        ]
    except Exception:
        return
    finally:
        db.close()

    for target in targets:
        try:
            _heal_service(*target)
        except Exception:
            # 单服务自愈失败不阻塞整轮巡检
            continue


def _heal_service(
    app_id: int,
    app_name: str,
    service_id: int,
    service_name: str,
    ref: str,
    is_public: bool,
    host_port: int | None,
) -> None:
    try:
        info = docker_service.get(ref)
    except docker_service.ContainerNotFoundError:
        _set_service_status(service_id, "failed")
        _restart_counts.pop((app_id, service_name), None)
        _forget_readiness(app_id, service_name)
        if is_public:
            _mark_app_failed(app_id, reason="公开服务容器丢失")
        return
    except docker_service.DockerUnavailableError:
        return  # Docker 临时不可用，下轮再试

    key = (app_id, service_name)
    state = (info.state or info.status or "").lower()
    if state in ("running", "restarting", "created"):
        _restart_counts.pop(key, None)  # 恢复正常，清零计数
        _set_service_status(service_id, state)
        if state == "running" and is_public:
            _probe_readiness(app_id, service_name, host_port)
        return

    # 容器已退出 / 死亡：尝试自愈
    if state in ("exited", "dead", "paused"):
        count = _restart_counts.get(key, 0)
        if count >= settings.HEALTH_MAX_RESTARTS:
            _set_service_status(service_id, "failed")
            _forget_readiness(app_id, service_name)
            _restart_counts.pop(key, None)
            if is_public:
                _mark_app_failed(app_id, reason=f"公开服务连续重启 {count} 次仍异常")
            return
        try:
            docker_service.restart(ref)
            _restart_counts[key] = count + 1
            _set_service_status(service_id, "restarting")
        except (docker_service.ContainerNotFoundError, docker_service.DockerUnavailableError):
            _set_service_status(service_id, "failed")
            if is_public:
                _mark_app_failed(app_id, reason="公开服务重启失败")


def _set_service_status(service_id: int, status: str) -> None:
    """刷新服务状态（仅状态变化时写库，避免每轮巡检都产生 UPDATE）。"""
    db = SessionLocal()
    try:
        service = db.get(AppService, service_id)
        if service is not None and service.status != status:
            service.status = status
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _mark_app_failed(app_id: int, reason: str) -> None:
    """把应用状态落为 failed（公开服务异常且无法自愈时）。"""
    _forget_readiness(app_id)
    db = SessionLocal()
    try:
        app = db.get(App, app_id)
        if app and app.status == "running":
            app.status = "failed"
            # 公开服务异常才触发应用 failed，同步其服务状态便于前端展示
            public = db.execute(
                select(AppService).where(
                    AppService.app_id == app_id, AppService.is_public.is_(True)
                )
            ).scalars().all()
            for service in public:
                service.status = "failed"
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# HTTP 就绪探针（仅公开服务）
# ---------------------------------------------------------------------------


def _probe_readiness(app_id: int, service_name: str, host_port: int | None) -> None:
    """对运行中的公开服务做一次 HTTP 探活，并按连续失败次数决定是否自动重启。"""
    if not settings.ENABLE_HEALTH_HTTP:
        return
    if not host_port:
        _record_readiness((app_id, service_name), UNKNOWN, "尚未分配宿主端口，无法探活")
        return

    state, detail, code = _http_probe(host_port)
    entry = _record_readiness((app_id, service_name), state, detail, code)
    if state == UNREADY and entry["consecutive_failures"] >= settings.HEALTH_HTTP_MAX_FAILS:
        ref = _service_ref(app_id, service_name)
        if ref:
            _restart_unready(app_id, service_name, ref, entry["consecutive_failures"])


def _service_ref(app_id: int, service_name: str) -> str | None:
    """按服务名取当前容器名（探针重启时使用）。"""
    db = SessionLocal()
    try:
        service = db.execute(
            select(AppService).where(
                AppService.app_id == app_id, AppService.name == service_name
            )
        ).scalar_one_or_none()
        return service.container_name if service is not None else None
    except Exception:
        return None
    finally:
        db.close()


def _http_probe(port: int) -> tuple[str, str, int | None]:
    """向 127.0.0.1:宿主端口 发起一次 GET，返回 (状态, 说明, HTTP 状态码)。"""
    path = settings.HEALTH_HTTP_PATH or "/"
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=settings.HEALTH_HTTP_TIMEOUT)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()  # 消费响应体，保证连接可复用/正常关闭
            code = resp.status
        finally:
            conn.close()
    except socket.timeout:
        return UNREADY, f"GET {path} 超时（>{settings.HEALTH_HTTP_TIMEOUT}s）", None
    except OSError as exc:  # 连接被拒 / 网络不可达
        return UNREADY, f"无法连接 127.0.0.1:{port}：{exc}", None
    except Exception as exc:  # noqa: BLE001 - 探活异常不得打断巡检
        return UNREADY, f"探活异常：{type(exc).__name__}: {exc}", None

    if code >= 500:
        return DEGRADED, f"GET {path} 返回 {code}（应用内部错误）", code
    return READY, f"GET {path} 返回 {code}", code


def _record_readiness(key: tuple[int, str], state: str, detail: str, code: int | None = None) -> dict:
    """写入一次探活结论，返回写入后的注册表条目副本。"""
    now = time.time()
    with _ready_lock:
        entry = _ready.get(key) or {
            "state": UNKNOWN,
            "detail": "",
            "status_code": None,
            "consecutive_failures": 0,
            "restarts": 0,
            "last_ok_at": None,
            "last_probe_at": None,
        }
        entry["state"] = state
        entry["detail"] = detail
        entry["status_code"] = code
        entry["last_probe_at"] = now
        if state == READY:
            entry["consecutive_failures"] = 0
            entry["restarts"] = 0  # 已恢复正常，清零 HTTP 自动重启计数
            entry["last_ok_at"] = now
        elif state == UNREADY:
            entry["consecutive_failures"] += 1
        else:  # degraded / unknown：不计失败，避免应用自身 5xx 触发重启
            entry["consecutive_failures"] = 0
        _ready[key] = entry
        _public_services[key[0]] = key[1]
        return dict(entry)


def _restart_unready(app_id: int, service_name: str, ref: str, fails: int) -> None:
    """连续探活失败达阈值：重启容器；重启次数达上限仍异常则置 failed。"""
    with _ready_lock:
        entry = _ready.get((app_id, service_name))
        if entry is None:
            return
        entry["restarts"] += 1
        entry["consecutive_failures"] = 0
        restarts = entry["restarts"]

    try:
        docker_service.restart(ref)
    except (docker_service.ContainerNotFoundError, docker_service.DockerUnavailableError) as exc:
        _audit_health(
            app_id,
            service_name,
            "restart_failed",
            f"连续 {fails} 次探活未就绪，自动重启失败：{exc}",
        )
        return

    _audit_health(
        app_id,
        service_name,
        "restart",
        f"连续 {fails} 次探活未就绪，已自动重启容器（第 {restarts} 次）",
    )
    if restarts >= settings.HEALTH_MAX_RESTARTS:
        _mark_app_failed(app_id, reason=f"探活异常，连续自动重启 {restarts} 次仍未就绪")


def _audit_health(app_id: int, service_name: str, stage: str, message: str) -> None:
    """就绪探针自动重启留痕（无交互用户，记匿名）。"""
    db = SessionLocal()
    try:
        app = db.get(App, app_id)
        audit_service.record_and_commit(
            db,
            user=None,
            action="app.health",
            target_type="app",
            target_id=app_id,
            target_name=app.name if app else str(app_id),
            detail={"stage": stage, "service": service_name, "message": message},
        )
    except Exception:
        db.rollback()
    finally:
        db.close()


def _forget_readiness(app_id: int, service_name: str | None = None) -> None:
    with _ready_lock:
        if service_name is None:
            for key in [key for key in _ready if key[0] == app_id]:
                _ready.pop(key, None)
        else:
            _ready.pop((app_id, service_name), None)


def readiness_state(app_id: int) -> dict:
    """读取某应用公开服务最近一次 HTTP 就绪探测结论（只读注册表）。"""
    with _ready_lock:
        service_name = _public_services.get(app_id)
        entry = _ready.get((app_id, service_name)) if service_name else None
        data = dict(entry) if entry else {}
    return {
        "app_id": app_id,
        "service": service_name,
        "state": data.get("state", UNKNOWN),
        "detail": data.get("detail") or "尚无探活记录",
        "status_code": data.get("status_code"),
        "consecutive_failures": data.get("consecutive_failures", 0),
        "restarts": data.get("restarts", 0),
        "last_ok_at": _iso(data.get("last_ok_at")),
        "last_probe_at": _iso(data.get("last_probe_at")),
        "probe_path": settings.HEALTH_HTTP_PATH,
        "interval": settings.HEALTH_CHECK_INTERVAL,
    }


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _watch_loop() -> None:
    while True:
        _heal_once()
        time.sleep(settings.HEALTH_CHECK_INTERVAL)


def get_restart_count(app_id: int) -> int:
    """读取某应用当前的最大连续自动重启计数（供告警巡检判定反复崩溃）。"""
    return max(
        (count for (target_app, _name), count in _restart_counts.items() if target_app == app_id),
        default=0,
    )


def start_watchdog() -> threading.Thread | None:
    """启动后台自愈巡检线程（lifespan 启动时调用，进程内仅一次）。"""
    if not settings.ENABLE_HEALTH_WATCHDOG:
        return None
    thread = threading.Thread(target=_watch_loop, name="paas-watchdog", daemon=True)
    thread.start()
    return thread
