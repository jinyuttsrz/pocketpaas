---
title: 快速开始
description: 两条最短路径跑起 PocketPaaS：本机开发与 Docker Compose。
---

PocketPaaS 只有两条最短启动路径：**本机开发**（WSL2 + Docker Desktop）与 **Docker Compose**（更接近线上）。
选之前先确认一件事：项目**不能直接在 Windows 原生运行**，必须在 Linux 环境里跑。

原因有两个：

1. Python 版本要求 `>=3.12,<3.13`；
2. `DOCKER_HOST=unix:///var/run/docker.sock` 是 Unix socket 路径，只在 Linux 侧有效（Windows 原生要用命名管道）。

## 路径 A：本机开发（推荐）

```bash
# 1. 在 WSL 的 Ubuntu 里确认 Docker 可用
wsl -d Ubuntu -- docker version

# 2. 切到后端目录（程序按当前工作目录解析 .env、SQLite 与工作区）
cd /mnt/f/test/backend

# 3. 启动（前台运行，Ctrl+C 停止）
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

看到下面两行即启动成功：

```text
INFO:  Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:  Application startup complete.
```

| 用途 | 地址 |
|---|---|
| 控制台首页 | `http://localhost:8000/` |
| 登录页 | `http://localhost:8000/login` |
| 接口文档（Swagger UI） | `http://localhost:8000/docs` |
| 健康检查 | `http://localhost:8000/api/health` |
| 就绪探针（含 Docker 状态） | `http://localhost:8000/api/ready` |

默认账号 **`admin` / `admin12345`**，登录页需要填写图形验证码。

:::note[默认账号的生效条件]
该账号**仅在数据库中一条用户都没有时**自动创建（`AUTH_BOOTSTRAP_ADMIN=true`）。
若 `backend/paas.db` 已存在且有用户，则使用之前设置的口令。
:::

:::tip[为什么用 `python -m uvicorn`]
`backend/.venv` 的元信息记录的是项目被移动前的旧路径，`./.venv/bin/uvicorn` 等脚本的
shebang 写死了旧路径，可能失效；以模块方式调用可绕开这个问题。详见[本机运行](/start/local/#关于-venv-的坑)。
:::

## 路径 B：Docker Compose

要求机器**已安装 Docker**，且宿主的 80 / 8080 端口规划需按实际环境调整。

```bash
# 1. 复制线上配置模板
cp backend/.env.prod.example backend/.env.prod

# 2. 按需编辑 .env.prod（至少改掉默认管理员口令）

# 3. 构建并启动
docker compose up -d --build
```

三点提示：

- 平台以 `network_mode: host` 运行，直接监听宿主 `8000`（host 网络下 `ports` 映射无效）。
- 宿主 **80 端口通常被宝塔 Nginx 占用**，本项目采用「外层宝塔 + 平台内层 Nginx」的双层方式。
- **`docker-compose.yml` 必须保留 `./demo:/demo:ro` 挂载**，否则示例包下拉为空。

:::caution[本机用 Compose 要先改三项]
`.env.prod.example` 是线上模板（`DEPLOY_MODE=prod`、`BASE_DOMAIN=beta.jinyuttsrz.top`、`ENABLE_NGINX=true`），
照抄到本机会因为等不到泛域名解析而行为异常。本机建议覆盖为：

```bash
DEPLOY_MODE=local
BASE_DOMAIN=
ENABLE_NGINX=false
```
:::

详细步骤见[服务器部署](/deploy/server/)与[本机运行](/start/local/)。

## 跑起来之后

登录控制台后，左侧导航共 **8 个页面**（分 3 组，「用户管理」仅管理员可见）：

| 分组 | 页面 | 能做什么 |
|---|---|---|
| 工作台 | 应用实例 | 创建应用、引入源码、构建、部署/停止/重启、改配额、编辑环境变量与启动命令、诊断、查看日志与负载曲线 |
| 工作台 | 网关路由 | 查看「域名 → 容器端口」映射与路由下发情况 |
| 工作台 | 模板与指南 | 用内置示例包一键创建应用、查看接入指南与 Dockerfile 约定 |
| 工作台 | 项目文件管理 | 在宿主源码目录与运行中容器文件系统之间切换，浏览/编辑/上传/下载 |
| 基础设施 | 资源与节点 | 宿主 CPU / 内存 / 磁盘曲线、受管容器资源占用、端口区间用量 |
| 基础设施 | 告警中心 | 阈值告警列表与确认处置 |
| 系统设置 | 审计日志 | 21 类操作的动作字典与审计流水 |
| 系统设置 | 用户管理 | 用户 CRUD、角色、启停、改显示名、重置口令（仅管理员） |

最快的体验路径：**模板与指南 → 选一个示例包 → 一键创建 → 构建 → 部署**。
