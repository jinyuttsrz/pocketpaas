# PocketPaaS

> 轻量级容器云平台与应用自动化交付系统

**Python 3.12 ｜ FastAPI ｜ Docker ｜ Nginx ｜ SQLite ｜ WebSocket**

PocketPaaS 在**单机 Docker 引擎**之上构建了一套「控制面—数据面」分离的类 PaaS 平台：用户只需提供源码（上传 zip、Git 仓库或内置示例包），平台即自动识别项目类型、生成 Dockerfile、构建镜像、启动容器，并为其动态渲染 Nginx 反向代理路由。它面向中小团队、教学环境与个人开发者，作为 Kubernetes「能力过剩」场景下的轻量替代方案。

整个交付闭环为：**创建应用 → 引入源码 → 生成 Dockerfile → 构建镜像 → 起容器 → 动态反向代理 → 实时日志 → 资源监控与告警**。

## 为什么做这个（项目背景与定位）

- **Kubernetes 的复杂度与本项目规模不匹配**：一套可用的 K8s 集群需要多节点、etcd、CNI/CSI、Ingress Controller、CRD/Operator 等一整套组件，学习与运维成本远超中小团队与教学场景的实际需要。
- **资源开销敏感**：K8s 控制面自身就要吃掉可观的 CPU 与内存，而本项目控制面是**单个 Python 进程**，可在单台机器上与业务容器共存。
- **单机即可跑通全链路**：不依赖集群编排，直接复用宿主 Docker 引擎（通过 `docker.sock`）管理应用容器，**开箱即用**，无需先搭建基础设施。
- **前端零构建**：控制台为原生 JavaScript + 手写 SVG 图表 + 内联 SVG 图标的零依赖 SPA，由 FastAPI `StaticFiles` 直接托管，无 npm、无打包工具、无 React/Vue、无任何 CDN 引用。
- **取舍明确**：牺牲多节点调度与高可用，换取低开销、易部署、易理解；适合作为单机部署平台、教学演示与个人项目托管。

## 功能特性

### 应用交付

- **三种源码来源**：上传 zip 压缩包、提供 Git 仓库地址、或直接选用内置示例包。
- **项目类型识别与 Dockerfile 自动生成**：按源码特征识别 `python` / `static` / `node` 项目并生成对应 Dockerfile。
- **镜像构建与构建日志**：一键构建镜像，构建过程分行落库，可随时查看最近一次构建日志。
- **容器生命周期**：部署（起容器）、停止、重启、销毁。
- **实时日志**：通过 WebSocket 推送容器日志流，支持 `?tail=` 回放历史日志。

### 运行时配置

- **环境变量与启动命令可视化编辑**：在控制台直接编辑 `env` 与启动命令；注意改动**只写库，需重新部署后才生效**。
- **CPU / 内存配额调整**：可视化调整容器 CPU 配额与内存上限，调整前会**对照宿主真实资源做预校验**。

### 路由与访问

- **Nginx 动态反向代理**：由 `route_service` 渲染 server 块 → `nginx -t` 语法校验 → `nginx -s reload` 热载，无需人工改配置。
- **应用子域名**：为应用分配全局唯一子域名，配合 `BASE_DOMAIN` 对外提供域名路由。
- **双模式访问**：本地可用 `127.0.0.1:{host_port}` 端口直连，线上可走域名路由。

### 安全与隔离

- **容器加固**：`no-new-privileges`、`cap_drop=ALL`（再按需 `cap_add`）、`pids_limit`、内存硬限制。
- **网络隔离**：为应用容器建立专用隔离网桥，**关闭容器间互通**。
- **Dockerfile 危险指令扫描**：构建前拦截高危指令。
- **Git 私有地址（SSRF）拦截**：阻止以 Git 仓库方式访问内网/私网地址。
- **账户安全**：口令 **PBKDF2 加盐哈希**、会话令牌（HttpOnly Cookie）、**零依赖自绘 SVG 图形验证码**。

### 多租户与权限

- **应用按属主隔离**：普通用户只能看到并操作自己的应用。
- **管理员跨租户管理**：管理员可查看与管理全部应用。
- **最后管理员保护**：避免误删导致无人可管理。
- **用户管理**：用户 CRUD、角色设置、启用/禁用、**修改显示名**、重置口令。

### 可观测与运维

- **指标采样与负载曲线**：按间隔采样容器 CPU / 内存并留存，前端绘制负载曲线。
- **宿主机性能监控**：采集宿主 CPU、内存、磁盘等实时指标。
- **HTTP 就绪探针（四态）**：`ready` / `degraded` / `unready` / `unknown`，探测 `127.0.0.1:{host_port}{HEALTH_HTTP_PATH}`；HTTP 状态码 < 500 记为 ready，≥ 500 记为 degraded（**只展示不重启**，避免重启风暴），连接被拒/超时记为 unready；连续 unready 达阈值才触发重启，仍不就绪则置 failed。
- **自愈重启策略**：由巡检线程按策略重启异常容器，恢复后清零计数。
- **指标阈值告警中心**：五类规则（`host_cpu` / `host_mem` / `host_disk` / `app_restart` / `app_failed`）；状态流转为 active（待处理）→ 手动确认后 acked（**不因指标持续超限重复打扰**，仅刷新观测值）→ 指标回落由巡检**自动置 resolved**。
- **操作审计日志**：覆盖 **21 个动作**（登录/注册/登出、应用创建/构建/部署/停止/重启/配额/配置/源码/文件/诊断/健康探针/Webhook 及其重置/销毁、用户创建/更新/重置密码/删除）。
- **一键诊断**：**8 项检查**（应用状态 / 源码目录 / 镜像 / 容器 / 宿主端口 / 反向代理路由 / 最近部署 / 资源配额），单项状态为 `ok` / `warn` / `fail` / `skip`，全部只读。

### 文件与终端

- **项目文件管理（双通道）**：`target=source` 操作宿主源码目录 `workspace/{app_id}/src`，`target=container` 操作运行中容器文件系统。支持浏览、读取、在线编辑保存、上传（多选）、下载（文件或目录打包 zip）、新建目录、重命名、删除；路径经 `safe_join` 校验，拒绝绝对路径与 `..`。
- **容器 Web 终端**：基于 WebSocket + `docker exec` 的交互式 TTY；在应用详情抽屉与项目文件管理页**两个入口**均可打开，且**全局单会话互斥**（同一时间只保留一个容器会话）。

### 自动化交付

- **Git Webhook 自动部署**：接收端点匿名可访问但**必须通过签名校验**（HMAC-SHA256，兼容 `X-Hub-Signature-256` / `X-Paas-Signature`；亦支持 GitLab `X-Gitlab-Token` 明文比对）；**进程内并发锁**防止重复触发（重入返回 409）；流水线为「拉源码 → 重建镜像 → 重新部署」，**构建期间旧容器继续对外服务**。

## 技术栈

| 层次 | 选型 | 说明 / 版本 |
|---|---|---|
| 后端 | Python | **3.12**（`pyproject` 要求 `>=3.12,<3.13`，镜像 `python:3.12-slim`） |
| 后端框架 | FastAPI `0.115.6` + Uvicorn `0.34.0` | ASGI 服务与 Web 框架 |
| 数据校验 | Pydantic `2.10.4` / pydantic-settings `2.7.0` | 请求响应模型与配置加载 |
| 持久化 | SQLAlchemy `2.0.36` + SQLite | `DB_URL` 可切 MySQL |
| Docker 交互 | docker-py `7.1.0` | 容器生命周期 / 镜像构建 / 日志流 / exec 终端 |
| 其他依赖 | python-multipart `0.0.20`、websockets `14.1`、psutil `6.1.1` | 表单上传 / WS / 系统指标 |
| 前端 | 原生 JavaScript + 手写 SVG 图表 | **零构建、零外部依赖**，FastAPI `StaticFiles` 托管，无 npm / 打包器 / CDN |
| 反向代理 | Nginx | `route_service` 渲染 server 块 + `nginx -t` 校验 + `nginx -s reload` 热载 |
| 实时通信 | WebSocket | 容器日志流、容器终端 |

## 系统架构

```text
        浏览器            Git 平台（Webhook 推送）
          │                        │
          └───────────┬────────────┘
                      ▼
        ┌──────────────────────────────────┐
        │   外层 Nginx（宝塔，listen :80）   │
        │   deploy/baota-front.conf         │
        │   server_name *.beta.… / app.…    │
        └────────────────┬─────────────────┘
                         │ 反向代理（透传 Host）
                         ▼
        ┌──────────────────────────────────┐
        │   平台内层 Nginx（listen :8080）   │
        │   deploy/console.conf（控制台路由）│
        │   + paas-{id}.conf（应用子域名）   │
        └───────┬──────────────────┬───────┘
                │                  │
     控制台 / API :8000             │  应用子域名 → 127.0.0.1:2000x
                ▼                  ▼
   ┌──────────────────────┐   ┌────────────────────────┐
   │  PocketPaaS 控制面    │   │  应用容器（数据面）      │
   │  FastAPI + 前端 SPA   │   │  用户镜像 / 端口 2000x   │
   └──────────┬───────────┘   └────────────────────────┘
              │  docker.sock（创建 / 构建 / 管理）
              ▼
   ┌──────────────────────┐
   │   宿主 Docker Engine  │
   └──────────────────────┘
```

**控制面（control plane）与数据面（data plane）的职责划分：**

- **控制面**是 PocketPaaS 本体（单个 FastAPI 进程）：负责应用编排、源码接收、镜像构建、容器生命周期管理、路由渲染、指标采集、告警与审计。它**不承载用户业务流量**，只通过 `docker.sock` 指挥宿主 Docker。
- **数据面**是用户应用容器：真正对外提供服务，端口从 `PORT_RANGE_START`~`PORT_RANGE_END`（默认 20000~29999）区间内分配并发布到宿主机。
- **流量路径**：外部请求先由外层 Nginx（宝塔，占 80）转发到平台内层 Nginx（8080），再由内层 Nginx 按 Host 分流——面板域名指向控制面 8000，应用子域名指向对应的 `127.0.0.1:2000x`。
- 应用容器与平台控制面之间通过**专用隔离网桥**分隔，容器间互通被关闭。

## 目录结构

```text
f:\test/                        仓库根目录
├── backend/                    后端服务（含前端静态资源）
│   ├── app/
│   │   ├── api/                路由层：console, system, auth, users, apps,
│   │   │                       files, diagnose, webhook, audit, alerts, ws
│   │   ├── services/           服务层（21 个），部分代表：
│   │   │                       app_service / builder_service / docker_service
│   │   │                       source_service / route_service / metrics_service
│   │   │                       watchdog_service / alert_service / audit_service
│   │   │                       auth_service / captcha_service / security_service
│   │   │                       file_service / diagnose_service / webhook_service
│   │   │                       demo_service / port_allocator / resource_service
│   │   ├── models/             models.py：9 张数据表
│   │   ├── schemas/            Pydantic 请求 / 响应模型
│   │   ├── core/               config.py / database.py / deps.py
│   │   ├── web/                零构建前端：index.html / app.js / styles.css
│   │   │                       login.html / login.js / login.css / favicon.svg
│   │   └── main.py             FastAPI 实例、lifespan、router 注册、静态托管
│   ├── Dockerfile              平台镜像：python:3.12-slim + nginx/git/procps/curl
│   ├── entrypoint.sh           先拉起内层 nginx，再 exec 平台本体
│   ├── requirements.txt        依赖清单（见「技术栈」）
│   ├── pyproject.toml          项目元信息 + ruff 约定
│   ├── .env / .env.example     本地开发配置
│   └── .env.prod.example       线上配置模板（复制为 .env.prod）
├── demo/                       4 个内置示例代码包
├── deploy/                     双层 Nginx 配置：console.conf / baota-front.conf
├── docker-compose.yml          线上编排（单服务、host 网络）
├── README.md                   本文件
├── 本地运行说明.md               本机（WSL2 + Docker Desktop）运行手册
├── 云端运行说明.md               线上容器化部署手册
└── 毕业论文初稿.md               毕业设计论文初稿
```

## 快速开始

### A. 本地开发（WSL2 + Docker Desktop，推荐）

项目**不能直接在 Windows 原生运行**：Python 版本要求 `>=3.12,<3.13`，且 `DOCKER_HOST=unix:///var/run/docker.sock` 只在 Linux 侧有效。请在 WSL 的 Ubuntu 中先启动 Docker Desktop，再进入后端目录启动：

```bash
# 进入 WSL 后切到后端目录（程序按当前工作目录解析 .env、SQLite 与工作区）
cd /mnt/f/test/backend

# 启动（前台运行，Ctrl+C 停止）
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> **为什么用 `python -m uvicorn` 而不是 `.venv/bin/uvicorn`？**
> `backend/.venv` 的元信息（`pyvenv.cfg`）记录的是**旧路径**，项目搬到 `F:\test` 后，`./.venv/bin/python` 因为是绝对路径软链接仍可用，但 `./.venv/bin/uvicorn`、`./.venv/bin/pip` 等**脚本头（shebang）里写死了旧路径，可能失效**。以模块方式调用 `python -m xxx` 可绕开这个问题。

启动成功后访问：

| 用途 | 地址 |
|---|---|
| 控制台首页 | `http://localhost:8000/` |
| 登录页 | `http://localhost:8000/login` |
| 接口文档（Swagger UI） | `http://localhost:8000/docs` |

默认账号 **`admin` / `admin12345`**。注意：

- 该账号**仅在数据库中一条用户都没有时自动创建**（`AUTH_BOOTSTRAP_ADMIN=true`）；若 `backend/paas.db` 已存在且有用户，则使用之前设置的口令。
- 登录页**需要填写图形验证码**（`AUTH_ENABLE_CAPTCHA=true`）。

详细步骤（含前置检查、Docker Desktop 启停、`.venv` 说明）见 **「本地运行说明.md」**。

### B. Docker Compose（更接近线上）

要求服务器**已安装 Docker**，且宿主的 80 / 8080 端口规划需按实际环境调整。

```bash
# 1. 复制线上配置模板
cp backend/.env.prod.example backend/.env.prod

# 2. 按需编辑 .env.prod（至少改掉默认管理员口令）

# 3. 构建并启动
docker compose up -d --build
```

三点提示：

- 平台以 `network_mode: host` 运行，直接监听宿主 `8000`（host 网络下 `ports` 映射无效）。
- 宿主 **80 端口通常被宝塔 Nginx 占用**，本项目采用「外层宝塔 + 平台内层 Nginx」双层的处理方式，端口规划请参考手册。
- **`docker-compose.yml` 必须保留 `./demo:/demo:ro` 挂载**，否则控制台「模板与指南」页的示例包下拉会显示「（未找到示例包）」。

详细步骤见 **「云端运行说明.md」**。

## 配置说明

关键配置项（完整清单见 `backend/app/core/config.py` 与 `backend/.env.example`）：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `DEPLOY_MODE` | `local` | 运行模式：`local` \| `prod` |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 控制面监听地址与端口 |
| `DB_URL` | `sqlite:///./paas.db` | 数据库连接串（可切 MySQL）；线上为 `sqlite:////data/paas.db` |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker 守护进程地址 |
| `WORKSPACE_DIR` | `./workspace` | 构建工作区；线上为 `/data/workspace`，应用源码位于 `{WORKSPACE_DIR}/{app_id}/src` |
| `DEMO_DIR` | `../demo` | 内置示例包目录（见下方专项提示） |
| `PORT_RANGE_START` / `PORT_RANGE_END` | `20000` / `29999` | 应用容器端口分配区间 |
| `ENABLE_NGINX` | `False` | 是否启用平台内层 Nginx 动态路由 |
| `BASE_DOMAIN` | 空 | 应用子域名所依附的基础域名 |
| `ENABLE_AUTH` | `True` | 是否启用登录鉴权 |
| `AUTH_DEFAULT_PASSWORD` | `admin12345` | 首次引导管理员的默认口令（`.env.prod.example` 中为「请改成强密码」，**上线务必修改**） |
| `ENABLE_ALERTS` | `True` | 是否启用指标阈值告警中心（阈值如 `ALERT_CPU_PERCENT=85.0`） |
| `ENABLE_HEALTH_WATCHDOG` | `True` | 是否启用自愈巡检（配合 `ENABLE_HEALTH_HTTP=True` 的 HTTP 就绪探针） |

> **关于 `DEMO_DIR`（重要）**：本地默认 `../demo`，即仓库根目录的 `demo/`；平台镜像内 `WORKDIR` 为 `/app`，因此 `../demo` 在容器内解析为 **`/demo`**。镜像是**只 `COPY app`、不含 demo 的**，所以 `docker-compose.yml` **必须保留 `./demo:/demo:ro` 挂载**，否则「模板与指南」页的示例包下拉会显示「（未找到示例包）」。

其他常用开关：`ENABLE_CONTAINER_HARDENING`、`ENABLE_NETWORK_ISOLATION`（默认 `True`）、`BLOCK_DANGEROUS_DOCKERFILE`、`BLOCK_PRIVATE_GIT_HOST`、`AUTH_ALLOW_REGISTER`、`AUTH_ENABLE_CAPTCHA`。

## API 概览

完整端点以运行时 Swagger UI 为准：**`http://<host>:8000/docs`**。以下按模块列代表性端点。

**系统与探针**（`/api`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 存活探针 |
| GET | `/api/ready` | 就绪探针（含 Docker 连通性） |
| GET | `/api/system/host-metrics` | 宿主机实时性能 |
| POST | `/api/system/resource-check` | 端口 / 内存 / CPU 资源校验 |
| GET | `/api/system/demos` | 内置示例包列表 |

**认证**（`/api/auth`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/auth/captcha` | 图形验证码（SVG，匿名） |
| POST | `/api/auth/login` | 登录（写 HttpOnly Cookie） |
| POST | `/api/auth/register` | 注册并自动登录 |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/me` | 当前登录用户 |

**用户管理**（`/api/users`，全部 `require_admin`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/users` | 用户列表 |
| POST | `/api/users` | 新建用户 |
| PATCH | `/api/users/{user_id}` | 改显示名 / 角色 / 启用状态 |
| PUT | `/api/users/{user_id}/password` | 重置口令 |
| DELETE | `/api/users/{user_id}` | 删除用户 |

**应用与交付**（`/api/apps`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/api/apps` | 应用列表 / 创建应用 |
| POST | `/api/apps/{app_id}/source` | 上传 zip 源码包 |
| POST | `/api/apps/{app_id}/source/git` | 以 Git 仓库作为源码 |
| POST | `/api/apps/{app_id}/build` | 构建镜像 |
| POST | `/api/apps/{app_id}/deploy` | 部署（起容器） |
| POST | `/api/apps/{app_id}/stop` \| `/restart` | 停止 / 重启容器 |
| PATCH | `/api/apps/{app_id}/quota` \| `/config` | 调整配额 / 更新环境变量与启动命令 |
| GET | `/api/apps/{app_id}/runtime` | 容器实时状态与资源占用 |
| GET | `/api/apps/{app_id}/readiness` | HTTP 就绪探测结论（只读） |
| GET | `/api/apps/{app_id}/metrics` | 应用资源曲线 |

**项目文件**（`/api/apps/{app_id}/files`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/apps/{app_id}/files` | 浏览目录（`target=source\|container`） |
| GET / PUT | `/api/apps/{app_id}/files/content` | 读取 / 保存文本文件 |
| POST | `/api/apps/{app_id}/files/upload` | 上传文件（支持多选） |
| POST | `/api/apps/{app_id}/files` | 新建 / 重命名 / 删除（`op=mkdir\|rename\|delete`） |
| GET | `/api/apps/{app_id}/files/download` | 下载文件或目录打包 zip |

**诊断**（`/api/apps`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/apps/{app_id}/diagnose` | 一键诊断（8 项检查，只读） |

**Webhook**（`/api/apps`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/apps/{app_id}/webhook` | 接收 Git 推送（匿名，HMAC 校验） |
| GET | `/api/apps/{app_id}/webhook` | 查看 Webhook 配置 |
| POST | `/api/apps/{app_id}/webhook/reset` | 重置签名密钥 |
| POST | `/api/apps/{app_id}/webhook/trigger` | 手动触发自动部署 |

**审计**（`/api/audit`）与**告警**（`/api/alerts`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/audit/logs` | 审计日志（多租户过滤，`limit≤500`） |
| GET | `/api/audit/actions` | 动作字典（21 个动作） |
| GET | `/api/alerts` | 告警列表（`status` / `level` 过滤） |
| POST | `/api/alerts/{alert_id}/ack` | 确认告警 |

**WebSocket**

| 协议 | 路径 | 说明 |
|---|---|---|
| WS | `/ws/apps/{app_id}/logs` | 容器日志实时流（`?tail=` 回放；Cookie 或 `?token=` 鉴权） |
| WS | `/ws/apps/{app_id}/shell` | 容器 Web 终端（交互式 TTY） |

## 示例应用包

`demo/` 下提供 4 个示例包，服务于控制台「模板与指南」页的一键创建演示应用：

| 文件 | 大小 | 内容 | 用途 |
|---|---|---|---|
| `static-demo.zip` | 705 B | `static-demo/index.html` | 体验静态站点（`static` 类型）交付 |
| `python-demo.zip` | 2572 B | `python-demo/main.py` + `requirements.txt` | 体验 Python 应用（`python` 类型）交付 |
| `node-demo.zip` | 2890 B | `package.json` + `server.js` | 体验 Node 应用（`node` 类型）交付 |
| `custom-demo.zip` | 2469 B | `Dockerfile` + `index.html` | 体验「自带 Dockerfile」的自定义构建路径 |

## 部署形态

- **容器化部署**：平台自身以单容器运行（`docker-compose.yml`），镜像基于 `python:3.12-slim`，内置 nginx / git / procps / curl；启动时由 `entrypoint.sh` 先按 `ENABLE_NGINX` 拉起内层 Nginx，再 `exec` 平台本体。
- **双层 Nginx 方案**：宿主的 80 端口通常被宝塔 Nginx 占用，因此由**外层宝塔**（`deploy/baota-front.conf`，`listen 80`）先把请求转发到**平台内层 Nginx**（`deploy/console.conf`，`listen 8080`），内层再按 Host 分流到控制面与应用子域名。
- **`network_mode: host` 的必要性**：平台把应用容器端口发布到宿主机，动态路由的目标是 `127.0.0.1:{应用端口}`；**只有平台容器使用 host 网络，容器内的 `127.0.0.1` 才等于宿主机**，内层 Nginx 才能反代到宿主端口上的应用容器。
- **不要对公网开放 `8000` / `8080` / `20000-29999`**：这些端口应仅在本机/内网可达，对外统一走 80 端口的双层 Nginx 入口。

详细部署步骤见 **「云端运行说明.md」**。

## 常见问题（FAQ）

**1. 控制台「模板与指南」页示例包下拉为空，显示「（未找到示例包）」？**
检查 `docker-compose.yml` 是否保留了 `./demo:/demo:ro` 挂载。镜像只 `COPY app`、不含 demo，`DEMO_DIR` 在容器内解析为 `/demo`，不挂载则该目录不存在。

**2. 前端改了看不到效果？**
前端静态资源**没有缓存指纹**，浏览器可能仍用旧缓存。请在浏览器**硬刷新**（`Ctrl+F5` / `Ctrl+Shift+R`）。

**3. 改了环境变量为何没生效？**
环境变量与启动命令的可视化编辑（`PATCH /api/apps/{app_id}/config`）**只写入数据库**，必须**重新部署**应用后才会注入到新容器。

**4. 容器 Web 终端连不上？**
终端依赖 WebSocket 与 `docker exec`，请确认：目标应用**容器正在运行**；链路中的 Nginx 已配置 WebSocket 升级头；同一时间只允许一个容器会话（全局单会话互斥），先关闭其他终端抽屉再重试。

**5. 默认口令 `admin / admin12345` 登不进？**
默认管理员**仅在数据库中不存在任何用户时**才会自动创建。若 `paas.db` 已存在且有用户，请使用当时设置的口令；忘记口令可由管理员在「用户管理」页重置。登录页还需填写图形验证码。

**6. 本地启动报 Docker 连接失败？**
先启动 Docker Desktop 就绪后重试；`DOCKER_HOST` 为 Unix socket 路径，**仅 Linux/WSL 侧有效**，Windows 原生需使用命名管道。

## 开发说明

- **前端零构建**：直接修改 `backend/app/web/app.js`、`styles.css` 等文件后，浏览器硬刷新即可生效，**没有任何构建步骤**（无 npm / 打包器 / CDN）。
- **后端热重载**：本地开发可在启动命令追加 `--reload` 实现代码改动自动重启；注意线上镜像为**单进程**运行（验证码为内存态、指标与巡检为进程内线程，多 worker 会导致状态不一致）。
- **代码规模**（含空行，已排除 `.venv` 与 `__pycache__`）：
  - 后端 `app/**/*.py`：**48 个文件 / 约 7759 行**
  - 前端 `index.html` 999 行、`app.js` 2558 行、`styles.css` 1629 行
- **代码风格约定**：`backend/pyproject.toml` 中 ruff 配置 `line-length = 100`、`target-version = "py312"`。

## 许可证

本项目采用 **GNU General Public License v3.0（GPL-3.0）**，全文见 [LICENSE](LICENSE)。

> **GPL-3.0 是强著佐权（copyleft）许可**：你可以自由使用、修改、分发，但**分发衍生作品时必须以同样的 GPL-3.0 许可开源并提供完整源码**，同时保留版权与许可声明。它**不含「网络服务即触发开源」条款**（那是 AGPL-3.0 的范畴），因此把本项目作为线上服务运行，并不会被要求公开源码。作者不承担担保责任。
>
> 本项目依赖（FastAPI、SQLAlchemy、docker-py、uvicorn、psutil 等）均为宽松许可，可被 GPL-3.0 作品包含，不存在许可冲突。

## 致谢 / 说明

本项目为个人毕业设计作品，用于演示「在单机 Docker 之上构建轻量类 PaaS 平台」的完整交付链路；相关运行手册见 `本地运行说明.md` 与 `云端运行说明.md`。
