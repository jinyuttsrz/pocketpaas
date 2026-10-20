"""M7 安全加固服务：输入校验、SSRF 防护、Dockerfile 危险指令扫描。

设计原则：
- 纯校验逻辑，不依赖 docker-py，避免与 docker_service 循环导入；
- 所有拒绝都抛出 SecurityError（由接口层映射为 400），信息对用户可读；
- 加固开关集中在 config.py，便于按环境（本地宽松 / 线上严格）切换。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import settings


class SecurityError(RuntimeError):
    """安全校验未通过（映射为 4xx）。"""


# ---------------------------------------------------------------------------
# 1) 应用名校验：白名单字符，杜绝注入到容器名 / 域名 / 文件路径
# ---------------------------------------------------------------------------

# 仅允许小写字母、数字与中划线，必须以字母数字开头结尾（合法 DNS label / 容器名）
_APP_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# 保留名：避免与平台自身路由 / 系统子域冲突
# app：线上常把控制台放在 app.{BASE_DOMAIN}，而该域名落在应用泛解析
#      *.{BASE_DOMAIN} 内，若放行会导致 nginx 出现同名 server 块。
_RESERVED_NAMES = {"api", "www", "admin", "static", "ws", "localhost", "paas", "app"}


def validate_app_name(name: str) -> None:
    """应用名合法性校验：长度、字符集、保留字。

    应用名会直接参与容器命名、Nginx server_name 与子域名拼接，
    必须收敛为严格的 DNS label 字符集，防止配置注入。
    """
    if not name or len(name) > 63:
        raise SecurityError("应用名长度需在 1-63 个字符之间")
    if not _APP_NAME_RE.match(name):
        raise SecurityError(
            "应用名仅允许小写字母、数字与中划线，且需以字母或数字开头结尾"
        )
    if name in _RESERVED_NAMES:
        raise SecurityError(f"应用名 {name!r} 为系统保留名，请更换")


# ---------------------------------------------------------------------------
# 2) Git 源 SSRF 防护：拒绝指向本机 / 内网 / 云元数据的仓库地址
# ---------------------------------------------------------------------------

# 云厂商元数据服务地址（SSRF 常见目标，窃取临时凭据）
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "100.100.100.200"}


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断 IP 是否属于不允许访问的网段。"""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_git_url(git_url: str) -> None:
    """Git 仓库地址校验：协议白名单 + SSRF 内网/元数据地址防护。

    仅允许 http/https；解析主机名后，若其解析到的任一 IP 落在
    私网 / 回环 / 链路本地 / 云元数据地址，则拒绝，防止借构建机探测内网。
    """
    url = (git_url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise SecurityError("仅支持 http(s) 协议的 Git 地址")

    if not settings.BLOCK_PRIVATE_GIT_HOST:
        return

    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise SecurityError("无法解析 Git 地址中的主机名")
    if host in _METADATA_HOSTS:
        raise SecurityError("拒绝访问云元数据地址")

    # 主机名本身就是 IP 的情况
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            raise SecurityError(f"拒绝访问内网 / 本机地址：{host}")
        return
    except ValueError:
        pass

    # 域名：解析所有 A/AAAA 记录逐一校验，任一命中内网即拒绝
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SecurityError(f"Git 主机名无法解析：{host}") from exc
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise SecurityError(f"Git 地址 {host} 解析到内网地址 {addr}，已拒绝")


# ---------------------------------------------------------------------------
# 3) 自带 Dockerfile 危险指令扫描
# ---------------------------------------------------------------------------

# 危险模式：挂载宿主敏感路径、特权、宿主网络、设备直通等
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*FROM\s+scratch", re.I), "暂不支持 FROM scratch 基础镜像"),
    (re.compile(r"(^|\s)--privileged(\s|$)", re.I), "Dockerfile 不得声明特权模式"),
    (re.compile(r"(^|\s)--security-opt", re.I), "Dockerfile 不得覆盖安全选项"),
    (re.compile(r"(^|\s)--cap-add", re.I), "Dockerfile 不得额外添加 capability"),
    (re.compile(r"(^|\s)--network[= ]host", re.I), "Dockerfile 不得使用宿主网络"),
    (re.compile(r"(^|\s)--pid[= ]host", re.I), "Dockerfile 不得共享宿主 PID 命名空间"),
    (re.compile(r"--mount=[^\n]*source=/(?:etc|root|var/run|proc|sys)", re.I),
     "Dockerfile 不得挂载宿主敏感路径"),
    (re.compile(r"/var/run/docker\.sock", re.I), "Dockerfile 不得挂载 docker.sock"),
]


def scan_dockerfile(src: Path) -> None:
    """扫描源码目录内自带的 Dockerfile，命中危险指令即拒绝构建。

    仅在用户自带 Dockerfile（custom 项目）时有意义；平台自动生成的
    Dockerfile 天然安全。开关由 BLOCK_DANGEROUS_DOCKERFILE 控制。
    """
    if not settings.BLOCK_DANGEROUS_DOCKERFILE:
        return
    dockerfile = src / "Dockerfile"
    if not dockerfile.exists():
        return

    text = dockerfile.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for pattern, reason in _DANGEROUS_PATTERNS:
            if pattern.search(stripped):
                raise SecurityError(f"{reason}：{stripped[:120]}")


# ---------------------------------------------------------------------------
# 4) 容器运行时加固参数：供 docker_service.run_container 合入 run() kwargs
# ---------------------------------------------------------------------------


def container_security_kwargs() -> dict:
    """构造容器运行时加固参数（docker-py run/create 口径）。

    - no-new-privileges：禁止子进程通过 setuid 提权；
    - cap_drop=ALL + 最小 cap_add：收敛 Linux capability；
    - pids_limit：限制进程数，防 fork 炸弹；
    - mem_swappiness=0：禁用 swap，使内存上限真正硬性生效。
    开关关闭时返回空 dict（本地调试可宽松）。
    """
    if not settings.ENABLE_CONTAINER_HARDENING:
        return {}
    return {
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
        "cap_add": list(settings.CONTAINER_CAP_ADD),
        "pids_limit": settings.CONTAINER_PIDS_LIMIT,
        "mem_swappiness": 0,
    }
