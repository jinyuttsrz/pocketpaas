"""ORM 数据模型（M1 初稿，对应实施计划第四节数据库设计）。"""

import json
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class App(Base):
    """应用主表。

    多租户隔离：`owner_id` 指向创建者，普通用户只能看到 / 操作自己的应用；
    管理员（role=admin）可跨用户查看与操作全部应用。
    应用名在同一属主下唯一（跨用户可重名），子域名因对外寻址仍需全局唯一。
    """

    __tablename__ = "apps"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_apps_owner_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 属主：创建该应用的用户；用户被删除时置空（应用保留，仅管理员可见）
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    subdomain: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), default="upload")  # upload / git
    git_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # M3：源码所在目录（构建工作区内）与识别出的项目类型
    source_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    project_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # python/static/node
    status: Mapped[str] = mapped_column(String(16), default="created")  # created/building/running/stopped/failed
    # M2：运行时信息与资源配额
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    container_port: Mapped[int] = mapped_column(Integer, default=8000)
    host_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mem_limit: Mapped[str] = mapped_column(String(16), default="256m")
    cpu_quota: Mapped[float] = mapped_column(Float, default=1.0)
    # 持久化容器配置：env 以 JSON 字符串存储（键值对），command 为覆盖启动命令
    env: Mapped[str | None] = mapped_column(Text, nullable=True)
    command: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Git Webhook：每个应用独立的 HMAC-SHA256 签名密钥（懒生成，可一键重置）
    webhook_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 多容器发布槽位：v{deployment_id}（旧版单/多容器部署记为 blue）
    active_slot: Mapped[str | None] = mapped_column(String(8), nullable=True)
    previous_slot: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    deployments: Mapped[list["Deployment"]] = relationship(
        back_populates="app", cascade="all, delete-orphan"
    )
    routes: Mapped[list["Route"]] = relationship(
        back_populates="app", cascade="all, delete-orphan"
    )
    services: Mapped[list["AppService"]] = relationship(
        back_populates="app", cascade="all, delete-orphan"
    )
    owner: Mapped["User | None"] = relationship(back_populates="apps")

    @property
    def env_map(self) -> dict[str, str]:
        """把 env 列的 JSON 文本解析为字典；无值或数据损坏时返回空字典。"""
        if not self.env:
            return {}
        try:
            data = json.loads(self.env)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def owner_name(self) -> str | None:
        """属主展示名：优先显示名，回退用户名；无属主（历史数据）返回 None。

        查询侧用 selectinload(App.owner) 预加载，避免序列化时触发 N+1 懒加载。
        """
        if self.owner is None:
            return None
        return self.owner.display_name or self.owner.username


class Deployment(Base):
    """每次部署记录。"""

    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"))
    image_tag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    build_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/building/success/failed
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mem_limit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cpu_quota: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 多容器发布：slot=blue/green，strategy=rolling/bluegreen/rollback
    slot: Mapped[str | None] = mapped_column(String(8), nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    app: Mapped["App"] = relationship(back_populates="deployments")
    build_logs: Mapped[list["BuildLog"]] = relationship(
        back_populates="deployment", cascade="all, delete-orphan"
    )


class AppService(Base):
    """应用内的单个服务（多容器 compose 的最小运行时单元）。

    单容器应用也会有一条 name="default" 的服务记录：
    - 对外公开的服务 is_public=True，host_port 指向平台分配的宿主端口；
    - 内部服务（如数据库）is_public=False，不分配宿主端口，也不渲染路由。
    """

    __tablename__ = "app_services"
    __table_args__ = (
        UniqueConstraint("app_id", "name", name="uq_app_services_app_name"),
        Index("ix_app_services_app_public", "app_id", "is_public"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[int] = mapped_column(
        ForeignKey("apps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    container_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    host_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mem_limit: Mapped[str] = mapped_column(String(16), default="256m")
    cpu_quota: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(16), default="created")
    is_public: Mapped[bool] = mapped_column(default=False)
    # 蓝绿发布：当前 slot 与上一个 slot 的容器 / 端口，用于失败回退与一键回滚
    previous_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_container_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    previous_host_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_container_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    previous_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    app: Mapped["App"] = relationship(back_populates="services")

    @property
    def is_primary(self) -> bool:
        """主入口公开服务：宿主端口与应用对外端口一致。"""
        app = self.app
        return bool(
            self.is_public
            and app is not None
            and self.host_port
            and self.host_port == app.host_port
        )

    @property
    def access_url(self) -> str | None:
        """服务访问地址：主入口用应用域名，其他公开服务用独立子域名。"""
        from app.core.config import settings

        app = self.app
        if not self.is_public or not self.host_port or app is None:
            return None
        if not settings.ENABLE_NGINX:
            return f"http://localhost:{self.host_port}"

        from app.services import route_service  # 延迟导入，避免模型层依赖服务层

        domain = (
            route_service.domain_for(app.subdomain)
            if self.is_primary
            else route_service.domain_for_service(app.subdomain, self.name)
        )
        https = bool(
            settings.USE_HTTPS and settings.SSL_CERT_PATH and settings.SSL_KEY_PATH
        )
        scheme = "https" if https else "http"
        default_port = 443 if https else 80
        port = (
            ""
            if settings.NGINX_PUBLIC_PORT == default_port
            else f":{settings.NGINX_PUBLIC_PORT}"
        )
        return f"{scheme}://{domain}{port}"



class Route(Base):
    """路由表：域名到容器端口的映射。"""

    __tablename__ = "routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"))
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    upstream_port: Mapped[int] = mapped_column(Integer, nullable=False)
    # 多公开服务：路由归属的服务名；旧数据为 NULL（应用主入口）
    service_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ssl_enabled: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    app: Mapped["App"] = relationship(back_populates="routes")


class BuildLog(Base):
    """构建日志明细。"""

    __tablename__ = "build_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE")
    )
    log_line: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    deployment: Mapped["Deployment"] = relationship(back_populates="build_logs")


class Metric(Base):
    """资源指标采样。

    `service_id` 指向应用内的具体服务：多容器 compose 逐服务采样，
    单容器应用同样落到 default 服务上；历史数据该列为空（视为应用整体）。
    """

    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"))
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_services.id", ondelete="CASCADE"), nullable=True, index=True
    )
    cpu: Mapped[float] = mapped_column(Float, default=0.0)
    mem: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# 用户与登录会话（登录 / 鉴权）
# ---------------------------------------------------------------------------


class User(Base):
    """平台用户表（登录账号）。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # 口令以 PBKDF2 加盐哈希存储，格式见 auth_service.hash_password
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="admin")  # admin / user
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sessions: Mapped[list["AuthSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    apps: Mapped[list["App"]] = relationship(back_populates="owner")


class AuthSession(Base):
    """登录会话表：服务端保存令牌哈希，支持主动登出与过期失效。"""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # 仅存令牌哈希（sha256），即便库泄露也无法直接冒用会话
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped["User"] = relationship(back_populates="sessions")


# ---------------------------------------------------------------------------
# 告警中心（指标阈值告警）
# ---------------------------------------------------------------------------


class Alert(Base):
    """告警记录：一条规则在一个目标上的当前状态。

    去重不变式：同一 (rule, target_type, target_id) 至多存在一条未消除记录
    （status 为 active 或 acked）；指标回落时由巡检线程置为 resolved。
    """

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_key", "rule", "target_type", "target_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule: Mapped[str] = mapped_column(String(32), nullable=False)  # host_cpu / host_mem / host_disk / app_restart / app_failed
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)  # host / app
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # app_id；host 级为空
    target_name: Mapped[str] = mapped_column(String(128), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="warning")  # warning / critical
    message: Mapped[str] = mapped_column(String(512), default="")
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / acked / resolved
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ---------------------------------------------------------------------------
# 操作审计日志
# ---------------------------------------------------------------------------


class AuditLog(Base):
    """操作审计：记录"谁在何时对哪个对象做了什么"。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 操作人：用户被删除时置空（历史记录保留，靠 username 快照回溯）
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    username: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(16), default="app")
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 附加细节：JSON 文本（如变更前后的配额），无则空
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

