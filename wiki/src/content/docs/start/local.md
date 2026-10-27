---
title: 本机运行
description: 在 Windows + WSL2 Ubuntu + Docker Desktop 环境下启动 PocketPaaS 的完整手册。
---

适用环境：本机 Windows + WSL2 Ubuntu + Docker Desktop，项目位于 `F:\test`。
目标是在本机把 PocketPaaS 后端（含控制台前端）跑起来，用于开发与演示。

## 1. 前置条件检查

打开 PowerShell，确认 Docker 守护进程可用：

```powershell
wsl -d Ubuntu -- docker version
```

看到 `Server: Docker Engine` 且版本号正常即为就绪（示例环境为 29.7.2）。
如果报 `Cannot connect to the Docker daemon`，说明 Docker Desktop 没启动。

## 2. 启动 Docker Desktop

```powershell
Start-Process 'C:\Users\Administrator\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe'
```

等 30~90 秒（首次启动更久），再用上面的 `docker version` 确认。
托盘出现鲸鱼图标即表示在运行。

## 3. 进入 WSL 并切换目录

```powershell
wsl -d Ubuntu
```

```bash
cd /mnt/f/test/backend
```

:::caution[必须在 backend 目录启动]
程序按「当前工作目录」解析 `.env`、SQLite 文件（`./paas.db`）与工作目录（`./workspace`）。
换个目录启动会读不到配置。
:::

## 4. 启动服务

```bash
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

服务在前台运行，终端窗口会被占用；停止服务按 `Ctrl+C`。

开发时想改代码自动重启，追加 `--reload`：

```bash
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## 5. 访问控制台

WSL2 有 localhost 转发，Windows 浏览器直接访问 `localhost:8000` 即可，不用查 WSL 的 IP。

| 用途 | 地址 |
|---|---|
| 控制台首页 | `http://localhost:8000/` |
| 登录页 | `http://localhost:8000/login` |
| 接口文档（Swagger） | `http://localhost:8000/docs` |
| 健康检查 | `http://localhost:8000/api/health` |
| 就绪探针（含 Docker 状态） | `http://localhost:8000/api/ready` |

登录：用户名 `admin`，密码 `admin12345`，并填写图形验证码。

:::note
默认账号**只在数据库里一条用户都没有时**自动创建（`AUTH_BOOTSTRAP_ADMIN=true`）。
如果 `backend/paas.db` 已存在且已有用户，用的就是之前设过的密码。
:::

## 关于 `.venv` 的坑

`backend/.venv` 的元信息（`pyvenv.cfg`）显示它是在**旧路径**下创建的。项目搬家后：

| 项目 | 状态 |
|---|---|
| `./.venv/bin/python` | **可用**，它是指向 `/usr/local/bin/python3.12` 的绝对路径软链接 |
| `./.venv/bin/pip` 等脚本 | **可能失效**，脚本头写死了旧路径 |

所以：

- 启动服务用 `python -m uvicorn`，没问题；
- 装包时用模块方式调用：

```bash
./.venv/bin/python -m pip list
./.venv/bin/python -m pip install -r requirements.txt
```

实在别扭就按下面「全新重建虚拟环境」重建一个干净的 venv。

## 配置从哪里读

读取顺序为「环境变量 → `backend/.env` → 代码默认值」：`config.py` 通过 `SettingsConfigDict(env_file=".env")`
读取当前工作目录下的 `.env`，没写的项一律取代码默认值。

本机常用核心项：

| 配置项 | 本机值 | 含义 |
|---|---|---|
| `DEPLOY_MODE` | `local` | 应用走宿主端口直连 |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 平台监听地址与端口 |
| `BASE_DOMAIN` / `USE_HTTPS` | 空 / `false` | 本地不涉及域名与证书 |
| `ENABLE_NGINX` | `false` | 本地不启用动态域名路由 |
| `DB_URL` | `sqlite:///./paas.db` | SQLite 文件落在 `backend/paas.db` |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | 连本机 Docker 守护进程 |
| `WORKSPACE_DIR` | `./workspace` | 应用源码存放目录 |
| `DEMO_DIR` | `../demo` | 内置示例包目录（仓库根目录的 `demo/`） |
| `PORT_RANGE_START` / `PORT_RANGE_END` | `20000` / `29999` | 应用容器映射到宿主的端口区间 |
| `ENABLE_AUTH` | `true` | 接口与实时日志需携带有效会话 |

完整配置项见[配置项参考](/reference/config/)。

## 运行起来后能做什么

控制台是零构建 SPA，登录后左侧导航共 8 个页面：

| 分组 | 页面 | 能做什么 |
|---|---|---|
| 工作台 | 应用实例 | 创建、构建、部署、停止/重启、配额调整、配置编辑、一键诊断、构建日志与负载曲线 |
| 工作台 | 网关路由 | 查看「域名 → 容器端口」映射与路由下发情况 |
| 工作台 | 模板与指南 | 内置示例包一键创建；Python / 静态 / Node / Compose 接入指南 |
| 工作台 | 项目文件管理 | 双通道浏览 / 上传 / 下载 / 在线编辑 / 重命名 / 删除 |
| 基础设施 | 资源与节点 | 宿主 CPU / 内存 / 磁盘实时曲线、受管容器资源、端口用量 |
| 基础设施 | 告警中心 | 阈值告警与确认处置 |
| 系统设置 | 审计日志 | 25 类动作字典与审计流水（按租户过滤） |
| 系统设置 | 用户管理 | 用户 CRUD、角色、启停、改显示名、重置口令（仅管理员） |

应用详情抽屉内含 **7 个 tab**：概览、诊断、实时日志、构建日志、负载曲线、文件、终端。

## 常见操作

### 重置数据库（回到初始状态）

先 `Ctrl+C` 停掉服务，然后：

```bash
cd /mnt/f/test/backend
rm -f paas.db
```

再启动，数据库会重新建表并自动创建 `admin / admin12345`。如果还想清掉上传的应用：

```bash
rm -rf workspace/*
```

### 全新重建虚拟环境

当 `.venv` 真的损坏（比如 `python -m pip` 都用不了）时：

```bash
cd /mnt/f/test/backend
rm -rf .venv
/usr/local/bin/python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

重建后 venv 的路径就绑定到 `/mnt/f/test/backend/.venv`，**以后别再移动项目目录**。

### 清理遗留的测试容器

平台创建的容器统一带 `paas-` 前缀：

```bash
wsl -d Ubuntu -- docker ps --filter "name=paas-"
wsl -d Ubuntu -- docker rm -f <容器名> ...
```

正常清理建议直接在控制台「停止 / 销毁应用」，让数据库记录与容器状态保持一致。

### 可选：用 Docker Compose 跑整个平台

仓库根目录的 `docker-compose.yml` 构建 `backend/Dockerfile` 镜像（Python 3.12 + Nginx）。要点：

- 项目名 `pocketpaas`，单服务；
- `network_mode: host`、`user: root`：平台要能执行 `nginx -s reload` 并写入 conf.d，且容器内 `127.0.0.1` 需等同宿主机；
- 5 条挂载：`docker.sock`、`./paas-data/data:/data`、`./paas-data/nginx:/etc/nginx/conf.d`、
  `./deploy/console.conf`（只读）、`./demo:/demo:ro`（只读）；
- `env_file: ./backend/.env.prod`。

仓库里只有 `.env.prod.example`，**必须先创建 `.env.prod` 否则起不来**：

```bash
cd /mnt/f/test
cp backend/.env.prod.example backend/.env.prod
# 本机记得覆盖为 DEPLOY_MODE=local、BASE_DOMAIN=、ENABLE_NGINX=false
docker compose up -d --build
docker compose logs -f
```

停止：`docker compose down`。这条路径更接近线上形态，但调试不如直接启动方便，日常开发建议用前面几节的方式。

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| `Cannot connect to the Docker daemon` | Docker Desktop 没启动，或启动后未等就绪 |
| `ModuleNotFoundError: No module named 'app'` | 不在 `backend` 目录启动 → `cd /mnt/f/test/backend` 重来 |
| `[Errno 98] Address already in use` | 8000 被占用 → 换端口 `--port 8001`，或先停旧进程 |
| 登录一直提示验证码错误 | 验证码 120 秒过期，刷新页面重新获取 |
| 默认口令登不进 | 库中已有用户，默认口令不再生效 → 用之前设的密码，或重置库 |
| `pip` 报路径不存在 | venv 搬家导致 shebang 失效 → 用 `python -m pip` 或重建 venv |
| 页面能开但「部署应用」失败 | Docker 未就绪，看 `/api/ready` 返回的 docker 状态 |
| 示例包下拉显示「（未找到示例包）」 | 本地确认 `F:\test\demo` 下有 5 个 zip；Compose 模式下确认 `./demo:/demo:ro` 挂载 |
| 控制台页面像旧的 | 前端静态资源无缓存指纹 → 硬刷新（`Ctrl+F5`） |
| 改完环境变量应用行为没变 | 配置只写库，需**重新部署**才生效 |
| 容器终端连不上 | 应用须处于 `running`；全局同一时间只允许一个容器会话 |
