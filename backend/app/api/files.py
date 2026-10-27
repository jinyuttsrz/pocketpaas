"""项目文件管理接口：浏览 / 读写 / 增删改 / 下载 项目源码与容器内文件。"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.apps import _handle, _log_action
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_user
from app.models.models import User
from app.schemas.file_schema import (
    FileContentOut,
    FileEntryOut,
    FileListOut,
    FileOpIn,
    FileOpResultOut,
    FileWriteIn,
)
from app.services import file_service
from app.services.file_service import FileError

router = APIRouter(prefix="/apps", tags=["files"])

_UPLOAD_LIMIT = settings.FILE_MAX_UPLOAD_MB * 1024 * 1024
_READ_LIMIT = settings.FILE_MAX_READ_KB * 1024
_ARCHIVE_LIMIT = settings.FILE_MAX_ARCHIVE_MB * 1024 * 1024


def _caps() -> dict:
    return {
        "max_read_kb": settings.FILE_MAX_READ_KB,
        "max_upload_mb": settings.FILE_MAX_UPLOAD_MB,
        "max_archive_mb": settings.FILE_MAX_ARCHIVE_MB,
    }


def _remove_temp(path: Path) -> None:
    """下载完成后清理临时产物（仅用于 is_temp=True 的流）。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@router.get("/{app_id}/files", response_model=FileListOut, summary="浏览项目文件目录")
def list_files(
    app_id: int,
    target: str = Query("source", description="source=项目源码目录，container=运行中容器文件"),
    path: str = Query("", description="相对目标根目录的路径，根目录留空"),
    service: str = Query("", description="container 目标对应的服务名，缺省取公开服务"),
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> FileListOut:
    try:
        backend, _app = file_service.resolve_backend(
            db, app_id, target, user, service or None
        )
        entries, truncated = backend.list(path)
        states = file_service.target_states(db, app_id, user, service or None)
    except Exception as exc:  # noqa: BLE001 - 统一交给 _handle 映射
        raise _handle(exc) from exc
    return FileListOut(
        target=target,
        path=file_service.normalize_path(path),
        parent=file_service.parent_path(path),
        entries=[FileEntryOut.model_validate(item) for item in entries],
        targets=states,
        truncated=truncated,
        **_caps(),
    )


@router.get("/{app_id}/files/content", response_model=FileContentOut, summary="读取文本文件")
def read_file(
    app_id: int,
    target: str = Query("source"),
    path: str = Query(..., description="相对目标根目录的文件路径"),
    service: str = Query("", description="container 目标对应的服务名"),
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> FileContentOut:
    try:
        backend, _app = file_service.resolve_backend(
            db, app_id, target, user, service or None
        )
        data, truncated = backend.read(path, _READ_LIMIT)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    text, is_text = file_service.decode_text(data)
    return FileContentOut(
        target=target,
        path=file_service.normalize_path(path),
        content=text,
        is_text=is_text,
        truncated=truncated,
        size=len(data),
    )


@router.put("/{app_id}/files/content", response_model=FileOpResultOut, summary="保存文本文件")
def write_file(
    app_id: int,
    payload: FileWriteIn,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> FileOpResultOut:
    data = payload.content.encode("utf-8")
    if len(data) > _UPLOAD_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"内容超过 {settings.FILE_MAX_UPLOAD_MB}MB 上限，请改用上传文件",
        )
    try:
        backend, app = file_service.resolve_backend(
            db, app_id, payload.target, user, payload.service
        )
        backend.write(payload.path, data)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    _log_action(
        db,
        request,
        user,
        "app.file",
        app,
        detail={
            "op": "write",
            "target": payload.target,
            "path": payload.path,
            "service": payload.service,
        },
    )
    return FileOpResultOut(message="文件已保存")


@router.post("/{app_id}/files/upload", response_model=FileOpResultOut, summary="上传文件到目录")
async def upload_files(
    app_id: int,
    request: Request,
    target: str = Form("source"),
    path: str = Form(""),
    service: str = Form(""),
    files: list[UploadFile] = File(..., description="待上传的文件，可多选"),
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> FileOpResultOut:
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="没有收到上传的文件")
    try:
        backend, app = file_service.resolve_backend(
            db, app_id, target, user, service or None
        )
        base = file_service.normalize_path(path)
        saved = 0
        for item in files:
            name = Path(item.filename or "").name  # 只取 basename，阻断路径穿越
            if not name:
                continue
            data = bytearray()
            while True:
                chunk = await item.read(1 << 20)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > _UPLOAD_LIMIT:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"文件 {name} 超过 {settings.FILE_MAX_UPLOAD_MB}MB 上限",
                    )
            backend.write(f"{base}/{name}" if base else name, bytes(data))
            saved += 1
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    if saved == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="没有收到有效的文件")
    _log_action(
        db,
        request,
        user,
        "app.file",
        app,
        detail={"op": "upload", "target": target, "path": base, "count": saved, "service": service or None},
    )
    return FileOpResultOut(message=f"已上传 {saved} 个文件", count=saved)


@router.post("/{app_id}/files", response_model=FileOpResultOut, summary="新建文件夹 / 重命名 / 删除")
def file_op(
    app_id: int,
    payload: FileOpIn,
    request: Request,
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> FileOpResultOut:
    try:
        backend, app = file_service.resolve_backend(
            db, app_id, payload.target, user, payload.service
        )
        if payload.op == "mkdir":
            backend.mkdir(payload.path)
            message = "文件夹已创建"
        elif payload.op == "rename":
            if not payload.new_name:
                raise FileError("缺少新名称")
            backend.rename(payload.path, payload.new_name)
            message = "已重命名"
        else:
            backend.delete(payload.path)
            message = "已删除"
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    _log_action(
        db,
        request,
        user,
        "app.file",
        app,
        detail={
            "op": payload.op,
            "target": payload.target,
            "path": payload.path,
            "new_name": payload.new_name,
            "service": payload.service,
        },
    )
    return FileOpResultOut(message=message)


@router.get("/{app_id}/files/download", summary="下载文件，或以 zip 打包下载整个目录")
def download_file(
    app_id: int,
    target: str = Query("source"),
    path: str = Query("", description="相对目标根目录的路径，根目录即整包"),
    service: str = Query("", description="container 目标对应的服务名"),
    user: User | None = Depends(require_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    try:
        backend, _app = file_service.resolve_backend(
            db, app_id, target, user, service or None
        )
        result = backend.stream(path, _ARCHIVE_LIMIT)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    media = mimetypes.guess_type(result.download_name)[0] or "application/octet-stream"
    return FileResponse(
        result.path,
        filename=result.download_name,
        media_type=media,
        background=BackgroundTask(_remove_temp, result.path) if result.is_temp else None,
    )
