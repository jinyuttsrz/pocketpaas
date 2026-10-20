"""审计日志接口的请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field

from app.services.audit_service import ACTION_LABELS


class AuditLogOut(BaseModel):
    """单条审计记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None
    username: str
    action: str
    target_type: str
    target_id: int | None
    target_name: str | None
    detail: str | None
    client_ip: str | None
    ts: datetime

    @computed_field
    @property
    def action_label(self) -> str:
        """动作的中文描述，便于前端直接展示。"""
        return ACTION_LABELS.get(self.action, self.action)


class AuditActionOut(BaseModel):
    """可筛选的动作项（供前端下拉）。"""

    action: str
    label: str
