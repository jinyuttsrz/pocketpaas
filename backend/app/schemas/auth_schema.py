"""登录 / 鉴权相关请求与响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RegisterIn(BaseModel):
    """注册请求。"""

    username: str = Field(..., min_length=2, max_length=32, description="登录用户名")
    password: str = Field(..., min_length=8, max_length=128, description="登录密码")
    display_name: str | None = Field(None, max_length=64, description="显示名（可选）")
    captcha_uuid: str | None = Field(None, description="验证码会话 id")
    captcha_code: str | None = Field(None, max_length=8, description="用户填写的验证码")


class LoginIn(BaseModel):
    """登录请求。"""

    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=1, max_length=128)
    captcha_uuid: str | None = Field(None, description="验证码会话 id")
    captcha_code: str | None = Field(None, max_length=8, description="用户填写的验证码")


class CaptchaOut(BaseModel):
    """图形验证码响应：uuid 用于提交时回传，img 为可直接展示的 data URI。"""

    uuid: str
    img: str = Field(..., description="验证码图片（SVG data URI）")
    expires_in: int = Field(..., description="有效期（秒）")


class UserOut(BaseModel):
    """用户信息（响应，绝不包含口令哈希）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str | None
    role: str
    is_active: bool = True
    created_at: datetime
    last_login_at: datetime | None


class AdminUserCreateIn(BaseModel):
    """管理员新建用户请求。"""

    username: str = Field(..., min_length=2, max_length=32, description="登录用户名")
    password: str = Field(..., min_length=8, max_length=128, description="登录密码")
    display_name: str | None = Field(None, max_length=64, description="显示名（可选）")
    role: str = Field("user", pattern="^(admin|user)$", description="角色：admin / user")


class AdminUserUpdateIn(BaseModel):
    """管理员修改用户请求（字段均可选，仅更新提供的字段）。"""

    display_name: str | None = Field(None, max_length=64, description="显示名")
    role: str | None = Field(None, pattern="^(admin|user)$", description="角色：admin / user")
    is_active: bool | None = Field(None, description="是否启用账号")


class AdminUserResetPasswordIn(BaseModel):
    """管理员重置用户口令请求。"""

    password: str = Field(..., min_length=8, max_length=128, description="新密码")


class LoginOut(BaseModel):
    """登录 / 注册成功响应：携带会话令牌与用户信息。"""

    token: str
    expires_in: int = Field(..., description="令牌有效期（秒）")
    user: UserOut


class AuthStatusOut(BaseModel):
    """匿名可访问的登录态探测：告知前端是否需要登录 / 是否可注册。"""

    auth_enabled: bool
    allow_register: bool
    captcha_enabled: bool = False
    has_user: bool
    authenticated: bool
    user: UserOut | None = None


class MessageOut(BaseModel):
    """通用消息响应。"""

    message: str
