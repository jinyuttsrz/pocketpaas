"""认证服务：口令哈希、会话令牌签发与校验、默认管理员引导。

设计要点：
- 口令使用标准库 hashlib.pbkdf2_hmac 加盐哈希，不引入额外依赖；
- 会话采用「随机令牌 + 服务端存哈希」模式，支持主动登出与过期失效；
- 校验逻辑保持纯函数风格，异常统一抛 AuthError，由接口层映射为 401/400。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import AuthSession, User

# PBKDF2 迭代次数（兼顾安全与单机性能）
_PBKDF2_ITERATIONS = 120_000
_SALT_BYTES = 16
_TOKEN_BYTES = 32

# 用户名：字母/数字/下划线/中划线，2-32 位，首字符为字母或数字
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")


class AuthError(RuntimeError):
    """认证 / 授权失败（映射为 401 或 400）。"""


# ---------------------------------------------------------------------------
# 1) 口令哈希（PBKDF2-HMAC-SHA256）
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """生成口令哈希，格式：pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>。"""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令是否匹配已存储哈希（常量时间比较，防时序侧信道）。"""
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


# ---------------------------------------------------------------------------
# 2) 用户名 / 口令强度校验
# ---------------------------------------------------------------------------


def validate_username(username: str) -> None:
    """用户名合法性校验，防止注入到后续展示与日志。"""
    if not username or not _USERNAME_RE.match(username):
        raise AuthError("用户名需为 2-32 位字母、数字、下划线或中划线，且以字母或数字开头")


def validate_password_strength(password: str) -> None:
    """口令强度校验：长度下限 + 字符多样性。"""
    if not password or len(password) < settings.AUTH_PASSWORD_MIN_LENGTH:
        raise AuthError(f"密码长度不得少于 {settings.AUTH_PASSWORD_MIN_LENGTH} 位")
    if len(password) > 128:
        raise AuthError("密码长度不得超过 128 位")
    if password.isdigit() or password.isalpha():
        raise AuthError("密码需同时包含字母与数字，以提升安全性")


# ---------------------------------------------------------------------------
# 3) 会话令牌
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    db: Session,
    user: User,
    *,
    user_agent: str | None = None,
    client_ip: str | None = None,
) -> tuple[str, AuthSession]:
    """为用户签发一个新会话，返回（明文令牌, 会话记录）。

    明文令牌只在本次响应中返回给客户端，服务端仅保存其哈希。
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    session = AuthSession(
        user_id=user.id,
        token_hash=_hash_token(token),
        expires_at=datetime.utcnow() + timedelta(hours=settings.AUTH_SESSION_HOURS),
        user_agent=(user_agent or "")[:255] or None,
        client_ip=(client_ip or "")[:64] or None,
    )
    db.add(session)
    # 顺带清理该用户已过期会话，避免表无限增长
    _purge_expired(db, user.id)
    db.commit()
    db.refresh(session)
    return token, session


def resolve_session(db: Session, token: str | None) -> User | None:
    """按令牌解析当前用户；令牌缺失 / 无效 / 过期均返回 None。"""
    if not token:
        return None
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == _hash_token(token)))
    if session is None:
        return None
    if session.expires_at < datetime.utcnow():
        db.delete(session)
        db.commit()
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        return None
    return user


def revoke_session(db: Session, token: str | None) -> None:
    """注销（登出）指定令牌对应的会话，幂等。"""
    if not token:
        return
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == _hash_token(token)))
    if session is not None:
        db.delete(session)
        db.commit()


def _purge_expired(db: Session, user_id: int) -> None:
    """删除某用户已过期的会话记录。"""
    now = datetime.utcnow()
    for s in db.scalars(select(AuthSession).where(AuthSession.user_id == user_id)).all():
        if s.expires_at < now:
            db.delete(s)


# ---------------------------------------------------------------------------
# 4) 注册 / 登录
# ---------------------------------------------------------------------------


def register(
    db: Session,
    username: str,
    password: str,
    *,
    display_name: str | None = None,
    role: str | None = None,
) -> User:
    """注册新用户，返回用户实体（不自动登录，由调用方决定是否签发会话）。"""
    username = (username or "").strip()
    validate_username(username)
    validate_password_strength(password)

    if db.scalar(select(User).where(User.username == username)) is not None:
        raise AuthError(f"用户名 {username!r} 已存在")

    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=(display_name or username).strip() or username,
        # 首个注册用户为管理员，便于平台初始化；其余为普通用户
        role=role or ("admin" if _is_first_user(db) else "user"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, username: str, password: str) -> User:
    """校验账号口令，成功返回用户并更新最近登录时间。"""
    username = (username or "").strip()
    user = db.scalar(select(User).where(User.username == username))
    # 用户不存在时也执行一次哈希运算，避免通过响应时间枚举账号
    if user is None:
        verify_password(password, "pbkdf2_sha256$120000$" + "00" * 16 + "$" + "00" * 32)
        raise AuthError("用户名或密码错误")
    if not user.is_active:
        raise AuthError("账号已被禁用，请联系管理员")
    if not verify_password(password, user.password_hash):
        raise AuthError("用户名或密码错误")

    user.last_login_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return user


def _is_first_user(db: Session) -> bool:
    return db.scalar(select(User.id).limit(1)) is None


# ---------------------------------------------------------------------------
# 5) 用户管理（管理员）
# ---------------------------------------------------------------------------


def list_users(db: Session) -> list[User]:
    """全部用户，按 id 升序（管理员用户管理面板用）。"""
    return list(db.execute(select(User).order_by(User.id)).scalars().all())


def get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise AuthError(f"用户不存在：id={user_id}")
    return user


def create_user(
    db: Session,
    username: str,
    password: str,
    *,
    display_name: str | None = None,
    role: str = "user",
) -> User:
    """管理员创建用户：与注册同规则，但可显式指定角色。"""
    if role not in ("admin", "user"):
        raise AuthError("角色只能为 admin 或 user")
    return register(db, username, password, display_name=display_name, role=role)


def update_user(
    db: Session,
    user_id: int,
    *,
    display_name: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
) -> User:
    """修改用户资料 / 角色 / 启用状态。"""
    user = get_user(db, user_id)
    if role is not None:
        if role not in ("admin", "user"):
            raise AuthError("角色只能为 admin 或 user")
        _guard_last_admin(db, user, new_role=role, new_active=is_active)
        user.role = role
    if is_active is not None:
        _guard_last_admin(db, user, new_role=role, new_active=is_active)
        user.is_active = is_active
        # 禁用账号时一并注销其所有会话，立即失效
        if not is_active:
            for s in db.scalars(select(AuthSession).where(AuthSession.user_id == user.id)).all():
                db.delete(s)
    if display_name is not None:
        user.display_name = display_name.strip() or user.username
    db.commit()
    db.refresh(user)
    return user


def _guard_last_admin(
    db: Session, user: User, *, new_role: str | None, new_active: bool | None
) -> None:
    """防止把最后一名启用中的管理员降级或禁用，导致平台无人可管理。"""
    if user.role != "admin" or not user.is_active:
        return  # 目标本就不是「启用中的管理员」，无需保护
    losing_admin = (new_role is not None and new_role != "admin") or new_active is False
    if not losing_admin:
        return
    others = db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == "admin", User.is_active.is_(True), User.id != user.id)
    )
    if not others:
        raise AuthError("至少需保留一名启用状态的管理员")


def reset_password(db: Session, user_id: int, new_password: str) -> User:
    """管理员重置指定用户口令，并注销其既有会话。"""
    user = get_user(db, user_id)
    validate_password_strength(new_password)
    user.password_hash = hash_password(new_password)
    for s in db.scalars(select(AuthSession).where(AuthSession.user_id == user.id)).all():
        db.delete(s)
    db.commit()
    db.refresh(user)
    return user


def delete_user(db: Session, user_id: int, *, operator_id: int | None = None) -> None:
    """删除用户；其名下的应用属主置空（保留应用，仅管理员可见/可管理）。"""
    user = get_user(db, user_id)
    if operator_id is not None and user.id == operator_id:
        raise AuthError("不能删除当前登录的账号")
    _guard_last_admin(db, user, new_role="user", new_active=False)
    db.delete(user)
    db.commit()


def ensure_default_admin(db: Session) -> User | None:
    """可选引导：当库中无任何用户且配置了默认管理员时，自动创建。

    用于「开箱即用」——本地首次启动即有一组可用账号；
    线上可通过 AUTH_BOOTSTRAP_ADMIN=false 关闭，改为手动注册。
    """
    if not settings.AUTH_BOOTSTRAP_ADMIN:
        return None
    if db.scalar(select(User.id).limit(1)) is not None:
        return None
    try:
        return register(
            db,
            settings.AUTH_DEFAULT_USERNAME,
            settings.AUTH_DEFAULT_PASSWORD,
            display_name="平台管理员",
            role="admin",
        )
    except AuthError:
        return None
