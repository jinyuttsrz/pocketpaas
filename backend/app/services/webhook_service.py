"""Git Webhook 自动部署：签名校验 + 异步重建流水线。

开发机（WSL / 内网）通常收不到 GitHub 等平台的外网回调，因此提供两条触发通道：
- 真实 webhook：POST /api/apps/{id}/webhook，用 HMAC-SHA256 签名校验来源；
- 手动触发：控制台按钮，等价于"模拟一次 push"。
两条通道共用同一套异步流水线：拉取最新源码 → 重建镜像 → 重新部署。

流水线在后台线程里跑（构建可能耗时数分钟，不能占用 HTTP 请求），
同一应用同时只允许一条，避免并发重复构建。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import secrets
import threading

from app.core.database import SessionLocal
from app.models.models import App
from app.services import app_service, audit_service, source_service

logger = logging.getLogger(__name__)


class WebhookError(RuntimeError):
    """Webhook 业务错误（签名不合法、重复触发等）→ 由接口层映射为 4xx。"""


# 进程内锁：登记正在执行自动部署的应用（避免同一应用并发重建）
_running: set[int] = set()
_running_lock = threading.Lock()


def generate_secret() -> str:
    """生成 webhook 签名密钥（48 位十六进制，足够抵抗离线爆破）。"""
    return secrets.token_hex(24)


def is_running(app_id: int) -> bool:
    with _running_lock:
        return app_id in _running


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """校验 HMAC-SHA256 签名，兼容各平台的常见写法：

    - GitHub / Gitea：`X-Hub-Signature-256: sha256=<hex>`
    - 通用自定义头：`X-Paas-Signature: <hex>`
    - GitLab：`X-Gitlab-Token: <明文密钥>`（不是 HMAC，这里直接比对密钥本身）
    """
    if not secret or not signature:
        return False
    provided = signature.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[len("sha256=") :]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.lower())


def verify_token(secret: str, token: str | None) -> bool:
    """GitLab 风格的明文 token 比对。"""
    if not secret or not token:
        return False
    return hmac.compare_digest(secret, token.strip())


def ensure_secret(db, app: App) -> str:
    """取应用的 webhook 密钥，缺失时懒生成并落库（老库补列后首次访问即生效）。"""
    if not app.webhook_secret:
        app.webhook_secret = generate_secret()
        db.commit()
        db.refresh(app)
    return app.webhook_secret


def rotate_secret(db, app: App) -> str:
    """重置密钥（旧密钥立即失效）。"""
    app.webhook_secret = generate_secret()
    db.commit()
    db.refresh(app)
    return app.webhook_secret


def trigger(app_id: int, *, source: str, actor: str, client_ip: str | None = None) -> None:
    """登记一次自动部署并异步执行（立即返回，不阻塞调用方）。"""
    with _running_lock:
        if app_id in _running:
            raise WebhookError("该应用已有自动部署在执行中，请稍后重试")
        _running.add(app_id)
    threading.Thread(
        target=_run_pipeline,
        args=(app_id, source, actor, client_ip),
        name=f"paas-webhook-{app_id}",
        daemon=True,
    ).start()


def _audit(db, app_id: int, app_name: str | None, source: str, stage: str, **extra) -> None:
    """写一条自动部署审计（接收 / 成功 / 失败三个阶段各一条，便于追溯）。"""
    audit_service.record_and_commit(
        db,
        user=None,
        action="app.webhook",
        target_type="app",
        target_id=app_id,
        target_name=app_name,
        detail={"source": source, "stage": stage, **extra},
    )


def _run_pipeline(app_id: int, source: str, actor: str, client_ip: str | None) -> None:
    """后台流水线：拉源码 → 重建镜像 → 重新部署（结果落审计，失败置 app.status=failed）。"""
    db = SessionLocal()
    app_name: str | None = None
    try:
        app = db.get(App, app_id)
        if app is None:
            logger.warning("Webhook 触发失败：应用不存在 id=%s", app_id)
            return
        app_name = app.name
        _audit(db, app_id, app_name, source, "received", actor=actor, client_ip=client_ip)

        # 1) Git 源：重新克隆，确保部署的是最新提交（上传/示例源无此步骤）
        if app.source_type == "git" and app.git_url:
            source_service.ingest_git(app.id, app.git_url)
            db.commit()
            db.refresh(app)

        # 2) 重建镜像：此时旧容器仍在对外服务，减少停机窗口
        app, deployment, _ = app_service.build_app(db, app_id, None, None)

        # 3) 重新部署：deploy_app 会移除同名旧容器，并复用应用已有的宿主端口
        app = app_service.deploy_app(db, app_id, None, None)
        _audit(
            db,
            app_id,
            app.name,
            source,
            "success",
            deployment_id=deployment.id,
            image=app.image,
            host_port=app.host_port,
        )
    except Exception as exc:  # noqa: BLE001 - 后台线程必须自行兜底，否则异常静默丢失
        logger.warning("Webhook 自动部署失败 app_id=%s: %s", app_id, exc)
        with contextlib.suppress(Exception):
            db.rollback()
            _audit(db, app_id, app_name, source, "failed", error=str(exc))
    finally:
        with _running_lock:
            _running.discard(app_id)
        with contextlib.suppress(Exception):
            db.close()
