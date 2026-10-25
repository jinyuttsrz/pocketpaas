---
title: 配置项
description: PocketPaaS 配置读取顺序、核心项、构建、监控、加固、告警与文件管理上限的完整清单。
---

## 读取顺序

配置读取优先级为 **环境变量 → `backend/.env` → 代码默认值**。
`config.py` 通过 `SettingsConfigDict(env_file=".env")` 读取**当前工作目录**下的 `.env`，
所以必须在 `backend` 目录启动服务。

容器化部署时由 `docker-compose.yml` 的 `env_file: ./backend/.env.prod` 注入。

## 运行模式与基础

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `DEPLOY_MODE` | `local` | `local` \| `prod` |
| `APP_NAME` | `PocketPaaS 容器云平台` | 平台名 |
| `DEBUG` | — | 调试开关 |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 控制面监听地址与端口 |
| `DB_URL` | `sqlite:///./paas.db` | 数据库连接串（可切 MySQL）；线上为 `sqlite:////data/paas.db` |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker 守护进程地址 |
| `WORKSPACE_DIR` | `./workspace` | 构建工作区；应用源码位于 `{WORKSPACE_DIR}/{app_id}/src` |
| `DEMO_DIR` | `../demo` | 内置示例包目录（容器内为 `/demo`） |
| `PORT_RANGE_START` / `PORT_RANGE_END` | `20000` / `29999` | 应用容器端口分配区间 |
| `MAX_PORT_RETRY` | `5` | 端口分配冲突重试次数 |

## 反向代理与域名

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ENABLE_NGINX` | `False` | 是否启用内层 nginx 动态路由 |
| `BASE_DOMAIN` | 空 | 应用子域名所依附的基础域名（泛域名去掉 `*`） |
| `USE_HTTPS` | `False` | 是否启用 HTTPS（当前阶段保持关闭） |
| `NGINX_LISTEN_PORT` | `80` | 内层 nginx 监听端口；宝塔占 80 时填 `8080` |
| `NGINX_PUBLIC_PORT` | `80` | 用户实际访问的端口 |
| `NGINX_CONF_DIR` | `/etc/nginx/conf.d` | 动态路由 server 块写入目录 |
| `SSL_CERT_PATH` / `SSL_KEY_PATH` | — | HTTPS 证书路径（启用 HTTPS 时才需要） |

:::caution[改 NGINX_LISTEN_PORT 要同步 console.conf]
`deploy/console.conf` 里的 `listen` 必须与 `NGINX_LISTEN_PORT` 保持一致。
:::

## 镜像构建

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `IMAGE_PREFIX` | `paas` | 平台镜像前缀（也是容器名前缀） |
| `MAX_UPLOAD_MB` | `100` | 源码包解压体积上限 |
| `BUILD_TIMEOUT` | `900` | 构建超时（秒） |
| `GIT_CLONE_TIMEOUT` | `300` | `git clone` 超时（秒） |
| `DEFAULT_MEM_LIMIT` | `256m` | 新建应用默认内存上限 |
| `DEFAULT_CPU_QUOTA` | `1.0` | 新建应用默认 CPU 配额 |

## 指标采样

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `METRICS_INTERVAL` | `5` | 应用资源采样间隔（秒） |
| `METRICS_KEEP` | `720` | 每应用保留条数 |
| `HOST_METRICS_INTERVAL` | `2` | 宿主机采样间隔（秒） |
| `HOST_METRICS_KEEP` | `90` | 宿主指标保留帧数（约 3 分钟窗口） |
| `HOST_CONTAINER_INTERVAL` | `5` | 受管容器占用刷新间隔（秒） |

## 健康探针与自愈

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ENABLE_HEALTH_WATCHDOG` | `true` | 自愈巡检总开关 |
| `HEALTH_CHECK_INTERVAL` | `15` | 巡检间隔（秒） |
| `ENABLE_HEALTH_HTTP` | `true` | HTTP 就绪探针开关 |
| `HEALTH_HTTP_PATH` | `/` | 探活路径 |
| `HEALTH_HTTP_TIMEOUT` | `2.0` | 单次探活超时（秒） |
| `HEALTH_HTTP_MAX_FAILS` | `3` | 连续失败触发重启次数 |
| `HEALTH_MAX_RESTARTS` | `3` | 连续自动重启上限，超过置 `failed` |

## 告警阈值

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ENABLE_ALERTS` | `true` | 告警巡检总开关 |
| `ALERT_CHECK_INTERVAL` | `30` | 巡检间隔（秒） |
| `ALERT_CPU_PERCENT` | `85.0` | 宿主 CPU 告警阈值（%） |
| `ALERT_MEM_PERCENT` | `85.0` | 宿主内存告警阈值（%） |
| `ALERT_DISK_PERCENT` | `90.0` | 宿主磁盘告警阈值（%） |
| `ALERT_RESTART_COUNT` | `3` | 单容器连续自动重启告警阈值 |

## 容器加固与安全

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ENABLE_CONTAINER_HARDENING` | `true` | 容器运行时加固 |
| `CONTAINER_PIDS_LIMIT` | `256` | 单容器进程数上限 |
| `ENABLE_NETWORK_ISOLATION` | `true` | 隔离网桥开关 |
| `PAAS_NETWORK_NAME` | `paas-net` | 隔离网桥名 |
| `BLOCK_DANGEROUS_DOCKERFILE` | `true` | 危险 Dockerfile 指令拦截 |
| `BLOCK_PRIVATE_GIT_HOST` | `true` | Git 私网地址 SSRF 防护 |

## 登录与鉴权

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ENABLE_AUTH` | `true` | 登录鉴权总开关 |
| `AUTH_ALLOW_REGISTER` | `true` | 是否允许自助注册（对外可关） |
| `AUTH_ENABLE_CAPTCHA` | `true` | 登录 / 注册图形验证码 |
| `AUTH_CAPTCHA_TTL` | `120` | 验证码有效期（秒） |
| `AUTH_PASSWORD_MIN_LENGTH` | `8` | 口令最小长度 |
| `AUTH_SESSION_HOURS` | `72` | 会话有效期（小时） |
| `AUTH_BOOTSTRAP_ADMIN` | `true` | 首次启动自动创建管理员 |
| `AUTH_DEFAULT_USERNAME` | `admin` | 引导管理员用户名 |
| `AUTH_DEFAULT_PASSWORD` | `admin12345` | 引导管理员口令（**上线必须改**） |

## 项目文件管理上限

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `FILE_MAX_READ_KB` | `512` | 在线预览 / 编辑的文本体积上限 |
| `FILE_MAX_UPLOAD_MB` | `50` | 单文件上传上限 |
| `FILE_MAX_ARCHIVE_MB` | `200` | 整目录打包下载上限 |

## 完整清单

以上为常用项，完整定义以 `backend/app/core/config.py` 与 `backend/.env.example` 为准。
