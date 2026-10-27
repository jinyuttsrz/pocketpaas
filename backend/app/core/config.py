"""全局配置：通过环境变量 / .env 控制本地模式与线上模式的差异。

设计原则：域名与 HTTPS 是可插拔的最后一环，本地开发不依赖它们。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 运行模式：local（本地端口直连）/ prod（线上泛域名 + HTTPS）
    DEPLOY_MODE: str = "local"

    # 应用基本信息
    APP_NAME: str = "PocketPaaS 容器云平台"
    DEBUG: bool = True

    # 监听地址
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # 域名与 HTTPS（本地模式留空 / 关闭）
    BASE_DOMAIN: str = ""
    USE_HTTPS: bool = False

    # 反向代理（本地模式可关闭）
    ENABLE_NGINX: bool = False
    NGINX_CONF_DIR: str = "/etc/nginx/conf.d"
    # M4 动态路由：平台内层 nginx 的监听端口（渲染 server 块时使用）。
    # 独占 80 时填 80；若宿主已有 nginx（如宝塔）占用 80，
    # 则填一个内网端口（如 8080），由外层 nginx 反代进来。
    NGINX_LISTEN_PORT: int = 80
    # 用户实际访问的对外端口（由外层反向代理监听，通常 80）。
    # 与 NGINX_LISTEN_PORT 分离：两层部署时内层 8080、外层 80，
    # 控制台展示的访问地址不应带上内层端口。
    NGINX_PUBLIC_PORT: int = 80
    # M4 HTTPS 证书路径（线上 Let's Encrypt；两项都配置才渲染 443，否则自动降级 HTTP）
    SSL_CERT_PATH: str = ""
    SSL_KEY_PATH: str = ""
    # M5 监控：采样间隔（秒）与每应用保留条数
    METRICS_INTERVAL: int = 5
    METRICS_KEEP: int = 720
    # M5 监控：「资源与节点」宿主机性能视图采样
    # CPU 帧采样间隔（秒）与保留帧数（90 帧 × 2s ≈ 3 分钟滚动窗口）
    HOST_METRICS_INTERVAL: int = 2
    HOST_METRICS_KEEP: int = 90
    # 受管容器占用（CPU / 内存 / 网络）刷新间隔（秒）
    HOST_CONTAINER_INTERVAL: int = 5

    # 数据库：本地 SQLite，线上可切 MySQL
    DB_URL: str = "sqlite:///./paas.db"

    # Docker
    DOCKER_HOST: str = "unix:///var/run/docker.sock"
    # 容器资源默认配额
    DEFAULT_MEM_LIMIT: str = "256m"
    DEFAULT_CPU_QUOTA: float = 1.0

    # M3 镜像构建
    # 构建工作区：存放源码包、解压产物与自动生成的 Dockerfile
    WORKSPACE_DIR: str = "./workspace"
    # 内置示例代码包目录：供「模板与指南」页面一键创建演示应用（相对后端工作目录）
    DEMO_DIR: str = "../demo"
    # 平台构建出的镜像统一前缀，便于识别与清理
    IMAGE_PREFIX: str = "paas"
    # 代码包解压后的体积上限（MB），防止解压炸弹
    MAX_UPLOAD_MB: int = 100
    # 构建并发上限之外的构建超时（秒）
    BUILD_TIMEOUT: int = 900
    # 多容器 compose：等待服务启动 / 健康检查的超时（秒）
    COMPOSE_START_TIMEOUT: int = 60
    COMPOSE_HEALTH_TIMEOUT: int = 180
    # git clone 超时（秒）
    GIT_CLONE_TIMEOUT: int = 300
    # 构建期 pip 源（本地网络受限时用国内镜像；线上留空走官方源）
    PIP_INDEX_URL: str = "https://pypi.tuna.tsinghua.edu.cn/simple"
    # 构建期 npm 源（同理）
    NPM_REGISTRY: str = "https://registry.npmmirror.com"

    # 容器端口映射区间（本地模式端口直连使用）
    PORT_RANGE_START: int = 20000
    PORT_RANGE_END: int = 29999
    # M8：端口竞态时换端口重试次数（预留给容器后仍可能被宿主进程抢占）
    MAX_PORT_RETRY: int = 5

    # ------------------------------------------------------------------
    # M7 安全与稳定性加固
    # ------------------------------------------------------------------
    # 容器运行时加固（no-new-privileges / 丢弃全部 capability / 限制进程数 / 禁用 swap）
    ENABLE_CONTAINER_HARDENING: bool = True
    # 容器内进程数上限，防 fork 炸弹
    CONTAINER_PIDS_LIMIT: int = 256
    # 在 cap_drop=ALL 基础上保留的最小 capability（nginx/gunicorn 降权与绑定端口所需）
    CONTAINER_CAP_ADD: list[str] = [
        "CHOWN", "SETUID", "SETGID", "NET_BIND_SERVICE", "DAC_OVERRIDE", "FOWNER",
    ]
    # 内网隔离网桥：所有受管容器接入专用网络，容器间默认不可互通（ICC 关闭）
    ENABLE_NETWORK_ISOLATION: bool = True
    PAAS_NETWORK_NAME: str = "paas-net"
    # 自带 Dockerfile 危险指令扫描（命中即拒绝构建）
    BLOCK_DANGEROUS_DOCKERFILE: bool = True
    # Git 源 SSRF 防护：拒绝指向本机 / 内网 / 云元数据地址的仓库
    BLOCK_PRIVATE_GIT_HOST: bool = True
    # 异常自愈：后台巡检受管容器，异常退出时按策略自愈
    ENABLE_HEALTH_WATCHDOG: bool = True
    HEALTH_CHECK_INTERVAL: int = 15
    # 单个容器连续自动重启上限（超过则判定为持续崩溃，置为 failed 停止重启）
    HEALTH_MAX_RESTARTS: int = 3
    # HTTP 就绪探针：容器处于 running 时进一步探测应用是否真正可服务
    ENABLE_HEALTH_HTTP: bool = True
    # 探活请求路径（拼在 127.0.0.1:宿主端口 之后）
    HEALTH_HTTP_PATH: str = "/"
    # 单次探活超时（秒）
    HEALTH_HTTP_TIMEOUT: float = 2.0
    # 连续探活失败达到该次数即自动重启容器（达到 HEALTH_MAX_RESTARTS 次仍不就绪则置 failed）
    HEALTH_HTTP_MAX_FAILS: int = 3

    # ------------------------------------------------------------------
    # 告警中心：按阈值巡检宿主与应用，超限时留档并可手动确认
    # ------------------------------------------------------------------
    ENABLE_ALERTS: bool = True
    # 巡检间隔（秒）
    ALERT_CHECK_INTERVAL: int = 30
    # 宿主 CPU / 内存 / 磁盘使用率阈值（百分比）
    ALERT_CPU_PERCENT: float = 85.0
    ALERT_MEM_PERCENT: float = 85.0
    ALERT_DISK_PERCENT: float = 90.0
    # 单个容器连续自动重启达到该次数即告警
    ALERT_RESTART_COUNT: int = 3

    # ------------------------------------------------------------------
    # 项目文件管理（源码目录 / 容器文件系统）
    # ------------------------------------------------------------------
    # 文本文件在线预览与编辑的体积上限（KB），超出只允许下载
    FILE_MAX_READ_KB: int = 512
    # 单个上传文件体积上限（MB）
    FILE_MAX_UPLOAD_MB: int = 50
    # 整目录打包下载的体积上限（MB）
    FILE_MAX_ARCHIVE_MB: int = 200

    # ------------------------------------------------------------------
    # 登录与鉴权
    # ------------------------------------------------------------------
    # 是否强制登录：开启后应用接口与实时日志均需携带有效会话令牌
    ENABLE_AUTH: bool = True
    # 会话有效期（小时）
    AUTH_SESSION_HOURS: int = 72
    # 新口令最小长度
    AUTH_PASSWORD_MIN_LENGTH: int = 8
    # 是否允许自助注册（关闭后仅管理员可创建账号）
    AUTH_ALLOW_REGISTER: bool = True
    # 图形验证码：登录 / 注册页是否要求填写（关闭后接口不再校验）
    AUTH_ENABLE_CAPTCHA: bool = True
    # 验证码有效期（秒）
    AUTH_CAPTCHA_TTL: int = 120
    # 首次启动自动创建默认管理员（库中无任何用户时生效）
    AUTH_BOOTSTRAP_ADMIN: bool = True
    AUTH_DEFAULT_USERNAME: str = "admin"
    AUTH_DEFAULT_PASSWORD: str = "admin12345"

    # ------------------------------------------------------------------
    # AI 只读分析助手（OpenAI 兼容接口）
    # ------------------------------------------------------------------
    # 总开关：未配置完整参数时保持关闭，不影响平台其它功能
    AI_ENABLED: bool = False
    # 供应商 Base URL，例如 https://api.deepseek.com/v1
    AI_BASE_URL: str = ""
    # 仅从环境变量注入，禁止写入代码或前端
    AI_API_KEY: str = ""
    AI_MODEL: str = ""
    # 系统提示词文件：留空使用 app/prompts/ai_system.md；相对路径以 backend 根目录为基准
    AI_PROMPT_FILE: str = ""
    # 单次请求超时（秒）与最大输出 token
    AI_TIMEOUT: float = 60.0
    AI_MAX_TOKENS: int = 1200
    AI_TEMPERATURE: float = 0.2
    # 多轮对话最多携带的历史消息条数
    AI_HISTORY_LIMIT: int = 8
    # 每用户每分钟最多调用次数（<=0 表示不限制）
    AI_RATE_LIMIT_PER_MIN: int = 10
    # 上下文最多携带的应用数量，避免 token 膨胀
    AI_CONTEXT_APPS: int = 30

    @property
    def is_local(self) -> bool:
        return self.DEPLOY_MODE.lower() == "local"

    @property
    def is_prod(self) -> bool:
        return self.DEPLOY_MODE.lower() == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
