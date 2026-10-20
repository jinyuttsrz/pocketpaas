"""控制台页面路由：SPA 入口与登录页。

前端为「零构建静态 SPA」（原生 JS + SVG 图表），无需 node/npm 构建链，
由 FastAPI 直接托管，保证在 WSL / 2H4G 线上环境都能开箱即用。
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from fastapi import Response

router = APIRouter(tags=["console"])

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@router.get("/", include_in_schema=False)
def console() -> FileResponse:
    """控制台首页：返回 SPA 入口。"""
    return FileResponse(WEB_DIR / "index.html")


@router.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    """登录 / 注册页。"""
    return FileResponse(WEB_DIR / "login.html")


@router.get("/.well-known/appspecific/com.chrome.devtools.json", include_in_schema=False)
def ignore_chrome_devtools():
    return Response(status_code=204)