"""M7 异常自愈巡检：后台线程周期巡检受管容器，异常时按策略自愈。

策略：
- DB 记为 running 但容器实际 exited/dead：未超重启上限则重启并计数，
  超限则判定为"持续崩溃"，状态置 failed 并停止无谓重启；
- DB 记为 running 但容器已不存在：置 failed（需用户重新部署）；
- 容器恢复 running：清零该应用的重启计数，并追加一次 HTTP 就绪探测。

HTTP 就绪探针（容器 running 时才做）：
- 能建立连接且返回 HTTP < 500 → ready；
- 能返回响应但 HTTP ≥ 500 → degraded（应用自身报错，只展示不重启，避免重启风暴）；
- 连接被拒 / 超时 → unready，连续失败达 HEALTH_HTTP_MAX_FAILS 次则自动重启容器；
  连续自动重启达 HEALTH_MAX_RESTARTS 次仍不就绪则判定 failed（交由告警中心归档）；
- 探活恢复 ready 时清零"连续失败"与"HTTP 自动重启"计数。

重启计数仅存进程内内存（不增 DB 字段）；进程重启后自然归零，符合
"重启计数是运行期保护"的语义。单应用巡检异常不影响其余应用与主循环。
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
from app.models.models import App
from app.services import audit_service, docker_service

# 就绪四态
READY = "ready"          # 连接成功且 HTTP < 500
DEGRADED = "degraded"    # 连接成功但 HTTP ≥ 500
UNREADY = "unready"      # 连接被拒 / 超时
UNKNOWN = "unknown"      # 尚未探测 / 未分配端口 / 应用未运行

# app_id -> 连续自动重启次数
_restart_counts: dict[int, int] = {}

# app_id -> HTTP 就绪探测注册表
_ready: dict[int, dict] = {}
_ready_lock = threading.Lock()


def _heal_once() -> None:
    """巡检一轮：遍历所有 DB 记为 running 的应用并校正/自愈。"""
    db = SessionLocal()
    try:
        running = db.execute(select(App).where(App.status == "running")).scalars().all()
        targets = [(a.id, a.name, a.host_port) for a in running]
    except Exception:
        return
    finally:
        db.close()

    for app_id, name, host_port in targets:
        try:
            _heal_app(app_id, name, host_port)
        except Exception:
            # 单应用自愈失败不阻塞整轮巡检
            continue


def _heal_app(app_id: int, name: str, host_port: int | None) -> None:
    ref = docker_service.container_name_for(app_id, name)
    try:
        info = docker_service.get(ref)
    except docker_service.ContainerNotFoundError:
        _mark_failed(app_id, reason="容器丢失")
        return
    except docker_service.DockerUnavailableError:
        return  # Docker 临时不可用，下轮再试

    state = (info.state or info.status or "").lower()
    if state in ("running", "restarting", "created"):
        _restart_counts.pop(app_id, None)  # 恢复正常，清零计数
        if state == "running":
            _probe_readiness(app_id, name, host_port)
        return

    # 容器已退出 / 死亡：尝试自愈
    if state in ("exited", "dead", "paused"):
        count = _restart_counts.get(app_id, 0)
        if count >= settings.HEALTH_MAX_RESTARTS:
            _mark_failed(app_id, reason=f"连续重启 {count} 次仍异常")
            _restart_counts.pop(app_id, None)
            return
        try:
            docker_service.restart(ref)
            _restart_counts[app_id] = count + 1
        except (docker_service.ContainerNotFoundError, docker_service.DockerUnavailableError):
            _mark_failed(app_id, reason="重启失败")


def _mark_failed(app_id: int, reason: str) -> None:
    """把应用状态落为 failed（容器异常且无法自愈时）。"""
    _forget_readiness(app_id)
    db = SessionLocal()
    try:
        app = db.get(App, app_id)
        if app and app.status == "running":
            app.status = "failed"
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# HTTP 就绪探针
# ---------------------------------------------------------------------------


def _probe_readiness(app_id: int, name: str, host_port: int | None) -> None:
    """对运行中的容器做一次 HTTP 探活，并按其连续失败次数决定是否自动重启。"""
    if not settings.ENABLE_HEALTH_HTTP:
        return
    if not host_port:
        _record_readiness(app_id, UNKNOWN, "尚未分配宿主端口，无法探活")
        return

    state, detail, code = _http_probe(host_port)
    entry = _record_readiness(app_id, state, detail, code)
    if state == UNREADY and entry["consecutive_failures"] >= settings.HEALTH_HTTP_MAX_FAILS:
        _restart_unready(app_id, name, entry["consecutive_failures"])


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


def _record_readiness(app_id: int, state: str, detail: str, code: int | None = None) -> dict:
    """写入一次探活结论，返回写入后的注册表条目副本。"""
    now = time.time()
    with _ready_lock:
        entry = _ready.get(app_id) or {
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
        _ready[app_id] = entry
        return dict(entry)


def _restart_unready(app_id: int, name: str, fails: int) -> None:
    """连续探活失败达阈值：重启容器；重启次数达上限仍异常则置 failed。"""
    with _ready_lock:
        entry = _ready.get(app_id)
        if entry is None:
            return
        entry["restarts"] += 1
        entry["consecutive_failures"] = 0
        restarts = entry["restarts"]

    ref = docker_service.container_name_for(app_id, name)
    try:
        docker_service.restart(ref)
    except (docker_service.ContainerNotFoundError, docker_service.DockerUnavailableError) as exc:
        _audit_health(app_id, name, "restart_failed", f"连续 {fails} 次探活未就绪，自动重启失败：{exc}")
        return

    _audit_health(app_id, name, "restart", f"连续 {fails} 次探活未就绪，已自动重启容器（第 {restarts} 次）")
    if restarts >= settings.HEALTH_MAX_RESTARTS:
        _mark_failed(app_id, reason=f"探活异常，连续自动重启 {restarts} 次仍未就绪")


def _audit_health(app_id: int, name: str, stage: str, message: str) -> None:
    """就绪探针自动重启留痕（无交互用户，记匿名）。"""
    db = SessionLocal()
    try:
        audit_service.record_and_commit(
            db,
            user=None,
            action="app.health",
            target_type="app",
            target_id=app_id,
            target_name=name,
            detail={"stage": stage, "message": message},
        )
    except Exception:
        db.rollback()
    finally:
        db.close()


def _forget_readiness(app_id: int) -> None:
    with _ready_lock:
        _ready.pop(app_id, None)


def readiness_state(app_id: int) -> dict:
    """读取某应用最近一次 HTTP 就绪探测结论（只读注册表，不触发实际探测）。"""
    with _ready_lock:
        entry = _ready.get(app_id)
        data = dict(entry) if entry else {}
    return {
        "app_id": app_id,
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
    """读取某应用当前的连续自动重启计数（供告警巡检判定反复崩溃）。"""
    return _restart_counts.get(app_id, 0)


def start_watchdog() -> threading.Thread | None:
    """启动后台自愈巡检线程（lifespan 启动时调用，进程内仅一次）。"""
    if not settings.ENABLE_HEALTH_WATCHDOG:
        return None
    thread = threading.Thread(target=_watch_loop, name="paas-watchdog", daemon=True)
    thread.start()
    return thread
