"""FastAPI 应用入口。

启动：
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import (
    ai,
    alerts,
    apps,
    audit,
    auth,
    console,
    diagnose,
    files,
    system,
    users,
    webhook,
    ws,
)
from app.core.config import settings
from app.core.database import init_db, SessionLocal
from app.services import (
    alert_service,
    app_service,
    auth_service,
    docker_service,
    host_metrics_service,
    metrics_service,
    watchdog_service,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化数据库表 + 拉起 M5 指标采样线程 + M7 自愈巡检线程 + 告警巡检线程。"""
    init_db()
    # 登录引导：库中无用户且开启引导时，创建默认管理员
    db = SessionLocal()
    try:
        auth_service.ensure_default_admin(db)
        # 多容器改造：为历史应用补 default 服务记录
        app_service.backfill_app_services(db)
    finally:
        db.close()
    # M7：确保内网隔离网桥存在（失败不阻塞启动，容器将降级默认网络）
    docker_service.ensure_network()
    metrics_service.start_collector()
    host_metrics_service.start_collector()
    watchdog_service.start_watchdog()
    alert_service.start_alert_watcher()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    description="轻量级容器云平台与应用自动化交付系统",
    lifespan=lifespan,
)

app.include_router(system.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(audit.router, prefix="/api")
app.include_router(alerts.router, prefix="/api")
app.include_router(ai.router, prefix="/api")
app.include_router(apps.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(diagnose.router, prefix="/api")
app.include_router(webhook.router, prefix="/api")
app.include_router(ws.router)  # /ws/... WebSocket 日志流
app.include_router(console.router)

# M6：控制台静态资源（CSS / JS），SPA 入口由 console.py 提供
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parent / "web"),
    name="static",
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
