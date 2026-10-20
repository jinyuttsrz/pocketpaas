"""指标阈值告警：后台巡检宿主与应用，超限时留档。

设计要点：
- 去重不变式：同一 (rule, target_type, target_id) 至多存在一条未消除记录
  （status 为 active 或 acked）。靠数据库查询保证，不用进程内集合，
  进程重启后也不会重复堆积。
- 自动恢复：某规则本轮不再触发，则把它的未消除记录置为 resolved
  （"本轮未触发"即代表指标已回落）。
- 已确认（acked）的记录不因再次触发而复活，只刷新观测值与消息，
  避免确认过的告警反复打扰。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.models import Alert, App
from app.services import host_metrics_service, watchdog_service

# 未消除状态：active（待处理）/ acked（已确认但指标仍未回落）
OPEN_STATUSES = ("active", "acked")


def _key(rule: str, target_type: str, target_id: int | None) -> tuple:
    return (rule, target_type, target_id)


def _raise_or_keep(
    db,
    *,
    rule: str,
    target_type: str,
    target_id: int | None,
    target_name: str,
    level: str,
    message: str,
    value: float | None = None,
    threshold: float | None = None,
) -> None:
    """创建新告警；若该目标已有未消除记录则只刷新观测值（不复活已确认状态）。"""
    existing = db.execute(
        select(Alert).where(
            Alert.rule == rule,
            Alert.target_type == target_type,
            Alert.target_id == target_id,
            Alert.status.in_(OPEN_STATUSES),
        )
    ).scalars().first()
    if existing is not None:
        existing.message = message
        existing.value = value
        existing.threshold = threshold
        existing.level = level
        return
    db.add(
        Alert(
            rule=rule,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            level=level,
            message=message,
            value=value,
            threshold=threshold,
            status="active",
        )
    )


def _scan_host(db, snap: dict, fired: set[tuple]) -> None:
    """宿主级阈值：CPU / 内存 / 磁盘使用率。"""
    checks = (
        ("host_cpu", "cpu", settings.ALERT_CPU_PERCENT, "CPU"),
        ("host_mem", "memory", settings.ALERT_MEM_PERCENT, "内存"),
        ("host_disk", "disk", settings.ALERT_DISK_PERCENT, "磁盘"),
    )
    for rule, field, threshold, label in checks:
        percent = float(snap.get(field, {}).get("percent") or 0)
        if percent < threshold:
            continue
        _raise_or_keep(
            db,
            rule=rule,
            target_type="host",
            target_id=None,
            target_name="宿主机",
            level="critical" if percent >= threshold + 10 else "warning",
            message=f"宿主{label}使用率 {percent:.1f}%（阈值 {threshold:.0f}%）",
            value=percent,
            threshold=threshold,
        )
        fired.add(_key(rule, "host", None))


def _scan_apps(db, fired: set[tuple]) -> None:
    """应用级阈值：容器反复重启 / 已判定持续崩溃。"""
    apps = db.execute(
        select(App).where(App.status.in_(("running", "failed")))
    ).scalars().all()
    for app in apps:
        if app.status == "failed":
            _raise_or_keep(
                db,
                rule="app_failed",
                target_type="app",
                target_id=app.id,
                target_name=app.name,
                level="critical",
                message=f"应用「{app.name}」容器异常且无法自愈，已停止重启",
            )
            fired.add(_key("app_failed", "app", app.id))
            continue
        restarts = watchdog_service.get_restart_count(app.id)
        if restarts >= settings.ALERT_RESTART_COUNT:
            _raise_or_keep(
                db,
                rule="app_restart",
                target_type="app",
                target_id=app.id,
                target_name=app.name,
                level="warning",
                message=f"应用「{app.name}」已连续自动重启 {restarts} 次",
                value=float(restarts),
                threshold=float(settings.ALERT_RESTART_COUNT),
            )
            fired.add(_key("app_restart", "app", app.id))


def _resolve_stale(db, fired: set[tuple]) -> None:
    """把本轮未触发的未消除记录置为 resolved（指标已回落）。"""
    open_alerts = db.execute(
        select(Alert).where(Alert.status.in_(OPEN_STATUSES))
    ).scalars().all()
    now = datetime.now()
    for alert in open_alerts:
        if _key(alert.rule, alert.target_type, alert.target_id) not in fired:
            alert.status = "resolved"
            alert.resolved_at = now


def _scan_once() -> None:
    """巡检一轮：评估阈值 → 写入/刷新告警 → 恢复本轮未触发的记录。"""
    db = SessionLocal()
    try:
        fired: set[tuple] = set()
        _scan_host(db, host_metrics_service.snapshot(), fired)
        _scan_apps(db, fired)
        _resolve_stale(db, fired)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _watch_loop() -> None:
    while True:
        _scan_once()
        time.sleep(max(5, settings.ALERT_CHECK_INTERVAL))


def start_alert_watcher() -> threading.Thread | None:
    """启动告警巡检线程（lifespan 启动时调用，进程内仅一次）。"""
    if not settings.ENABLE_ALERTS:
        return None
    thread = threading.Thread(target=_watch_loop, name="paas-alerts", daemon=True)
    thread.start()
    return thread


def ack(db, alert_id: int, operator: str | None) -> Alert | None:
    """手动确认一条告警（仅对未消除记录生效）。"""
    alert = db.get(Alert, alert_id)
    if alert is None or alert.status not in OPEN_STATUSES:
        return None
    alert.status = "acked"
    alert.acked_at = datetime.now()
    alert.acked_by = operator
    db.commit()
    db.refresh(alert)
    return alert
