"""M4 动态路由服务：Nginx server 块自动渲染 + 语法校验 + 热载。

设计要点：
- 每个应用一个独立 conf 文件（paas-{app_id}.conf），部署/销毁只动自己的文件，
  互不影响；写坏时先回滚再校验，保证 Nginx 永远处于可用状态。
- 本地模式域名形如 {subdomain}.localhost（无需真实 DNS，curl/hosts 即可验证）；
  线上模式 {subdomain}.{BASE_DOMAIN}，配置 SSL 证书后自动渲染 443 server 块。
- 未配置证书时自动降级 HTTP，不阻塞交付闭环（对应实施计划的风险预案）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.core.config import settings


class RouteError(RuntimeError):
    """路由配置 / Nginx 操作失败。"""


def domain_for(subdomain: str) -> str:
    """应用主入口域名：本地 {sub}.localhost；线上 {sub}.{BASE_DOMAIN}。"""
    base = settings.BASE_DOMAIN.strip().lstrip(".")
    return f"{subdomain}.localhost" if not base else f"{subdomain}.{base}"


def _service_label(service_name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", service_name.lower()).strip("-")
    return (text or "svc")[:32].strip("-") or "svc"


def domain_for_service(subdomain: str, service_name: str) -> str:
    """非主入口公开服务的域名：{sub}-{service}.{BASE_DOMAIN}。"""
    label = f"{subdomain}-{_service_label(service_name)}"[:63].rstrip("-")
    return domain_for(label)


def conf_path(app_id: int) -> Path:
    """单个应用的路由配置文件路径。"""
    return Path(settings.NGINX_CONF_DIR) / f"paas-{app_id}.conf"


def ssl_ready() -> bool:
    """两项证书路径都配置才启用 HTTPS 渲染，否则自动降级 HTTP。"""
    return bool(settings.SSL_CERT_PATH and settings.SSL_KEY_PATH)


def _render_block(*, domain: str, upstream_port: int, ssl: bool) -> str:
    """渲染单个域名对应的 server block（HTTPS 时附带 80 → 443 跳转）。"""
    proxy = """        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;""".format(port=upstream_port)

    if ssl:
        return f"""# paas managed route (app): {domain} [https]
server {{
    listen 443 ssl;
    http2 on;
    server_name {domain};

    ssl_certificate {settings.SSL_CERT_PATH};
    ssl_certificate_key {settings.SSL_KEY_PATH};

    location / {{
{proxy}
    }}
}}

server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}
"""

    return f"""# paas managed route (app): {domain} [http]
server {{
    listen {settings.NGINX_LISTEN_PORT};
    server_name {domain};

    location / {{
{proxy}
    }}
}}
"""


def render_server_block(*, domain: str, upstream_port: int, ssl: bool) -> str:
    """渲染单条路由（保留旧接口）。"""
    return _render_block(domain=domain, upstream_port=upstream_port, ssl=ssl)


def render_server_blocks(routes: list[tuple[str, int]], ssl: bool) -> str:
    """把多个域名 → upstream 路由渲染为一个配置文件的多个 server block。"""
    return "\n".join(
        _render_block(domain=domain, upstream_port=port, ssl=ssl)
        for domain, port in routes
    )


def _run_nginx(args: list[str]) -> subprocess.CompletedProcess:
    """执行 nginx 命令行（-t 校验 / -s reload / 启动）。"""
    try:
        return subprocess.run(
            ["nginx", *args], capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError as exc:
        raise RouteError("nginx 未安装或不在 PATH 中") from exc
    except subprocess.TimeoutExpired as exc:
        raise RouteError("nginx 命令执行超时") from exc


def test_config() -> None:
    """nginx -t 语法校验，失败抛 RouteError（带 stderr 详情）。"""
    r = _run_nginx(["-t"])
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        raise RouteError(f"nginx 配置校验失败：{detail}")


def reload() -> None:
    """热载 Nginx：master 在跑则 reload，否则启动。"""
    running = subprocess.run(
        ["pgrep", "-x", "nginx"], capture_output=True
    ).returncode == 0
    r = _run_nginx(["-s", "reload"] if running else [])
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        raise RouteError(f"nginx {'reload' if running else '启动'}失败：{detail}")


def apply_routes(*, app_id: int, routes: list[tuple[str, int]]) -> bool:
    """写入并生效应用的全部公开路由（多个域名共用一个 conf 文件）。

    流程：渲染 → 写 conf → nginx -t（失败回滚）→ reload。
    返回是否启用 HTTPS（降级时为 False，由调用方落库 ssl_enabled）。
    """
    path = conf_path(app_id)
    if not routes:
        remove_route(app_id)
        return False

    ssl = ssl_ready()
    path.parent.mkdir(parents=True, exist_ok=True)

    backup = path.read_text(encoding="utf-8") if path.exists() else None
    content = render_server_blocks(routes, ssl)
    path.write_text(content, encoding="utf-8")

    try:
        test_config()
    except RouteError:
        # 回滚：无备份则删除新文件，保证 Nginx 不被坏配置卡死
        if backup is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(backup, encoding="utf-8")
        test_config()  # 确认回滚后配置可用
        raise
    reload()
    return ssl


def apply_route(*, app_id: int, domain: str, upstream_port: int) -> bool:
    """单路由兼容入口（单容器应用 / 旧调用方）。"""
    return apply_routes(app_id=app_id, routes=[(domain, upstream_port)])


def remove_route(app_id: int) -> None:
    """删除应用路由配置并热载（应用销毁时调用；文件不存在则跳过）。"""
    path = conf_path(app_id)
    if not path.exists():
        return
    path.unlink()
    try:
        test_config()
    except RouteError:
        raise
    reload()
