"""鉴权依赖：从请求中提取会话令牌并解析当前用户。

令牌来源兼容两种方式：
- `Authorization: Bearer <token>`（REST / 脚本调用）
- `paas_token` Cookie（浏览器控制台，便于 WebSocket 与页面导航共用登录态）
"""

from __future__ import annotations

from fastapi import Cookie, Depends, Header, HTTPException, Request, status

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.models import User
from app.services import auth_service


def client_ip(request: Request) -> str | None:
    """提取客户端真实 IP：优先取反向代理的 X-Forwarded-For，其次直连地址。

    供登录会话与操作审计共用。
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _extract_token(authorization: str | None, cookie_token: str | None) -> str | None:
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return authorization.strip()
    return cookie_token


def current_user_optional(
    authorization: str | None = Header(default=None, alias="Authorization"),
    paas_token: str | None = Cookie(default=None, alias="paas_token"),
) -> User | None:
    """解析当前用户，未登录返回 None（不抛异常）。"""
    if not settings.ENABLE_AUTH:
        return None
    token = _extract_token(authorization, paas_token)
    db = SessionLocal()
    try:
        return auth_service.resolve_session(db, token)
    finally:
        db.close()


def require_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
    paas_token: str | None = Cookie(default=None, alias="paas_token"),
) -> User | None:
    """鉴权依赖：未开启鉴权时直接放行；开启时要求有效会话，否则 401。"""
    if not settings.ENABLE_AUTH:
        return None
    token = _extract_token(authorization, paas_token)
    db = SessionLocal()
    try:
        user = auth_service.resolve_session(db, token)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="未登录或登录已过期，请重新登录",
                headers={"WWW-Authenticate": "Bearer"},
            )
        db.expunge(user)
        return user
    finally:
        db.close()


def require_admin(user: User | None = Depends(require_user)) -> User | None:
    """管理员依赖：在 require_user 基础上要求 role=admin，否则 403。

    未开启鉴权时（单租户本地模式）直接放行，保持与 require_user 一致的语义。
    """
    if not settings.ENABLE_AUTH:
        return None
    if user is None or user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return user
