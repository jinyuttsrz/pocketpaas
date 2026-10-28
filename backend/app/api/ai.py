"""AI 只读分析接口：平台状态总结与多轮问答。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import client_ip, require_user
from app.models.models import User
from app.schemas.ai_schema import AiAnalyzeIn, AiChatIn
from app.services import ai_service, app_service, audit_service

router = APIRouter(prefix="/ai", tags=["ai"])


def _http_error(exc: ai_service.AiError) -> HTTPException:
    if isinstance(exc, ai_service.AiRateLimit):
        code = status.HTTP_429_TOO_MANY_REQUESTS
    elif isinstance(exc, (ai_service.AiDisabled, ai_service.AiConfigError)):
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(exc, ai_service.AiUpstreamError):
        code = status.HTTP_502_BAD_GATEWAY
    else:
        code = status.HTTP_500_INTERNAL_SERVER_ERROR
    return HTTPException(status_code=code, detail=str(exc))


# 审计里保留问答正文，方便事后回看；过长时截断并标记，避免单条记录无限膨胀
AUDIT_QUESTION_LIMIT = 2000
AUDIT_ANSWER_LIMIT = 4000


def _audit(db: Session, request: Request, user: User | None, action: str, result: dict) -> None:
    summary = result.get("context_summary") or {}
    target = summary.get("target_app") or {}
    question = result.get("question") or ""
    answer = result.get("answer") or ""
    audit_service.record_and_commit(
        db,
        user=user,
        action=action,
        target_type="system",
        target_id=target.get("id"),
        target_name="AI 助手",
        detail={
            "model": result.get("model"),
            "scope": summary.get("scope"),
            "scope_app": target.get("name"),
            "question": question[:AUDIT_QUESTION_LIMIT],
            "answer": answer[:AUDIT_ANSWER_LIMIT],
            "truncated": len(answer) > AUDIT_ANSWER_LIMIT,
            "usage": result.get("usage"),
            "elapsed_ms": result.get("elapsed_ms"),
        },
        client_ip=client_ip(request),
    )


@router.get("/health", summary="AI 模块配置状态")
def ai_health(user: User | None = Depends(require_user)) -> dict:
    """返回是否开启、参数是否完整、当前模型；不探测供应商、不返回 Key。"""
    return ai_service.health()


@router.post("/analyze", summary="AI 总结平台或应用运行状态")
def ai_analyze(
    payload: AiAnalyzeIn,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = ai_service.analyze(db, user, payload.app_id, payload.focus)
    except app_service.AppError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ai_service.AiError as exc:
        raise _http_error(exc) from exc
    _audit(db, request, user, "ai.analyze", result)
    return result


@router.post("/chat", summary="AI 只读运维问答")
def ai_chat(
    payload: AiChatIn,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = ai_service.chat(
            db,
            user,
            payload.message,
            app_id=payload.app_id,
            history=payload.history,
        )
    except app_service.AppError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ai_service.AiError as exc:
        raise _http_error(exc) from exc
    _audit(db, request, user, "ai.chat", result)
    return result
