"""M5 监控中心：容器 CPU/内存定时采样、落库与查询。

- 后台 daemon 线程按 METRICS_INTERVAL 周期采样所有 running 容器（docker stats 一次性快照）；
- 每应用仅保留最近 METRICS_KEEP 条，防止 SQLite 无限膨胀（2H4G 线上同理）；
- 采样循环内逐容器 try/except：单容器异常不影响其余应用与主循环。
"""

from __future__ import annotations

import threading
import time

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.models import App, AppService, Metric
from app.services import docker_service


def sample_app(app_id: int, container_ref: str, service_id: int | None = None) -> Metric | None:
    """对单个服务容器做一次采样并落库；容器不在运行则返回 None。"""
    try:
        info = docker_service.get(container_ref)
        if info.status != "running":
            return None
        s = docker_service.stats(container_ref)
    except (docker_service.ContainerNotFoundError, docker_service.DockerUnavailableError):
        return None

    metric = Metric(
        app_id=app_id, service_id=service_id, cpu=s["cpu_percent"], mem=s["mem_percent"]
    )
    db = SessionLocal()
    try:
        db.add(metric)
        db.commit()
        db.refresh(metric)
        _prune(db, app_id)
        return metric
    finally:
        db.close()


def _prune(db: Session, app_id: int, keep: int | None = None) -> None:
    """每应用仅保留最近 keep 条采样（默认取配置）。"""
    keep = keep or settings.METRICS_KEEP
    threshold = (
        select(Metric.id)
        .where(Metric.app_id == app_id)
        .order_by(Metric.id.desc())
        .offset(keep)
        .limit(1)
    ).scalar_subquery()
    db.execute(
        delete(Metric).where(Metric.app_id == app_id, Metric.id < threshold)
    )
    db.commit()


def _collect_loop() -> None:
    """采样主循环：每轮遍历所有 running 应用下的服务容器。"""
    while True:
        db = SessionLocal()
        try:
            rows = db.execute(
                select(AppService)
                .join(App, App.id == AppService.app_id)
                .where(App.status == "running", AppService.container_name.isnot(None))
                .order_by(AppService.app_id, AppService.name)
            ).scalars().all()
            targets = [(row.app_id, row.container_name, row.id) for row in rows]
        except Exception:
            targets = []
        finally:
            db.close()

        for app_id, ref, service_id in targets:
            try:
                sample_app(app_id, ref, service_id)
            except Exception:
                # 单容器采样失败不阻塞整体采集
                continue
        time.sleep(settings.METRICS_INTERVAL)


def start_collector() -> threading.Thread:
    """启动后台采样线程（lifespan 启动时调用，进程内仅一次）。"""
    thread = threading.Thread(target=_collect_loop, name="paas-metrics", daemon=True)
    thread.start()
    return thread


def _service_filter(db: Session, app_id: int, service: str | None):
    """构造采样过滤条件：默认取公开服务，未指定且无服务记录时兼容历史数据。"""
    services = db.execute(
        select(AppService)
        .where(AppService.app_id == app_id)
        .order_by(AppService.is_public.desc(), AppService.id)
    ).scalars().all()
    if not services:
        return Metric.service_id.is_(None)
    target = None
    if service:
        target = next((item for item in services if item.name == service), None)
    else:
        target = services[0]
    if target is None:
        raise ValueError(f"服务不存在：{service}")
    return Metric.service_id == target.id


def list_metrics(
    db: Session, app_id: int, service: str | None = None, limit: int = 120
) -> list[Metric]:
    """查询应用（或指定服务）最近 limit 条采样，按时间升序返回。"""
    stmt = select(Metric).where(Metric.app_id == app_id)
    stmt = stmt.where(_service_filter(db, app_id, service))
    rows = db.execute(
        stmt.order_by(Metric.id.desc()).limit(min(limit, settings.METRICS_KEEP))
    ).scalars().all()
    return list(reversed(rows))
