"""Git Webhook 接口：接收平台推送自动部署 + 控制台配置与手动触发。

接收端（POST /apps/{id}/webhook）不要求登录——调用方是外部 Git 平台，
身份由 HMAC 签名保证；其余接口仍按属主 / 管理员鉴权。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import client_ip, require_user
from app.models.models import App, AuditLog, User
from app.schemas.webhook_schema import WebhookOut, WebhookResetOut, WebhookTriggerOut
from app.services import app_service, audit_service, webhook_service
from app.services.app_service import AppError
from app.services.webhook_service import WebhookError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps", tags=["webhook"])


def _webhook_view(db: Session, app: App) -> WebhookOut:
    """组装 Webhook 配置视图：密钥（懒生成）+ 最近一次推送结果（取自审计）。"""
    secret = webhook_service.ensure_secret(db, app)
    last = (
        db.execute(
            select(AuditLog)
            .where(AuditLog.action == "app.webhook", AuditLog.target_id == app.id)
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    stage = error = None
    at = None
    if last is not None:
        at = last.ts.isoformat() if last.ts else None
        try:
            detail = json.loads(last.detail or "{}")
        except ValueError:
            detail = {}
        stage = detail.get("stage")
        error = detail.get("error")
    return WebhookOut(
        app_id=app.id,
        path=f"/api/apps/{app.id}/webhook",
        secret=secret,
        source_type=app.source_type,
        git_url=app.git_url,
        running=webhook_service.is_running(app.id),
        last_stage=stage,
        last_at=at,
        last_error=error,
    )


def _get_owned(db: Session, app_id: int, user: User | None) -> App:
    try:
        return app_service.get_app(db, app_id, user)
    except AppError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/{app_id}/webhook",
    response_model=WebhookTriggerOut,
    summary="接收 Git 平台推送（外部调用，HMAC 签名校验，无需登录）",
)
async def receive_webhook(
    app_id: int, request: Request, db: Session = Depends(get_db)
) -> WebhookTriggerOut:
    """外部 Git 平台的回调入口。

    鉴权：请求头签名——GitHub/Gitea 的 X-Hub-Signature-256、通用 X-Paas-Signature，
    或 GitLab 的 X-Gitlab-Token（明文密钥）。校验通过后异步执行
    "拉取最新源码 → 重建镜像 → 重新部署"，接口立即返回，不占用 Git 平台的请求超时预算。
    """
    body = await request.body()
    app = db.get(App, app_id)
    # 不区分"应用不存在"与"未配置密钥"，避免被用来探测应用是否存在
    if app is None or not app.webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Webhook 未启用或目标不存在"
        )

    headers = request.headers
    signature = headers.get("x-hub-signature-256") or headers.get("x-paas-signature")
    ok = webhook_service.verify_signature(app.webhook_secret, body, signature) or (
        webhook_service.verify_token(app.webhook_secret, headers.get("x-gitlab-token"))
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="签名校验失败")

    # GitHub 会先发一次 ping 事件用于验证连通性，只回应不触发部署
    event = (headers.get("x-github-event") or headers.get("x-gitlab-event") or "push").lower()
    if event == "ping":
        return WebhookTriggerOut(accepted=False, app_id=app_id, message="ping 事件：连通正常，未触发部署")

    try:
        webhook_service.trigger(
            app_id,
            source=f"webhook:{event}",
            actor="git-webhook",
            client_ip=client_ip(request),
        )
    except WebhookError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return WebhookTriggerOut(accepted=True, app_id=app_id, message="已受理：正在后台重新构建并部署")


@router.get("/{app_id}/webhook", response_model=WebhookOut, summary="查看 Webhook 配置")
def get_webhook(
    app_id: int, user: User | None = Depends(require_user), db: Session = Depends(get_db)
) -> WebhookOut:
    return _webhook_view(db, _get_owned(db, app_id, user))


@router.post("/{app_id}/webhook/reset", response_model=WebhookResetOut, summary="重置签名密钥")
def reset_webhook(
    app_id: int,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> WebhookResetOut:
    """重置密钥：旧密钥立即失效，需到 Git 平台同步更新。"""
    app = _get_owned(db, app_id, user)
    secret = webhook_service.rotate_secret(db, app)
    audit_service.record(
        db,
        user=user,
        action="app.webhook.reset",
        target_type="app",
        target_id=app.id,
        target_name=app.name,
        client_ip=client_ip(request),
    )
    db.commit()
    return WebhookResetOut(app_id=app.id, secret=secret)


@router.post("/{app_id}/webhook/trigger", response_model=WebhookTriggerOut, summary="手动触发自动部署")
def trigger_webhook(
    app_id: int,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> WebhookTriggerOut:
    """控制台手动触发（等价于模拟一次 push）。

    本机开发环境收不到外网回调时，用这个按钮走通同一条自动部署链路。
    """
    app = _get_owned(db, app_id, user)
    try:
        webhook_service.trigger(
            app_id,
            source="manual",
            actor=user.username if user else "anonymous",
            client_ip=client_ip(request),
        )
    except WebhookError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return WebhookTriggerOut(accepted=True, app_id=app.id, message="已受理：正在后台重新构建并部署")
