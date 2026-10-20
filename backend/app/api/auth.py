"""登录 / 注册 / 登出接口。

- `GET  /api/auth/status`   匿名：探测是否需要登录、是否可注册（供登录页渲染）
- `GET  /api/auth/captcha`  匿名：获取图形验证码（uuid + 图片）
- `POST /api/auth/register` 注册（受 AUTH_ALLOW_REGISTER 控制）
- `POST /api/auth/login`    登录，签发会话令牌（同时写入 Cookie）
- `POST /api/auth/logout`   登出，注销当前会话
- `GET  /api/auth/me`       获取当前登录用户
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import client_ip, current_user_optional, require_user
from app.models.models import User
from app.schemas.auth_schema import (
    AuthStatusOut,
    CaptchaOut,
    LoginIn,
    LoginOut,
    MessageOut,
    RegisterIn,
    UserOut,
)
from app.services import audit_service, auth_service
from app.services.auth_service import AuthError
from app.services.captcha_service import captcha_store

router = APIRouter(prefix="/auth", tags=["auth"])

_COOKIE = "paas_token"


def _verify_captcha(uuid_value: str | None, code: str | None) -> None:
    """校验图形验证码：开启时强制校验，未通过则抛 400。

    验证码在账号口令之前校验，失败时不查库、不做哈希，可有效阻挡暴力破解。
    """
    if not settings.AUTH_ENABLE_CAPTCHA:
        return
    if not uuid_value or not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请输入验证码")
    result = captcha_store.verify(uuid_value, code)
    if result == "expired":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="验证码已失效，请点击图片刷新"
        )
    if result != "ok":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="验证码错误")


def _set_cookie(response: Response, token: str) -> None:
    """写会话 Cookie：HttpOnly 防脚本读取；线上模式（HTTPS）加 Secure。"""
    response.set_cookie(
        key=_COOKIE,
        value=token,
        max_age=settings.AUTH_SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.USE_HTTPS,
        path="/",
    )


@router.get("/status", response_model=AuthStatusOut, summary="登录态探测（匿名可访问）")
def auth_status(
    user: User | None = Depends(current_user_optional),
    db: Session = Depends(get_db),
) -> AuthStatusOut:
    has_user = db.scalar(select(User.id).limit(1)) is not None
    return AuthStatusOut(
        auth_enabled=settings.ENABLE_AUTH,
        allow_register=settings.AUTH_ALLOW_REGISTER,
        captcha_enabled=settings.AUTH_ENABLE_CAPTCHA,
        has_user=has_user,
        authenticated=user is not None,
        user=UserOut.model_validate(user) if user is not None else None,
    )


@router.get("/captcha", response_model=CaptchaOut, summary="获取图形验证码（匿名可访问）")
def captcha() -> CaptchaOut:
    """签发一张图形验证码，返回 uuid 与图片（SVG data URI）。"""
    data = captcha_store.issue()
    return CaptchaOut(**data)


@router.post(
    "/register",
    response_model=LoginOut,
    status_code=status.HTTP_201_CREATED,
    summary="注册并自动登录",
)
def register(payload: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)):
    if not settings.AUTH_ALLOW_REGISTER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="平台已关闭自助注册")
    _verify_captcha(payload.captcha_uuid, payload.captcha_code)
    try:
        user = auth_service.register(
            db, payload.username, payload.password, display_name=payload.display_name
        )
        user.last_login_at = datetime.utcnow()
        db.commit()
        db.refresh(user)
        token, _ = auth_service.create_session(
            db, user, user_agent=request.headers.get("user-agent"), client_ip=client_ip(request)
        )
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    audit_service.record_and_commit(
        db,
        user=user,
        action="register",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        client_ip=client_ip(request),
    )
    _set_cookie(response, token)
    return LoginOut(
        token=token,
        expires_in=settings.AUTH_SESSION_HOURS * 3600,
        user=UserOut.model_validate(user),
    )


@router.post("/login", response_model=LoginOut, summary="登录")
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    _verify_captcha(payload.captcha_uuid, payload.captcha_code)
    try:
        user = auth_service.authenticate(db, payload.username, payload.password)
        token, _ = auth_service.create_session(
            db, user, user_agent=request.headers.get("user-agent"), client_ip=client_ip(request)
        )
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    audit_service.record_and_commit(
        db,
        user=user,
        action="login",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        client_ip=client_ip(request),
    )
    _set_cookie(response, token)
    return LoginOut(
        token=token,
        expires_in=settings.AUTH_SESSION_HOURS * 3600,
        user=UserOut.model_validate(user),
    )


@router.post("/logout", response_model=MessageOut, summary="登出")
def logout(
    request: Request,
    response: Response,
    _user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> MessageOut:
    token = request.cookies.get(_COOKIE)
    if not token:
        auth = request.headers.get("authorization", "")
        parts = auth.split(None, 1)
        token = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None
    auth_service.revoke_session(db, token)
    audit_service.record_and_commit(
        db,
        user=_user,
        action="logout",
        target_type="session",
        target_id=_user.id if _user is not None else None,
        target_name=_user.username if _user is not None else None,
        client_ip=client_ip(request),
    )
    response.delete_cookie(_COOKIE, path="/")
    return MessageOut(message="已退出登录")


@router.get("/me", response_model=UserOut, summary="当前登录用户")
def me(user: User | None = Depends(require_user)) -> UserOut:
    if user is None:  # 未开启鉴权时返回占位信息
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未开启鉴权")
    return UserOut.model_validate(user)
