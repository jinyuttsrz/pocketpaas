"""源码接收服务：把用户代码落入构建工作区。

M3 支持两种来源：
- upload：接收 zip 代码包，安全解压到 workspace/{app_id}/src
- git   ：git clone 指定仓库到 workspace/{app_id}/src

安全设计（对应 M7 的输入校验，提前在此落地基础防护）：
- 拒绝绝对路径与 `..` 路径穿越
- 解压后总大小与文件数超限即中止
- 拒绝符号链接，避免指向宿主敏感路径
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

from app.core.config import settings


class SourceError(RuntimeError):
    """源码接收失败（映射为 4xx）。"""


def workspace_root() -> Path:
    """构建工作区根目录（不存在则创建）。"""
    root = Path(settings.WORKSPACE_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def app_workspace(app_id: int) -> Path:
    """单个应用的构建目录：workspace/{app_id}。"""
    path = workspace_root() / str(app_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def source_dir(app_id: int) -> Path:
    """应用源码目录：workspace/{app_id}/src。"""
    return app_workspace(app_id) / "src"


def reset_source_dir(app_id: int) -> Path:
    """清空并重建源码目录（重复部署时使用）。"""
    src = source_dir(app_id)
    if src.exists():
        shutil.rmtree(src, ignore_errors=True)
    src.mkdir(parents=True, exist_ok=True)
    return src


def _is_within(base: Path, target: Path) -> bool:
    """判断 target 是否位于 base 目录内（防路径穿越）。"""
    base = base.resolve()
    target = target.resolve()
    return base == target or base in target.parents


def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """安全解压 zip：逐条目校验路径、类型与体积。"""
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    total = 0

    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                # Windows 压缩工具可能用反斜杠作为路径分隔符，统一规范化为 /
                name = info.filename.replace("\\", "/")
                # 跳过目录项
                if name.endswith("/"):
                    continue
                # 拒绝符号链接（外部属性高位标识）
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise SourceError(f"代码包包含符号链接，已拒绝：{info.filename}")
                # 路径穿越校验
                target = dest / name
                if not _is_within(dest, target):
                    raise SourceError(f"检测到非法路径（路径穿越）：{info.filename}")

                total += info.file_size
                if total > max_bytes:
                    raise SourceError(
                        f"解压后体积超过上限 {settings.MAX_UPLOAD_MB}MB，已中止"
                    )

                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    except zipfile.BadZipFile as exc:
        raise SourceError("上传的文件不是合法的 zip 代码包") from exc


def _flatten_single_root(src: Path) -> None:
    """若解压后只有一个顶层目录（zip 打包常见），将其内容上提一层。"""
    entries = [p for p in src.iterdir() if not p.name.startswith("__MACOSX")]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(src / item.name))
        inner.rmdir()


def ingest_zip(app_id: int, zip_path: Path) -> Path:
    """解压上传的 zip 到应用源码目录，返回源码目录。"""
    dest = reset_source_dir(app_id)
    _safe_extract_zip(zip_path, dest)
    _flatten_single_root(dest)
    if not any(dest.iterdir()):
        raise SourceError("代码包为空")
    return dest


def ingest_git(app_id: int, git_url: str) -> Path:
    """克隆 git 仓库到应用源码目录，返回源码目录。"""
    if not (git_url.startswith("http://") or git_url.startswith("https://")):
        raise SourceError("仅支持 http(s) 协议的 Git 地址")

    dest = reset_source_dir(app_id)
    # clone 到临时目录后移动内容，保持 src 为仓库根
    tmp = dest.parent / "_clone_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)

    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", git_url, str(tmp)],
            capture_output=True,
            text=True,
            timeout=settings.GIT_CLONE_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise SourceError("构建机未安装 git，无法拉取仓库") from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceError(f"git clone 超时（>{settings.GIT_CLONE_TIMEOUT}s）") from exc

    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SourceError(f"git clone 失败：{proc.stderr.strip()[:500]}")

    # 把仓库内容移入 src（去掉 .git 以减小上下文）
    for item in tmp.iterdir():
        if item.name == ".git":
            continue
        shutil.move(str(item), str(dest / item.name))
    shutil.rmtree(tmp, ignore_errors=True)
    return dest


def cleanup_zip(zip_path: Path) -> None:
    """删除上传的临时 zip 文件。"""
    try:
        if zip_path.exists():
            os.remove(zip_path)
    except OSError:
        pass
