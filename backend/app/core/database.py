"""数据库连接与会话管理。"""

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

# SQLite 需要额外参数以支持多线程访问
connect_args = {"check_same_thread": False} if settings.DB_URL.startswith("sqlite") else {}

engine = create_engine(settings.DB_URL, connect_args=connect_args, echo=False)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入：提供数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表（M1 阶段先以 create_all 起步，后续可引入 Alembic 迁移）。"""
    # 导入模型以便注册到 Base.metadata
    from app.models import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_columns()


# 已有表新增列的兼容补丁：create_all 只建新表，不会给老表加列。
# 仅用 "ADD COLUMN <列名> <类型>"，且新列一律 nullable、不带 DEFAULT——
# SQLite 禁止无默认值的 NOT NULL，MySQL 旧版 TEXT 不允许 DEFAULT，这样写两边都成立。
_COLUMN_PATCHES: tuple[tuple[str, str, str], ...] = (
    ("apps", "env", "ALTER TABLE apps ADD COLUMN env TEXT"),
    ("apps", "command", "ALTER TABLE apps ADD COLUMN command VARCHAR(512)"),
    ("apps", "webhook_secret", "ALTER TABLE apps ADD COLUMN webhook_secret VARCHAR(64)"),
    ("apps", "active_slot", "ALTER TABLE apps ADD COLUMN active_slot VARCHAR(8)"),
    ("apps", "previous_slot", "ALTER TABLE apps ADD COLUMN previous_slot VARCHAR(8)"),
    ("app_services", "previous_container_id", "ALTER TABLE app_services ADD COLUMN previous_container_id VARCHAR(64)"),
    ("app_services", "previous_container_name", "ALTER TABLE app_services ADD COLUMN previous_container_name VARCHAR(128)"),
    ("app_services", "previous_host_port", "ALTER TABLE app_services ADD COLUMN previous_host_port INTEGER"),
    ("app_services", "previous_container_port", "ALTER TABLE app_services ADD COLUMN previous_container_port INTEGER"),
    ("app_services", "previous_image", "ALTER TABLE app_services ADD COLUMN previous_image VARCHAR(255)"),
    ("app_services", "previous_status", "ALTER TABLE app_services ADD COLUMN previous_status VARCHAR(16)"),
    ("deployments", "slot", "ALTER TABLE deployments ADD COLUMN slot VARCHAR(8)"),
    ("deployments", "strategy", "ALTER TABLE deployments ADD COLUMN strategy VARCHAR(16)"),
    ("routes", "service_name", "ALTER TABLE routes ADD COLUMN service_name VARCHAR(64)"),
    ("metrics", "service_id", "ALTER TABLE metrics ADD COLUMN service_id INTEGER"),
)


def _ensure_columns() -> None:
    """为已有表补齐新增列（幂等）：已存在的列直接跳过。"""
    for table, column, ddl in _COLUMN_PATCHES:
        try:
            existing = {c["name"] for c in inspect(engine).get_columns(table)}
        except Exception:
            continue
        if column in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
        except OperationalError:
            # 并发启动（--reload / 多 worker）时另一进程可能已加过该列
            pass
