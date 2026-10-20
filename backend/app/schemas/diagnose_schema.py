"""一键诊断相关响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# 单项检查状态：通过 / 提示 / 失败 / 跳过
CheckStatus = Literal["ok", "warn", "fail", "skip"]
# 整体等级
DiagLevel = Literal["ok", "warn", "fail"]


class DiagnoseCheckOut(BaseModel):
    """单个检查项的结论。"""

    key: str
    label: str
    status: CheckStatus
    detail: str
    hint: str | None = None


class DiagnoseSummaryOut(BaseModel):
    """诊断汇总统计。"""

    level: DiagLevel
    total: int
    passed: int
    warned: int
    failed: int
    skipped: int
    hint: str


class DiagnoseOut(BaseModel):
    """一键诊断完整响应。"""

    app_id: int
    app_name: str
    level: DiagLevel
    checks: list[DiagnoseCheckOut]
    summary: DiagnoseSummaryOut
    generated_at: str
