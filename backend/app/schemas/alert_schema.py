"""告警中心接口的响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AlertOut(BaseModel):
    """单条告警记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    rule: str
    target_type: str
    target_id: int | None
    target_name: str
    level: str
    message: str
    value: float | None
    threshold: float | None
    status: str
    created_at: datetime
    resolved_at: datetime | None
    acked_at: datetime | None
    acked_by: str | None
