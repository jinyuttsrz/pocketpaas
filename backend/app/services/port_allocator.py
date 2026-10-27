"""端口分配器：在配置的端口区间内为应用分配未被占用的宿主机端口。"""

from __future__ import annotations

import socket
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import App, AppService

# M8：并发部署时，多个应用会同时"读库→选空闲端口"，而此刻彼此都尚未落库，
# 于是选到同一端口，容器 start 时报 "port is already allocated"。
# 用进程内锁串行化"选取 + 落库"这段临界区，保证端口预留的原子性。
_ALLOC_LOCK = threading.Lock()


def _is_occupied(port: int) -> bool:
    """检测宿主机端口是否已被占用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True


def _used_by_apps(db: Session) -> set[int]:
    """数据库中已被应用占用的端口（含多公开服务与回滚槽位端口）。"""
    used: set[int] = set()
    for value in db.execute(
        select(App.host_port).where(App.host_port.is_not(None))
    ).scalars():
        if value is not None:
            used.add(int(value))
    rows = db.execute(
        select(AppService.host_port, AppService.previous_host_port).where(
            (AppService.host_port.is_not(None))
            | (AppService.previous_host_port.is_not(None))
        )
    ).all()
    for current, previous in rows:
        if current is not None:
            used.add(int(current))
        if previous is not None:
            used.add(int(previous))
    return used


def allocate(db: Session) -> int:
    """从端口区间中分配一个可用端口。

    同时排除"数据库已分配"与"宿主机实际占用"两类端口，避免冲突。
    """
    used = _used_by_apps(db)
    for port in range(settings.PORT_RANGE_START, settings.PORT_RANGE_END + 1):
        if port in used or _is_occupied(port):
            continue
        return port
    raise RuntimeError(
        f"端口区间 {settings.PORT_RANGE_START}-{settings.PORT_RANGE_END} 已耗尽，无法分配"
    )


def reserve_for_app(db: Session, app: App, *, force_new: bool = False) -> int:
    """为应用原子性地预留一个宿主机端口并立即落库。

    M8：并发安全的端口分配入口。在进程内锁的保护下完成"选取空闲端口 +
    写回 app.host_port + commit"，使并发的其它部署能立刻看到该端口已被占用，
    从根本上消除 TOCTOU 竞态。

    force_new=True 时忽略应用已有端口，强制重新分配（用于端口竞态重试）。
    已有端口且未强制时直接复用，避免重复分配。
    """
    with _ALLOC_LOCK:
        if app.host_port and not force_new:
            return app.host_port
        port = allocate(db)
        app.host_port = port
        db.commit()
        db.refresh(app)
        return port


def reserve_for_service(
    db: Session, service: AppService, *, force_new: bool = False
) -> int:
    """为单个公开服务预留宿主端口（多公开服务 / 蓝绿切换使用）。"""
    with _ALLOC_LOCK:
        if service.host_port and not force_new:
            return service.host_port
        port = allocate(db)
        service.host_port = port
        db.commit()
        db.refresh(service)
        return port
