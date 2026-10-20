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
from app.models.models import App, Metric
from app.services import docker_service


def sample_app(app_id: int, container_ref: str) -> Metric | None:
    """对单个容器做一次采样并落库；容器不在运行则返回 None。"""
    try:
        info = docker_service.get(container_ref)
        if info.status != "running":
            return None
        s = docker_service.stats(container_ref)
    except (docker_service.ContainerNotFoundError, docker_service.DockerUnavailableError):
        return None

    metric = Metric(app_id=app_id, cpu=s["cpu_percent"], mem=s["mem_percent"])
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
    """采样主循环：每轮遍历所有 running 状态应用。"""
    while True:
        db = SessionLocal()
        try:
            running = db.execute(
                select(App).where(App.status == "running")
            ).scalars().all()
            targets = [
                (app.id, docker_service.container_name_for(app.id, app.name))
                for app in running
            ]
        except Exception:
            targets = []
        finally:
            db.close()

        for app_id, ref in targets:
            try:
                sample_app(app_id, ref)
            except Exception:
                # 单容器采样失败不阻塞整体采集
                continue
        time.sleep(settings.METRICS_INTERVAL)


def start_collector() -> threading.Thread:
    """启动后台采样线程（lifespan 启动时调用，进程内仅一次）。"""
    thread = threading.Thread(target=_collect_loop, name="paas-metrics", daemon=True)
    thread.start()
    return thread


def list_metrics(db: Session, app_id: int, limit: int = 120) -> list[Metric]:
    """查询应用最近 limit 条采样，按时间升序返回（折线图直接可用）。"""
    rows = db.execute(
        select(Metric)
        .where(Metric.app_id == app_id)
        .order_by(Metric.id.desc())
        .limit(min(limit, settings.METRICS_KEEP))
    ).scalars().all()
    return list(reversed(rows))
