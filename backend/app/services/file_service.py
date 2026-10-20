"""项目文件管理服务：统一「宿主源码目录」与「运行中容器文件系统」的读写。

两个目标共用一套最小接口（list / read / write / mkdir / rename / delete / stream）：

- source   ：workspace/{app_id}/src，即构建镜像的源码目录，改动持久保存；
- container：运行中容器内的文件系统，改动立即生效但重新部署即丢失。

安全与稳健性要点：
- 路径一律经 `safe_join` 归一化，拒绝绝对路径与 `..`，并复用 source_service._is_within 二次校验；
- 容器侧列出目录优先 `sh` 执行（省流量），镜像无 sh 时回退 tar 流式解析；
- 容器侧读写文件走 get_archive / put_archive，不依赖 shell，二进制安全；
- 所有流式操作在生成器内部自持 docker client（client() 是上下文管理器，关闭后流即失效）。
"""

from __future__ import annotations

import io
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Protocol

import docker
from docker.errors import APIError, DockerException, NotFound

from app.core.config import settings
from app.services import docker_service, source_service

TARGET_SOURCE = "source"
TARGET_CONTAINER = "container"
TARGETS = (TARGET_SOURCE, TARGET_CONTAINER)

TARGET_LABELS = {TARGET_SOURCE: "项目源码", TARGET_CONTAINER: "容器文件"}

_CHUNK = 1 << 20
# 容器列目录：NUL 分隔输出，规避文件名含空格 / 换行 / 制表符带来的歧义
_LIST_SCRIPT = (
    'cd "$1" 2>/dev/null || exit 3; '
    "for f in .[!.]* *; do [ -e \"$f\" ] || continue; "
    "if [ -d \"$f\" ]; then printf 'd\\000%s\\000\\000' \"$f\"; "
    "else printf 'f\\000%s\\000%s\\000' \"$f\" \"$(wc -c < \"$f\" 2>/dev/null || echo 0)\"; fi; done"
)


class FileError(RuntimeError):
    """文件操作失败（映射为 400）。"""


class FileNotFound(FileError):
    """目标文件 / 目录不存在（映射为 404）。"""


class FileTargetError(FileError):
    """目标当前不可用，如容器未运行（映射为 409）。"""


@dataclass
class FileEntry:
    """目录中的一个条目。"""

    name: str
    path: str
    is_dir: bool
    size: int | None = None
    mtime: str | None = None


@dataclass
class FileStream:
    """可下载的产物：本地路径 + 建议文件名 + 是否临时文件（需用完即删）。"""

    path: Path
    download_name: str
    is_temp: bool


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------


def _norm_parts(rel: str | None) -> list[str]:
    """把用户输入的路径拆成安全分段：去掉空段与「.」，拒绝绝对路径与「..」。"""
    raw = (rel or "").replace("\\", "/").strip()
    if raw.startswith("/"):
        raise FileError("路径必须使用相对形式")
    parts: list[str] = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise FileError("路径不允许包含 ..")
        parts.append(seg)
    return parts


def _check_name(name: str) -> str:
    """校验单个文件名：非空、不含路径分隔符、不为 . / .."""
    clean = (name or "").strip()
    if not clean or clean in (".", "..") or "/" in clean or "\\" in clean:
        raise FileError("文件名不合法")
    return clean


def safe_join(base: Path, rel: str | None) -> Path:
    """在 base 之下拼出绝对路径；越界即拒绝。"""
    parts = _norm_parts(rel)
    target = base.joinpath(*parts)
    if not source_service._is_within(base, target):
        raise FileError("路径越界")
    return target


def rel_path(parts: list[str]) -> str:
    return "/".join(parts)


def normalize_path(rel: str | None) -> str:
    """归一化后的相对路径；根目录返回空串。"""
    return rel_path(_norm_parts(rel))


def parent_path(rel: str | None) -> str | None:
    """返回上一级路径；已在根目录时返回 None。"""
    parts = _norm_parts(rel)
    if not parts:
        return None
    return rel_path(parts[:-1])


def _fmt_mtime(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def decode_text(data: bytes) -> tuple[str | None, bool]:
    """尝试按 UTF-8 解码；失败视为二进制（返回 (None, False)）。"""
    if b"\x00" in data[:4096]:
        return None, False
    try:
        return data.decode("utf-8"), True
    except UnicodeDecodeError:
        return None, False


# ---------------------------------------------------------------------------
# 容器侧底层原语
# ---------------------------------------------------------------------------


class _ChunkReader:
    """把 docker get_archive 的分块迭代器包装成 tarfile 可用的文件对象。

    超过 max_bytes 后返回 EOF 并置 truncated，避免超大目录把内存 / 磁盘吃满。
    """

    def __init__(self, chunks: Iterator[bytes], max_bytes: int | None = None) -> None:
        self._it = iter(chunks)
        self._buf = bytearray()
        self._eof = False
        self._read = 0
        self._max = max_bytes
        self.truncated = False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            for chunk in self._it:
                self._buf.extend(chunk)
            self._eof = True
            out = bytes(self._buf)
            self._buf.clear()
            return out
        while len(self._buf) < size and not self._eof:
            if self._max is not None and self._read >= self._max:
                self.truncated = True
                self._eof = True
                break
            try:
                chunk = next(self._it)
            except StopIteration:
                self._eof = True
                break
            self._read += len(chunk)
            self._buf.extend(chunk)
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out


def _tar_first_file(chunks: Iterator[bytes], max_bytes: int) -> tuple[bytes, bool]:
    """从归档流中取出首个普通文件的内容（容器读文件 / 下载单文件用）。

    读取到超过 max_bytes 即返回，并用 truncated 标记（只按需拉取，不会整包驻留）。
    """
    reader = _ChunkReader(chunks)
    with tarfile.open(fileobj=reader, mode="r|") as tf:
        for member in tf:
            if not member.isfile():
                continue
            handle = tf.extractfile(member)
            if handle is None:
                continue
            data = bytearray()
            while True:
                block = handle.read(_CHUNK)
                if not block:
                    break
                data.extend(block)
                if len(data) > max_bytes:
                    return bytes(data[:max_bytes]), True
            return bytes(data), False
    raise FileNotFound("文件不存在或不可读")


def _tar_stream_to_zip(chunks: Iterator[bytes], dest: Path, strip_prefix: str) -> None:
    """归档流 → zip（容器目录打包下载用）。"""
    reader = _ChunkReader(chunks)
    prefix = strip_prefix.rstrip("/") + "/" if strip_prefix else ""
    try:
        with tarfile.open(fileobj=reader, mode="r|") as tf:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for member in tf:
                    name = member.name.lstrip("/")
                    if prefix and name.startswith(prefix):
                        name = name[len(prefix) :]
                    if not name or name in (".", "./"):
                        continue
                    if member.isdir():
                        continue
                    handle = tf.extractfile(member)
                    if handle is None:
                        continue
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with zf.open(info, "w") as out:
                        shutil.copyfileobj(handle, out, _CHUNK)
    except tarfile.TarError as exc:
        raise FileError(f"归档解析失败：{exc}") from exc


def _pack_tar(name: str, data: bytes, mode: int = 0o644) -> bytes:
    """把单个文件打成 tar 字节流（put_archive 用）。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = mode
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _exec(container, argv: list[str]) -> None:
    """在容器内执行命令（argv 形式，不做字符串拼接）；非 0 退出码抛错。"""
    try:
        result = container.exec_run(argv, user="root")
    except APIError as exc:
        raise FileError(f"容器内执行失败：{exc}") from exc
    if result.exit_code != 0:
        detail = (result.output or b"").decode("utf-8", "replace").strip()
        raise FileError(f"容器内执行失败（exit={result.exit_code}）：{detail[:300]}")


# ---------------------------------------------------------------------------
# 后端实现
# ---------------------------------------------------------------------------


class _Backend(Protocol):
    """两个目标共用的最小接口。"""

    def list(self, rel: str) -> tuple[list[FileEntry], bool]: ...

    def read(self, rel: str, max_bytes: int) -> tuple[bytes, bool]: ...

    def write(self, rel: str, data: bytes) -> None: ...

    def mkdir(self, rel: str) -> None: ...

    def rename(self, rel: str, new_name: str) -> None: ...

    def delete(self, rel: str) -> None: ...

    def stream(self, rel: str, max_bytes: int) -> FileStream: ...

    def availability(self) -> tuple[bool, str | None]: ...


class _SourceBackend:
    """宿主源码目录后端。"""

    target = TARGET_SOURCE

    def __init__(self, app_id: int) -> None:
        self._app_id = app_id

    def root(self) -> Path:
        return source_service.source_dir(self._app_id)

    def availability(self) -> tuple[bool, str | None]:
        return True, None

    def list(self, rel: str) -> tuple[list[FileEntry], bool]:
        base = safe_join(self.root(), rel)
        if not base.is_dir():
            raise FileNotFound("目录不存在")
        entries: list[FileEntry] = []
        for item in base.iterdir():
            try:
                stat = item.lstat()
            except OSError:
                continue
            is_dir = item.is_dir() and not item.is_symlink()
            entries.append(
                FileEntry(
                    name=item.name,
                    path=rel_path(_norm_parts(rel) + [item.name]),
                    is_dir=is_dir,
                    size=None if is_dir else stat.st_size,
                    mtime=_fmt_mtime(stat.st_mtime),
                )
            )
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries, False

    def read(self, rel: str, max_bytes: int) -> tuple[bytes, bool]:
        path = safe_join(self.root(), rel)
        if not path.is_file():
            raise FileNotFound("文件不存在")
        size = path.stat().st_size
        with open(path, "rb") as handle:
            data = handle.read(max_bytes)
        return data, size > max_bytes

    def write(self, rel: str, data: bytes) -> None:
        path = safe_join(self.root(), rel)
        if path.is_dir():
            raise FileError("目标是目录，无法写入")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def mkdir(self, rel: str) -> None:
        path = safe_join(self.root(), rel)
        if path.exists():
            raise FileError("同名文件或目录已存在")
        path.mkdir(parents=True, exist_ok=True)

    def rename(self, rel: str, new_name: str) -> None:
        parts = _norm_parts(rel)
        if not parts:
            raise FileError("不能重命名根目录")
        src = safe_join(self.root(), rel)
        if not src.exists():
            raise FileNotFound("文件或目录不存在")
        dest = src.parent / _check_name(new_name)
        if dest.exists():
            raise FileError("同名文件或目录已存在")
        src.rename(dest)

    def delete(self, rel: str) -> None:
        parts = _norm_parts(rel)
        if not parts:
            raise FileError("不能删除根目录")
        path = safe_join(self.root(), rel)
        if not path.exists():
            raise FileNotFound("文件或目录不存在")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)

    def stream(self, rel: str, max_bytes: int) -> FileStream:
        path = safe_join(self.root(), rel)
        if not path.exists():
            raise FileNotFound("文件或目录不存在")
        if path.is_dir() and not path.is_symlink():
            dest = Path(tempfile.mkstemp(suffix=".zip", prefix="paas_arc_")[1])
            _zip_source_dir(path, dest, max_bytes)
            return FileStream(dest, f"{path.name or 'project'}.zip", True)
        return FileStream(path, path.name, False)


def _zip_source_dir(src: Path, dest: Path, max_bytes: int) -> None:
    """把源码目录打成 zip（内容相对该目录，不额外套一层目录名）。"""
    total = 0
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(src.rglob("*")):
                if item.is_dir() or item.is_symlink() or not item.is_file():
                    continue
                total += item.stat().st_size
                if total > max_bytes:
                    raise FileError(
                        f"打包体积超过上限 {settings.FILE_MAX_ARCHIVE_MB}MB，请改为下载子目录"
                    )
                info = zipfile.ZipInfo(item.relative_to(src).as_posix())
                info.compress_type = zipfile.ZIP_DEFLATED
                with zf.open(info, "w") as out, open(item, "rb") as handle:
                    shutil.copyfileobj(handle, out, _CHUNK)
    except FileError:
        dest.unlink(missing_ok=True)
        raise


class _ContainerBackend:
    """运行中容器文件系统后端。"""

    target = TARGET_CONTAINER

    def __init__(self, app) -> None:
        self._app = app
        self._ref = docker_service.container_name_for(app.id, app.name)
        self._base: Path | None = None

    # -- 容器解析 --------------------------------------------------------

    def _open(self, cli):
        try:
            return cli.containers.get(self._ref)
        except NotFound as exc:
            raise FileNotFound("容器不存在，请先部署应用") from exc

    def _root(self, container) -> Path:
        if self._base is None:
            workdir = ((container.attrs or {}).get("Config") or {}).get("WorkingDir") or "/"
            self._base = Path(workdir if workdir.startswith("/") else "/" + workdir)
        return self._base

    def availability(self) -> tuple[bool, str | None]:
        """容器是否存在且运行中。"""
        try:
            with docker_service.client() as cli:
                container = self._open(cli)
                status = container.status
        except FileNotFound as exc:
            return False, str(exc)
        except DockerException:
            return False, "Docker 引擎连接异常，暂时无法访问容器文件"
        if status != "running":
            return False, "容器未运行，无法浏览容器内文件，请先启动应用"
        return True, None

    def _require(self, cli):
        """取容器并校验运行状态。"""
        container = self._open(cli)
        if container.status != "running":
            raise FileTargetError("容器未运行，无法浏览容器内文件，请先启动应用")
        return container

    # -- 接口实现 --------------------------------------------------------

    def _probe(self, container, flag: str, path: Path) -> bool | None:
        """容器内 exists 探测（test 是 shell 内建，必须经 sh 调用）。

        返回 True / False；镜像无 sh 时返回 None（未知，不做拦截）。
        """
        try:
            result = container.exec_run(
                ["sh", "-c", f'test {flag} "$1"', "sh", str(path)], user="root"
            )
        except (APIError, DockerException):
            return None
        if result.exit_code == 0:
            return True
        if result.exit_code == 1:
            return False
        return None

    def list(self, rel: str) -> tuple[list[FileEntry], bool]:
        parts = _norm_parts(rel)
        with docker_service.client() as cli:
            container = self._require(cli)
            abs_dir = safe_join(self._root(container), rel)
            if parts and self._probe(container, "-d", abs_dir) is False:
                raise FileNotFound("目录不存在")
            try:
                return self._list_via_shell(container, abs_dir, parts), False
            except _ShellUnavailable:
                return self._list_via_archive(container, abs_dir, parts)

    def _list_via_shell(self, container, abs_dir: Path, parts: list[str]) -> list[FileEntry]:
        """主路径：容器内 sh 枚举目录（省流量，支持大目录）。"""
        try:
            result = container.exec_run(
                ["sh", "-c", _LIST_SCRIPT, "sh", str(abs_dir)], user="root"
            )
        except (APIError, DockerException) as exc:
            raise _ShellUnavailable(str(exc)) from exc
        if result.exit_code == 3:
            raise FileNotFound("目录不存在")
        if result.exit_code != 0:
            raise _ShellUnavailable(f"exit={result.exit_code}")
        tokens = (result.output or b"").split(b"\0")
        entries: list[FileEntry] = []
        for i in range(0, len(tokens) - 2, 3):
            kind = tokens[i].decode("utf-8", "replace")
            name = tokens[i + 1].decode("utf-8", "replace")
            if kind not in ("d", "f") or not name:
                continue
            raw_size = tokens[i + 2].decode("utf-8", "replace").strip()
            is_dir = kind == "d"
            entries.append(
                FileEntry(
                    name=name,
                    path=rel_path(parts + [name]),
                    is_dir=is_dir,
                    size=None if is_dir else (int(raw_size) if raw_size.isdigit() else None),
                )
            )
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def _list_via_archive(self, container, abs_dir: Path, parts: list[str]) -> tuple[list[FileEntry], bool]:
        """兜底路径：无 sh 的镜像走归档流解析（超大目录可能截断）。"""
        chunks, _stat = self._chunks_of(container, abs_dir)
        reader = _ChunkReader(chunks, settings.FILE_MAX_ARCHIVE_MB * 1024 * 1024)
        base_name = abs_dir.name
        seen: dict[str, FileEntry] = {}
        try:
            with tarfile.open(fileobj=reader, mode="r|") as tf:
                for member in tf:
                    name = member.name.lstrip("/")
                    if name == base_name:
                        continue
                    if name.startswith(base_name + "/"):
                        name = name[len(base_name) + 1 :]
                    if not name or "/" in name.rstrip("/"):
                        continue
                    entry_name = name.rstrip("/")
                    if not entry_name or entry_name in seen:
                        continue
                    seen[entry_name] = FileEntry(
                        name=entry_name,
                        path=rel_path(parts + [entry_name]),
                        is_dir=member.isdir(),
                        size=None if member.isdir() else member.size,
                    )
        except tarfile.TarError:
            # 截断导致的解析中断：返回已解析到的部分
            pass
        entries = list(seen.values())
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries, reader.truncated

    def _chunks_of(self, container, abs_path: Path):
        try:
            return container.get_archive(str(abs_path))
        except NotFound as exc:
            raise FileNotFound("文件或目录不存在") from exc
        except APIError as exc:
            raise FileError(f"读取容器文件失败：{exc}") from exc

    def _kind(self, container, abs_path: Path) -> str | None:
        """判定容器内路径类型：dir / file / missing / None（无法判定）。

        注意 docker 归档头里的 mode 是 Go FileMode（目录类型位在 bit31），
        不能按 POSIX st_mode 判断，故统一用容器内 test 探测。
        """
        script = 'if [ -d "$1" ]; then echo d; elif [ -f "$1" ]; then echo f; else echo x; fi'
        try:
            result = container.exec_run(
                ["sh", "-c", script, "sh", str(abs_path)], user="root"
            )
        except (APIError, DockerException):
            return None
        if result.exit_code != 0:
            return None
        tag = (result.output or b"").decode("utf-8", "replace").strip()
        if tag == "d":
            return "dir"
        if tag == "f":
            return "file"
        return "missing"

    def read(self, rel: str, max_bytes: int) -> tuple[bytes, bool]:
        parts = _norm_parts(rel)
        if not parts:
            raise FileError("缺少文件名")
        with docker_service.client() as cli:
            container = self._require(cli)
            abs_path = safe_join(self._root(container), rel)
            kind = self._kind(container, abs_path)
            if kind == "missing":
                raise FileNotFound("文件不存在")
            if kind == "dir":
                raise FileError("目标是目录，无法直接编辑")
            chunks, _stat = self._chunks_of(container, abs_path)
            return _tar_first_file(chunks, max_bytes)

    def write(self, rel: str, data: bytes) -> None:
        parts = _norm_parts(rel)
        if not parts:
            raise FileError("缺少文件名")
        with docker_service.client() as cli:
            container = self._require(cli)
            root = self._root(container)
            target = safe_join(root, rel)
            parent = target.parent
            if not str(parent) == str(root):
                try:
                    _exec(container, ["mkdir", "-p", str(parent)])
                except FileError:
                    pass
            try:
                ok = container.put_archive(str(parent), _pack_tar(parts[-1], data))
            except (APIError, DockerException) as exc:
                raise FileError(f"写入容器文件失败：{exc}") from exc
            if not ok:
                raise FileError("写入容器文件失败：父目录不可写")

    def mkdir(self, rel: str) -> None:
        parts = _norm_parts(rel)
        if not parts:
            raise FileError("缺少目录名")
        with docker_service.client() as cli:
            container = self._require(cli)
            target = safe_join(self._root(container), rel)
            if self._probe(container, "-e", target) is True:
                raise FileError("同名文件或目录已存在")
            _exec(container, ["mkdir", "-p", str(target)])

    def rename(self, rel: str, new_name: str) -> None:
        parts = _norm_parts(rel)
        if not parts:
            raise FileError("不能重命名根目录")
        clean = _check_name(new_name)
        with docker_service.client() as cli:
            container = self._require(cli)
            src = safe_join(self._root(container), rel)
            if self._probe(container, "-e", src) is False:
                raise FileNotFound("文件或目录不存在")
            dest = src.parent / clean
            if self._probe(container, "-e", dest) is True:
                raise FileError("同名文件或目录已存在")
            _exec(container, ["mv", "--", str(src), str(dest)])

    def delete(self, rel: str) -> None:
        parts = _norm_parts(rel)
        if not parts:
            raise FileError("不能删除根目录")
        with docker_service.client() as cli:
            container = self._require(cli)
            target = safe_join(self._root(container), rel)
            if self._probe(container, "-e", target) is False:
                raise FileNotFound("文件或目录不存在")
            _exec(container, ["rm", "-rf", "--", str(target)])

    def stream(self, rel: str, max_bytes: int) -> FileStream:
        parts = _norm_parts(rel)
        with docker_service.client() as cli:
            container = self._require(cli)
            abs_path = safe_join(self._root(container), rel)
            kind = self._kind(container, abs_path) if parts else "dir"
            if kind == "missing":
                raise FileNotFound("文件或目录不存在")
            chunks, stat = self._chunks_of(container, abs_path)
            name = Path(stat.get("name") or abs_path.name).name
            if kind != "dir":
                data, truncated = _tar_first_file(chunks, max_bytes)
                if truncated:
                    raise FileError(
                        f"文件体积超过下载上限 {settings.FILE_MAX_ARCHIVE_MB}MB，无法下载"
                    )
                dest = Path(tempfile.mkstemp(prefix="paas_dl_")[1])
                dest.write_bytes(data)
                return FileStream(dest, name, True)
            if (stat.get("size") or 0) > max_bytes:
                raise FileError(
                    f"打包体积超过上限 {settings.FILE_MAX_ARCHIVE_MB}MB，请改为下载子目录"
                )
            dest = Path(tempfile.mkstemp(suffix=".zip", prefix="paas_arc_")[1])
            # 归档流在 client 关闭后即失效，必须在同一连接内消费完毕
            _tar_stream_to_zip(chunks, dest, name)
            return FileStream(dest, f"{name or 'container'}.zip", True)


class _ShellUnavailable(Exception):
    """容器内没有可用的 sh，需回退归档解析。"""


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def resolve_backend(db, app_id: int, target: str, user) -> tuple[_Backend, object]:
    """按目标解析后端，并做应用归属校验。"""
    from app.services import app_service  # 延迟导入，避免循环依赖

    if target not in TARGETS:
        raise FileError(f"未知的文件目标：{target}")
    app = app_service.get_app(db, app_id, user)
    if target == TARGET_SOURCE:
        return _SourceBackend(app.id), app
    return _ContainerBackend(app), app


def target_states(db, app_id: int, user) -> dict[str, dict]:
    """返回两个目标的可用性快照，供前端置灰不可用目标。"""
    from app.services import app_service

    app = app_service.get_app(db, app_id, user)
    source_ok, source_reason = _SourceBackend(app.id).availability()
    container_ok, container_reason = _ContainerBackend(app).availability()
    return {
        TARGET_SOURCE: {
            "label": TARGET_LABELS[TARGET_SOURCE],
            "ready": source_ok,
            "reason": source_reason,
        },
        TARGET_CONTAINER: {
            "label": TARGET_LABELS[TARGET_CONTAINER],
            "ready": container_ok,
            "reason": container_reason,
        },
    }
