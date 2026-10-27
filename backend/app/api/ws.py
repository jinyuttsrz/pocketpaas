"""M5 WebSocket 接口：容器日志实时推送 + Web 终端。

连接即推送：先回放最近 tail 条历史，再实时跟随新输出；
容器停止/被删时发送 eof 后关闭；连接断开自动释放 docker 流。

Web 终端：在运行中容器内开一个 TTY 交互 shell（docker exec），
浏览器输入 ↔ 容器 stdin/stdout 双向桥接。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import threading
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.services import app_service, auth_service, docker_service

router = APIRouter()


def _msg(type_: str, **extra: Any) -> str:
    return json.dumps({"type": type_, **extra}, ensure_ascii=False)


@router.websocket("/ws/apps/{app_id}/logs")
async def stream_app_logs(websocket: WebSocket, app_id: int, db: Session = Depends(get_db)):
    """实时日志流：/ws/apps/{id}/logs?tail=100。

    docker logs 的 tail+follow 原生语义 = 历史回放 + 实时追加，一次连接全覆盖。
    """
    await websocket.accept()

    # 鉴权：优先取 Cookie（浏览器），其次取查询参数 token（脚本 / 客户端）
    user = None
    if settings.ENABLE_AUTH:
        token = websocket.cookies.get("paas_token") or websocket.query_params.get("token")
        user = auth_service.resolve_session(db, token)
        if user is None:
            await websocket.send_text(_msg("error", message="未登录或登录已过期"))
            await websocket.close(code=4401)
            return

    try:
        # 归属校验：普通用户仅能查看自己应用的日志，管理员可看全部
        app = app_service.get_app(db, app_id, user)
        # 服务选择：未指定时取公开服务；多容器应用可指定具体服务
        service, ref = app_service.resolve_service_container(
            app, websocket.query_params.get("service")
        )
    except app_service.AppError as exc:
        await websocket.send_text(_msg("error", message=str(exc)))
        await websocket.close(code=4404)
        return

    tail = int(websocket.query_params.get("tail", "200"))
    service_name = service.name if service is not None else "default"

    try:
        stream = docker_service.stream_logs(ref, tail=max(tail, 0))
    except docker_service.ContainerNotFoundError:
        await websocket.send_text(_msg("error", message="容器不存在：请先部署应用"))
        await websocket.close(code=4404)
        return
    except docker_service.DockerUnavailableError as exc:
        await websocket.send_text(_msg("error", message=str(exc)))
        await websocket.close(code=1011)
        return

    await websocket.send_text(_msg("open", app=app.name, service=service_name, container=ref))
    try:
        loop = asyncio.get_running_loop()
        while True:
            # docker SDK 为同步流，放线程池执行避免阻塞事件循环
            line = await loop.run_in_executor(None, next, stream, None)
            if line is None:
                await websocket.send_text(_msg("eof", message="容器已停止或退出"))
                break
            await websocket.send_text(_msg("log", line=line.rstrip("\r\n")))
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # 发送阶段客户端已断开
        pass
    finally:
        stream.close()


# ---------------------------------------------------------------------------
# Web 终端：交互式 shell
# ---------------------------------------------------------------------------


@router.websocket("/ws/apps/{app_id}/shell")
async def shell_session(websocket: WebSocket, app_id: int, db: Session = Depends(get_db)):
    """Web 终端：/ws/apps/{id}/shell（在运行中容器内开 TTY shell）。

    客户端 → 服务端消息：
      {"type":"input","data":"ls -al\\n"}        键盘输入
      {"type":"interrupt"}                       等价于 Ctrl+C
      {"type":"resize","height":30,"width":120}  终端尺寸变化
    服务端 → 客户端消息：
      open / output（data 为 base64 的原始字节，避免非法 UTF-8 截断）/ eof / error

    会话终止时显式结束 exec（先 Ctrl+C 再 exit），避免容器内残留 shell 进程。
    """
    await websocket.accept()

    user = None
    if settings.ENABLE_AUTH:
        token = websocket.cookies.get("paas_token") or websocket.query_params.get("token")
        user = auth_service.resolve_session(db, token)
        if user is None:
            await websocket.send_text(_msg("error", message="未登录或登录已过期"))
            await websocket.close(code=4401)
            return

    try:
        app = app_service.get_app(db, app_id, user)
        service, ref = app_service.resolve_service_container(
            app, websocket.query_params.get("service")
        )
    except app_service.AppError as exc:
        await websocket.send_text(_msg("error", message=str(exc)))
        await websocket.close(code=4404)
        return

    try:
        session = docker_service.open_shell(ref)
    except docker_service.ContainerNotFoundError as exc:
        await websocket.send_text(_msg("error", message=str(exc)))
        await websocket.close(code=4404)
        return
    except docker_service.DockerUnavailableError as exc:
        await websocket.send_text(_msg("error", message=str(exc)))
        await websocket.close(code=1011)
        return

    await websocket.send_text(
        _msg(
            "open",
            app=app.name,
            service=service.name if service is not None else "default",
            container=ref,
            target="容器文件系统",
        )
    )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _pump() -> None:
        """读线程：把 exec 输出搬进事件循环队列（None 表示流结束）。"""
        while True:
            data = session.read()
            if not data:
                loop.call_soon_threadsafe(queue.put_nowait, None)
                return
            loop.call_soon_threadsafe(queue.put_nowait, data)

    threading.Thread(
        target=_pump, name=f"paas-shell-{app.id}", daemon=True
    ).start()

    async def _forward_output() -> None:
        try:
            while True:
                data = await queue.get()
                if data is None:
                    await websocket.send_text(_msg("eof", message="终端会话已结束"))
                    return
                await websocket.send_text(
                    _msg("output", data=base64.b64encode(data).decode("ascii"))
                )
        except (WebSocketDisconnect, RuntimeError):
            return

    async def _receive_input() -> None:
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "input":
                    text = str(msg.get("data", ""))
                    await loop.run_in_executor(None, session.write, text.encode("utf-8"))
                elif kind == "interrupt":
                    await loop.run_in_executor(None, session.write, b"\x03")
                elif kind == "resize":
                    try:
                        h = int(msg.get("height") or 0)
                        w = int(msg.get("width") or 0)
                    except (TypeError, ValueError):
                        continue
                    if h > 0 and w > 0:
                        await loop.run_in_executor(
                            None, session.resize, min(h, 200), min(w, 500)
                        )
        except (WebSocketDisconnect, RuntimeError):
            return

    out_task = asyncio.create_task(_forward_output())
    in_task = asyncio.create_task(_receive_input())
    try:
        await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (out_task, in_task):
            task.cancel()
        # 收尾放在独立线程里并等待完成：客户端断开时本协程可能被取消，
        # 用 await 调度的收尾会被打断，导致容器内残留 exec 出来的 sh 进程（实测会泄漏）。
        closer = threading.Thread(
            target=session.close, name=f"paas-shell-close-{app.id}", daemon=True
        )
        closer.start()
        closer.join(timeout=5)
        with contextlib.suppress(Exception):
            await websocket.close()
