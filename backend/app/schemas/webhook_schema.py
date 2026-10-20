"""Git Webhook 相关响应模型。"""

from __future__ import annotations

from pydantic import BaseModel


class WebhookOut(BaseModel):
    """Webhook 配置视图（仅属主 / 管理员可见，含签名密钥）。"""

    app_id: int
    path: str  # 相对路径，前端用 location.origin 拼成可粘贴的完整地址
    secret: str
    source_type: str
    git_url: str | None = None
    running: bool = False  # 是否有自动部署正在执行
    last_stage: str | None = None  # received / success / failed
    last_at: str | None = None
    last_error: str | None = None


class WebhookResetOut(BaseModel):
    """重置密钥后的返回。"""

    app_id: int
    secret: str


class WebhookTriggerOut(BaseModel):
    """手动触发（模拟一次 push）的受理结果。"""

    accepted: bool
    app_id: int
    message: str
