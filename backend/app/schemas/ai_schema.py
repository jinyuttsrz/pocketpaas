"""AI 分析助手请求模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AiHistoryItem(BaseModel):
    """一轮历史消息。"""

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=2000)


class AiAnalyzeIn(BaseModel):
    """平台 / 单应用状态分析请求。"""

    app_id: int | None = Field(None, description="应用 id；为空表示全平台")
    focus: str | None = Field(None, max_length=200, description="可选关注点")


class AiChatIn(BaseModel):
    """多轮问答请求。"""

    message: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    app_id: int | None = Field(None, description="可选应用 id，限定分析范围")
    history: list[AiHistoryItem] = Field(
        default_factory=list,
        max_length=20,
        description="最近若干轮对话（服务端会再次截断）",
    )
