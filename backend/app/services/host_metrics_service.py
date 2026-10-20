"""M5 监控中心：宿主机实时性能采样（「资源与节点」页面）。

- 后台线程以 HOST_METRICS_INTERVAL 秒循环采集宿主 CPU 利用率，保留最近
  HOST_METRICS_KEEP 帧，构成约 3 分钟的滚动折线窗口；
- 同线程按 HOST_CONTAINER_INTERVAL 秒刷新平台受管容器的 CPU / 内存 / 网络占用；
- 内存、磁盘、负载、运行时长在请求时即时读取（psutil 单项失败即降级，
  不影响其余字段与主循环）。
"""

from __future__ import annotations

import threading
import time
from collections import deque

import psutil

from app.core.config import settings
from app.services import docker_service
from app.services.resource_service import human_bytes

_lock = threading.Lock()
# 宿主 CPU 利用率滚动窗口（百分比浮点帧）
_state: dict = {"history": deque(maxlen=settings.HOST_METRICS_KEEP), "containers": []}
_started = False


def _cpu_cores() -> int:
    try:
        return psutil.cpu_count(logical=True) or 1
    except Exception:
        return 1


def _load_avg() -> list[float] | None:
    """系统负载（1 / 5 / 15 分钟）；部分平台不支持时返回 None。"""
    try:
        return [round(float(x), 2) for x in psutil.getloadavg()]
    except Exception:
        return None


def _mem_info() -> dict:
    try:
        m = psutil.virtual_memory()
        return {"total": m.total, "used": m.used, "percent": round(float(m.percent), 1)}
    except Exception:
        return {"total": 0, "used": 0, "percent": 0.0}


def _disk_info() -> dict:
    """根文件系统占用；无法读取时各字段置零。"""
    try:
        d = psutil.disk_usage("/")
        return {"total": d.total, "used": d.used, "percent": round(float(d.percent), 1)}
    except Exception:
        return {"total": 0, "used": 0, "percent": 0.0}


def _uptime() -> int:
    try:
        return max(int(time.time() - psutil.boot_time()), 0)
    except Exception:
        return 0


def _sample_host() -> None:
    try:
        cpu = float(psutil.cpu_percent(interval=None))
    except Exception:
        cpu = 0.0
    with _lock:
        _state["history"].append(round(cpu, 1))


def _sample_containers() -> None:
    """采样平台受管运行中容器的资源占用（单容器异常仅自身降级）。"""
    try:
        items = docker_service.list_managed(all_states=False)
    except Exception:
        items = []
    snap: list[dict] = []
    for info in items:
        limit = info.mem_limit or 0
        entry = {
            "id": info.id,
            "name": info.name,
            "app_name": info.app_name or info.name,
            "status": info.state or info.status,
            "cpu_percent": 0.0,
            "mem_usage": 0,
            "mem_limit": limit,
            "mem_percent": 0.0,
            "net_rx": 0,
            "net_tx": 0,
        }
        try:
            s = docker_service.stats(info.name)
            entry.update(
                cpu_percent=s["cpu_percent"],
                mem_usage=s["mem_usage"],
                mem_limit=s.get("mem_limit") or limit,
                mem_percent=s["mem_percent"],
                net_rx=s.get("net_rx", 0),
                net_tx=s.get("net_tx", 0),
            )
        except Exception:
            pass  # 容器刚停止 / 采样竞态：保留零值占位
        entry["mem_usage_h"] = human_bytes(entry["mem_usage"])
        entry["mem_limit_h"] = human_bytes(entry["mem_limit"]) if entry["mem_limit"] else "—"
        entry["net_rx_h"] = human_bytes(entry["net_rx"])
        entry["net_tx_h"] = human_bytes(entry["net_tx"])
        snap.append(entry)
    with _lock:
        _state["containers"] = snap


def _loop_host() -> None:
    # 预热：cpu_percent(interval=None) 首帧依赖上次基线，先取一次建立基线
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass
    while True:
        _sample_host()
        time.sleep(settings.HOST_METRICS_INTERVAL)


def _loop_containers() -> None:
    """独立线程采样容器占用：避免 docker stats 的秒级耗时拖慢 CPU 帧节奏。"""
    while True:
        _sample_containers()
        time.sleep(settings.HOST_CONTAINER_INTERVAL)


def start_collector() -> threading.Thread | None:
    """启动宿主性能采样线程（宿主 CPU 与容器占用各一线程，进程内仅一次）。"""
    global _started
    if _started:
        return None
    _started = True
    host_thread = threading.Thread(target=_loop_host, name="paas-host-metrics", daemon=True)
    host_thread.start()
    threading.Thread(target=_loop_containers, name="paas-host-containers", daemon=True).start()
    return host_thread


def snapshot() -> dict:
    """返回宿主机性能快照（CPU 时序 / 负载 / 内存 / 磁盘 / 运行时长 / 容器占用）。"""
    mem = _mem_info()
    disk = _disk_info()
    with _lock:
        history = list(_state["history"])
        containers = list(_state["containers"])
    current = history[-1] if history else 0.0
    return {
        "cpu": {
            "cores": _cpu_cores(),
            "percent": current,
            "history": history,
            "interval": settings.HOST_METRICS_INTERVAL,
            "load": _load_avg(),
        },
        "memory": {
            **mem,
            "total_h": human_bytes(mem["total"]),
            "used_h": human_bytes(mem["used"]),
        },
        "disk": {
            **disk,
            "total_h": human_bytes(disk["total"]),
            "used_h": human_bytes(disk["used"]),
        },
        "uptime_seconds": _uptime(),
        "containers_running": sum(1 for c in containers if c["status"] == "running"),
        "containers": containers,
    }
