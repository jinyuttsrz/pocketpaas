"""项目文件管理相关请求 / 响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 文件目标：项目源码目录 / 运行中容器文件系统
FileTarget = Literal["source", "container"]


class FileEntryOut(BaseModel):
    """目录中的一个条目（路径相对目标根目录）。"""

    model_config = ConfigDict(from_attributes=True)

    name: str
    path: str
    is_dir: bool
    size: int | None = None
    mtime: str | None = None


class FileTargetStateOut(BaseModel):
    """单个目标的可用性快照。"""

    label: str
    ready: bool
    reason: str | None = None


class FileListOut(BaseModel):
    """目录列表响应。"""

    target: str
    path: str
    parent: str | None = None
    entries: list[FileEntryOut]
    targets: dict[str, FileTargetStateOut]
    truncated: bool = False
    max_read_kb: int
    max_upload_mb: int
    max_archive_mb: int


class FileContentOut(BaseModel):
    """文本文件内容响应。"""

    target: str
    path: str
    content: str | None = None
    is_text: bool
    truncated: bool = False
    size: int = 0


class FileWriteIn(BaseModel):
    """保存文本文件请求。"""

    target: FileTarget
    path: str = Field(..., min_length=1, description="相对目标根目录的文件路径")
    content: str = Field(..., description="文件内容（UTF-8 文本）")


class FileOpIn(BaseModel):
    """目录级操作请求：新建文件夹 / 重命名 / 删除。"""

    target: FileTarget
    path: str = Field("", description="相对目标根目录的路径（根目录为空串）")
    op: Literal["mkdir", "rename", "delete"]
    new_name: str | None = Field(None, max_length=255, description="重命名时的新名称")


class FileOpResultOut(BaseModel):
    """目录级操作结果。"""

    message: str
    count: int = 1
