"""操作审计：记录"谁在何时对哪个对象做了什么"。

设计要点：
- 写入放在 API 层调用（那里同时握有 Request 与已解析的实体）；
- `record()` 只 add + flush，不 commit —— 由业务端点统一提交，
  使审计与应用操作同事务，避免"操作成功但审计丢失"；
- 审计失败绝不能影响主流程，故内部吞掉所有异常并只记 warning。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.models import AuditLog, User

logger = logging.getLogger("paas.audit")

# 动作 → 中文描述（供前端下拉与展示复用）
ACTION_LABELS: dict[str, str] = {
    "login": "登录",
    "register": "注册",
    "logout": "登出",
    "app.create": "创建应用",
    "app.build": "构建镜像",
    "app.deploy": "部署应用",
    "app.stop": "停止应用",
    "app.restart": "重启应用",
    "app.quota": "调整配额",
    "app.config": "更新配置",
    "app.source": "上传源码",
    "app.file": "文件管理",
    "app.diagnose": "一键诊断",
    "app.health": "健康探针",
    "app.webhook": "自动部署",
    "app.webhook.reset": "重置 Webhook 密钥",
    "app.delete": "销毁应用",
    "user.create": "创建用户",
    "user.update": "更新用户",
    "user.reset_password": "重置密码",
    "user.delete": "删除用户",
}


def record(
    db: Session,
    *,
    user: User | None,
    action: str,
    target_type: str = "app",
    target_id: int | None = None,
    target_name: str | None = None,
    detail: dict[str, Any] | None = None,
    client_ip: str | None = None,
) -> None:
    """追加一条审计记录（不提交事务）。"""
    try:
        entry = AuditLog(
            user_id=user.id if user is not None else None,
            username=(user.username if user is not None else "anonymous"),
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            detail=json.dumps(detail, ensure_ascii=False) if detail else None,
            client_ip=client_ip,
        )
        db.add(entry)
        db.flush()
    except Exception as exc:  # noqa: BLE001 - 审计失败不得影响主流程
        logger.warning("写入审计日志失败（忽略）：%s", exc)


def record_and_commit(
    db: Session,
    *,
    user: User | None,
    action: str,
    target_type: str = "app",
    target_id: int | None = None,
    target_name: str | None = None,
    detail: dict[str, Any] | None = None,
    client_ip: str | None = None,
) -> None:
    """记录并立即提交（用于登录/登出这类没有其它业务事务的场景）。"""
    record(
        db,
        user=user,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        detail=detail,
        client_ip=client_ip,
    )
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("提交审计日志失败（忽略）：%s", exc)
        db.rollback()
