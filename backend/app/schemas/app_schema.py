"""应用相关请求 / 响应模型。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.core.config import settings

# 应用状态机
AppStatus = Literal["created", "building", "running", "stopped", "failed"]


class AppCreate(BaseModel):
    """创建应用请求。"""

    name: str = Field(..., min_length=1, max_length=64, description="应用名（唯一）")
    source_type: Literal["upload", "git"] = Field("upload", description="代码来源类型")
    git_url: str | None = Field(None, max_length=512, description="Git 仓库地址")
    image: str | None = Field(None, max_length=255, description="直接指定镜像（M2 阶段用）")
    container_port: int = Field(8000, ge=1, le=65535, description="容器内服务监听端口")
    mem_limit: str = Field("256m", min_length=1, description="内存上限，如 128m / 512m")
    # 不设硬上限：配额是否越界由 resource_service 对照宿主真实资源校验
    cpu_quota: float = Field(1.0, description="CPU 配额，1.0 表示 1 核")
    env: dict[str, str] | None = Field(None, description="容器环境变量")
    command: str | None = Field(None, description="覆盖容器启动命令")


class DemoSourceIn(BaseModel):
    """以平台内置示例包作为源码。"""

    demo: str = Field(..., min_length=1, max_length=255, description="示例包文件名，如 python-demo.zip")


class AppQuotaUpdate(BaseModel):
    """调整资源配额请求。

    与 ResourceCheckIn 一致，不设 ge/le 硬边界，
    越界与否由 resource_service 对照宿主真实资源判定并给出中文提示。
    """

    mem_limit: str = Field(..., min_length=1, description="内存上限，如 128m / 512m")
    cpu_quota: float = Field(..., description="CPU 配额，1.0 表示 1 核")


class AppConfigUpdate(BaseModel):
    """更新持久化容器配置（环境变量 / 启动命令），全部可选。"""

    env: dict[str, str] | None = Field(None, description="容器环境变量（键值对）")
    command: str | None = Field(None, max_length=512, description="覆盖容器启动命令")


class DeployIn(BaseModel):
    """部署请求（全部可选，缺省沿用应用已有配置）。"""

    image: str | None = Field(None, max_length=255, description="覆盖镜像（缺省用构建产物）")
    env: dict[str, str] | None = Field(None, description="容器环境变量")
    command: str | None = Field(None, description="覆盖容器启动命令")
    reassign_port: bool = Field(
        False, description="重新分配宿主端口（重新部署时使用，会换一个空闲端口）"
    )


class GitSourceIn(BaseModel):
    """以 Git 仓库作为源码来源。"""

    git_url: str = Field(..., min_length=1, max_length=512, description="Git 仓库地址（http/https）")


class BuildIn(BaseModel):
    """触发构建请求。"""

    container_port: int | None = Field(None, ge=1, le=65535, description="容器端口（缺省用应用配置）")


class ResourceCheckIn(BaseModel):
    """资源校验请求：容器端口（可选）+ 内存上限 + CPU 核数。

    新建向导会带上容器端口；详情页配额调整不涉及端口，可省略该字段。
    范围与格式校验统一在 resource_service 内完成，以便返回可读的中文提示，
    故此处的字段仅做类型约束，不设 ge/le 边界。
    """

    container_port: int | None = Field(None, description="容器内服务监听端口（可选）")
    mem_limit: str = Field(..., min_length=1, description="内存上限，如 256m / 512m")
    cpu_quota: float = Field(..., description="CPU 配额，1.0 表示 1 核")
    app_id: int | None = Field(
        None, description="被调整的应用 id（配额调整时传入，用于排除其旧配额）"
    )


class BuildLogOut(BaseModel):
    """单条构建日志。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    log_line: str
    ts: datetime


class DeploymentOut(BaseModel):
    """部署（构建）记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    app_id: int
    image_tag: str | None
    build_status: str
    container_id: str | None
    container_name: str | None
    port: int | None
    created_at: datetime


class BuildResultOut(DeploymentOut):
    """构建结果：在部署记录基础上附带"容器端口自动识别"的提示文案。"""

    port_notice: str | None = None


class RouteOut(BaseModel):
    """应用路由信息（M4 动态路由）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    app_id: int
    domain: str
    upstream_port: int
    ssl_enabled: bool


class MetricOut(BaseModel):
    """资源采样点（M5 负载曲线）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    app_id: int
    cpu: float
    mem: float
    ts: datetime


class AppOut(BaseModel):
    """应用响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    subdomain: str
    # 属主信息：管理员可据此区分应用归属；普通用户只会看到自己的应用
    owner_id: int | None = None
    owner_name: str | None = None
    source_type: str
    git_url: str | None
    source_path: str | None
    project_type: str | None
    status: str
    image: str | None
    container_port: int
    host_port: int | None
    mem_limit: str
    cpu_quota: float
    env: dict[str, str] = Field(default_factory=dict)
    command: str | None = None
    created_at: datetime

    @field_validator("env", mode="before")
    @classmethod
    def _parse_env(cls, value: object) -> dict[str, str]:
        """ORM 的 env 列是 JSON 文本；这里直接解析为字典返回给前端。"""
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value:
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    # 便捷访问入口：启用 Nginx 时走域名路由，否则本地端口直连
    @computed_field
    @property
    def access_url(self) -> str | None:
        if not self.host_port:
            return None
        if settings.ENABLE_NGINX:
            base = settings.BASE_DOMAIN.strip().lstrip(".")
            domain = f"{self.subdomain}.localhost" if not base else f"{self.subdomain}.{base}"
            https = bool(
                settings.USE_HTTPS and settings.SSL_CERT_PATH and settings.SSL_KEY_PATH
            )
            scheme = "https" if https else "http"
            # 对外端口取自 NGINX_PUBLIC_PORT（外层反代监听的口），
            # 而非 NGINX_LISTEN_PORT（平台内层 nginx 的口）：
            # 两层部署下内层跑 8080、外层跑 80，用户 URL 不应带内层端口。
            default_port = 443 if https else 80
            port = "" if settings.NGINX_PUBLIC_PORT == default_port else f":{settings.NGINX_PUBLIC_PORT}"
            return f"{scheme}://{domain}{port}"
        return f"http://localhost:{self.host_port}"


class ContainerOut(BaseModel):
    """容器状态响应。"""

    id: str
    name: str
    image: str
    status: str
    state: str
    app_id: int | None = None
    app_name: str | None = None
    host_port: int | None = None
    container_port: int | None = None
    mem_limit: int | None = None
    nano_cpus: int | None = None
    started_at: str | None = None
