"""内置示例代码包：供「模板与指南」页面一键创建演示应用。

示例包以 zip 形式存放在 DEMO_DIR（默认指向后端上一级的 demo 目录）。
stage_demo 会先复制到临时目录再交给上传链路——因为 ingest_source_zip 在
解压结束后会清理传入的 zip，直接传入原文件会把示例包删掉。
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

from app.core.config import settings


class DemoError(Exception):
    """示例包不存在或不可用。"""


def _demo_dir() -> Path:
    return Path(settings.DEMO_DIR).resolve()


def list_demos() -> list[dict]:
    """列出可用示例包（按文件名排序）。目录缺失时返回空列表。"""
    root = _demo_dir()
    if not root.is_dir():
        return []
    return [{"file": p.name, "name": p.stem} for p in sorted(root.glob("*.zip"))]


def stage_demo(file_name: str) -> Path:
    """按文件名取示例包并复制到临时路径。

    只取 basename 后拼到示例目录，天然阻断路径穿越；文件必须真实存在。
    """
    root = _demo_dir()
    target = root / Path(file_name).name
    if target.suffix.lower() != ".zip" or not target.is_file():
        raise DemoError(f"示例包不存在：{file_name}")
    tmp = Path(tempfile.gettempdir()) / f"paas_demo_{uuid.uuid4().hex}_{target.name}"
    shutil.copyfile(target, tmp)
    return tmp
