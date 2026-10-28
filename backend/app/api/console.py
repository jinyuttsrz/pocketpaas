"""控制台页面路由：SPA 入口与登录页。

前端为「零构建静态 SPA」（原生 JS + SVG 图表），无需 node/npm 构建链，
由 FastAPI 直接托管，保证在 WSL / 2H4G 线上环境都能开箱即用。
"""

import time
from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["console"])

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 每次进程启动生成一个版本号，拼到静态资源 URL 上，避免浏览器继续使用旧 app.js / styles.css。
# 容器重建后版本自动变化，无需手工改 index.html。
STATIC_VERSION = str(int(time.time()))


def _render_page(name: str, reset: bool = False) -> HTMLResponse:
    text = (WEB_DIR / name).read_text(encoding="utf-8")
    text = text.replace("__STATIC_VERSION__", STATIC_VERSION)
    headers = {"Cache-Control": "no-cache, must-revalidate"}
    if reset:
        # 访问 /?reset=1 时清掉本站浏览器缓存，用于浏览器顽固缓存旧 JS/CSS 的兜底
        headers["Clear-Site-Data"] = '"cache"'
    return HTMLResponse(text, headers=headers)


@router.get("/", include_in_schema=False)
def console(reset: bool = False) -> HTMLResponse:
    """控制台首页：返回 SPA 入口，并注入静态资源版本号。"""
    return _render_page("index.html", reset=reset)


@router.get("/login", include_in_schema=False)
def login_page(reset: bool = False) -> HTMLResponse:
    """登录 / 注册页：同样注入静态资源版本号。"""
    return _render_page("login.html", reset=reset)


@router.get("/.well-known/appspecific/com.chrome.devtools.json", include_in_schema=False)
def ignore_chrome_devtools():
    return Response(status_code=204)