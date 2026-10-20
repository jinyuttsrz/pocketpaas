"""资源校验服务：读取宿主真实资源，校验用户输入的容器端口 / 内存上限 / CPU 核数。

用于"新建应用"向导的字段确认：用户在确认容器端口、内存上限、CPU 核数后，
立即对照宿主机真实资源与平台已承诺配额给出即时提示，避免申请超过实际能力的资源。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import App
from app.services import docker_service

# 内存字符串解析：如 256m / 512M / 1g / 1024（b/k/m/g/t，缺省字节）
_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*$", re.IGNORECASE)
_MEM_UNITS = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
# Docker 单容器内存下限（低于此值容器无法创建）
_MIN_MEM_BYTES = 6 * 1024 * 1024


def parse_mem_limit(text: str) -> int | None:
    """把 "256m" / "512M" / "1g" / "1024" 解析为字节数；非法返回 None。"""
    m = _MEM_RE.match(text or "")
    if not m:
        return None
    return int(float(m.group(1)) * _MEM_UNITS[m.group(2).lower()])


def human_bytes(n: int) -> str:
    """字节数转人类可读字符串（用于前端提示）。"""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _meminfo() -> dict[str, int]:
    """读取 /proc/meminfo（WSL / Linux），返回字节数字典。"""
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii", errors="ignore") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    info[key.strip()] = int(parts[0]) * 1024  # kB -> B
    except OSError:
        pass
    return info


def host_resources() -> dict:
    """宿主真实资源：优先取 Docker Daemon 口径，缺失时回退系统信息。"""
    cpu_total = 0.0
    mem_total = 0
    try:
        with docker_service.client() as cli:
            info = cli.info()
            cpu_total = float(info.get("NCPU") or 0)
            mem_total = int(info.get("MemTotal") or 0)
    except Exception:  # Docker 不可用时不阻塞校验，回退系统信息
        pass
    mi = _meminfo()
    if not cpu_total:
        cpu_total = float(os.cpu_count() or 1)
    if not mem_total:
        mem_total = mi.get("MemTotal", 0)
    mem_available = mi.get("MemAvailable", mem_total)
    return {"cpu_total": cpu_total, "mem_total": mem_total, "mem_available": mem_available}


def _committed(db: Session, exclude_app_id: int | None = None) -> tuple[float, int]:
    """平台已承诺的资源：所有应用配额之和（CPU 核 / 内存字节）。

    exclude_app_id 用于配额调整场景：排除被调整应用自身的旧配额，
    否则其现有占用会被重复计入，导致剩余量被低估。
    """
    cpu = 0.0
    mem = 0
    for app in db.execute(select(App)).scalars().all():
        if exclude_app_id is not None and app.id == exclude_app_id:
            continue
        cpu += float(app.cpu_quota or 0)
        mem += parse_mem_limit(app.mem_limit) or 0
    return cpu, mem


@dataclass
class FieldCheck:
    """单个字段的校验结果：level ∈ ok / warn / error。"""

    level: str
    message: str


def _check_port(db: Session, port: int, resources: dict) -> FieldCheck:
    """端口校验：合法区间 + 是否已被其它应用声明 + 平台宿主端口池余量。"""
    if not 1 <= port <= 65535:
        return FieldCheck("error", f"容器端口 {port} 超出合法范围（1–65535）")
    dup = db.execute(
        select(App.name).where(App.container_port == port)
    ).scalars().first()
    pool_total = settings.PORT_RANGE_END - settings.PORT_RANGE_START + 1
    pool_free = max(pool_total - resources["ports_used"], 0)
    if dup:
        return FieldCheck(
            "warn",
            f"端口 {port} 已被应用「{dup}」声明为容器端口，确认非同一应用可继续",
        )
    return FieldCheck("ok", f"端口 {port} 可用；平台宿主端口池剩余 {pool_free}/{pool_total}")


def _check_mem(text: str, resources: dict) -> FieldCheck:
    """内存校验：格式 / 下限 / 是否超过宿主真实内存与平台剩余可分配内存。"""
    mem = parse_mem_limit(text)
    if mem is None:
        return FieldCheck("error", f"内存格式不正确：{text}（示例：256m / 512m / 1g）")
    if mem < _MIN_MEM_BYTES:
        return FieldCheck("error", f"内存上限 {text} 低于 Docker 最小限制（6 MB）")
    total, avail = resources["mem_total"], resources["mem_available"]
    free = resources["mem_free"]
    if mem > total:
        return FieldCheck(
            "error",
            f"内存上限 {text} 超过宿主物理内存 {human_bytes(total)}，无法分配",
        )
    if mem > avail:
        return FieldCheck(
            "warn",
            f"内存上限 {text} 超过当前空闲内存 {human_bytes(avail)}，"
            f"运行时可能因内存不足被 OOM 终止",
        )
    if mem > free:
        return FieldCheck(
            "warn",
            f"内存上限 {text} 超过平台剩余可分配内存 {human_bytes(free)}"
            f"（宿主 {human_bytes(total)}，已承诺 {human_bytes(resources['mem_committed'])}），存在超卖风险",
        )
    return FieldCheck("ok", f"内存 {text} 在可分配范围内（平台剩余 {human_bytes(free)}）")


def _check_cpu(cores: float, resources: dict) -> FieldCheck:
    """CPU 校验：是否超过宿主真实核心数与平台剩余可分配核数。"""
    if cores <= 0:
        return FieldCheck("error", f"CPU 配额 {cores} 无效，需大于 0")
    total = resources["cpu_total"]
    if cores > total:
        return FieldCheck(
            "error",
            f"CPU 配额 {cores} 核超过宿主核心数 {total:g} 核，无法分配",
        )
    free = resources["cpu_free"]
    if cores > free:
        return FieldCheck(
            "warn",
            f"CPU 配额 {cores} 核超过平台剩余可分配核数 {free:g}"
            f"（宿主 {total:g} 核，已承诺 {resources['cpu_committed']:g} 核），存在超卖风险",
        )
    return FieldCheck("ok", f"CPU {cores:g} 核在可分配范围内（平台剩余 {free:g} 核）")


def check_resources(
    db: Session,
    container_port: int | None,
    mem_limit: str,
    cpu_quota: float,
    exclude_app_id: int | None = None,
) -> dict:
    """对照宿主真实资源校验输入，返回逐字段结果与资源基线。

    container_port 为 None 时（详情页配额调整）不返回端口校验结果；
    exclude_app_id 用于配额调整时排除应用自身旧配额。
    """
    host = host_resources()
    cpu_committed, mem_committed = _committed(db, exclude_app_id)
    ports_used = db.execute(
        select(App.host_port).where(App.host_port.is_not(None))
    ).scalars().all()

    resources = {
        "cpu_total": host["cpu_total"],
        "cpu_committed": cpu_committed,
        "cpu_free": max(host["cpu_total"] - cpu_committed, 0.0),
        "mem_total": host["mem_total"],
        "mem_available": host["mem_available"],
        "mem_committed": mem_committed,
        "mem_free": max(host["mem_total"] - mem_committed, 0),
        "ports_used": len([p for p in ports_used if p is not None]),
        "port_range": f"{settings.PORT_RANGE_START}-{settings.PORT_RANGE_END}",
    }
    # 便于前端直接展示的可读值
    resources["mem_total_h"] = human_bytes(host["mem_total"])
    resources["mem_available_h"] = human_bytes(host["mem_available"])
    resources["mem_committed_h"] = human_bytes(mem_committed)
    resources["mem_free_h"] = human_bytes(resources["mem_free"])

    return {
        "resources": resources,
        "port": vars(_check_port(db, container_port, resources)) if container_port else None,
        "mem": vars(_check_mem(mem_limit, resources)),
        "cpu": vars(_check_cpu(cpu_quota, resources)),
    }