# PocketPaaS

> 轻量级容器云平台与应用自动化交付系统

**Python 3.12 ｜ FastAPI ｜ Docker ｜ Nginx ｜ SQLite ｜ WebSocket**

PocketPaaS 在单机 Docker 之上提供类 PaaS 的应用交付能力：上传源码或提供 Git 仓库，平台自动识别项目类型、生成 Dockerfile、构建镜像、启动容器，并动态渲染 Nginx 路由。它面向中小团队、教学环境与个人开发者，用单进程控制面替代 Kubernetes 的集群复杂度。

**完整文档：<https://www.jinyuttsrz.top/project/pocketpaas/>**

## 功能概览

- **应用交付**：zip 上传、Git 仓库、内置示例包三种源码来源；自动生成 Dockerfile；一键构建、部署、停止、重启。
- **动态路由**：应用分配全局唯一子域名，`route_service` 渲染路由并热载 Nginx，无需手工改配置。
- **运行时管理**：在线编辑环境变量与启动命令、调整 CPU / 内存配额，并提供实时日志与容器 Web 终端。
- **文件管理**：同时支持宿主源码目录与运行中容器文件系统，可浏览、编辑、上传、下载、重命名与删除。
- **安全隔离**：容器能力裁剪、独立隔离网桥、Dockerfile 危险指令扫描、Git 私网地址拦截、PBKDF2 口令哈希。
- **可观测**：指标采样与负载曲线、四态 HTTP 就绪探针、自愈巡检、阈值告警中心、操作审计、一键诊断（8 项）。
- **自动化**：带 HMAC 签名校验的 Git Webhook 自动部署，构建期间旧容器继续对外服务。
- **多租户**：应用按属主隔离，管理员可跨租户管理，并带最后管理员保护。

## 快速开始

### 本地运行（WSL2 + Docker Desktop）

```bash
cd backend
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问 `http://localhost:8000/`，默认账号 `admin` / `admin12345`（仅在库中无任何用户时创建，登录需填图形验证码）。

> 不要在 Windows 原生环境运行；`.venv/bin/uvicorn` 的 shebang 可能指向旧路径，统一用 `python -m uvicorn` 启动。

### 服务器部署（Docker Compose）

```bash
cp backend/.env.prod.example backend/.env.prod   # 先改掉默认管理员口令
docker compose up -d --build
```

对外只需开放 `80` / `443`；**不要**开放 `8000`、`8080`、`20000-29999`。
`docker-compose.yml` 必须保留 `./demo:/demo:ro`，否则示例包列表为空。

## 文档导航

| 主题 | 地址 |
|---|---|
| 项目简介与定位 | <https://www.jinyuttsrz.top/project/pocketpaas/start/overview/> |
| 快速开始 | <https://www.jinyuttsrz.top/project/pocketpaas/start/quickstart/> |
| 本机运行（WSL2） | <https://www.jinyuttsrz.top/project/pocketpaas/start/local/> |
| 服务器部署 | <https://www.jinyuttsrz.top/project/pocketpaas/deploy/server/> |
| 双层 Nginx 与 HTTPS | <https://www.jinyuttsrz.top/project/pocketpaas/deploy/nginx/> |
| 部署验收与排查 | <https://www.jinyuttsrz.top/project/pocketpaas/deploy/verify/> |
| 应用交付流程 | <https://www.jinyuttsrz.top/project/pocketpaas/guide/apps/> |
| 文件管理与终端 | <https://www.jinyuttsrz.top/project/pocketpaas/guide/files-terminal/> |
| 监控、告警与日志 | <https://www.jinyuttsrz.top/project/pocketpaas/guide/observability/> |
| Git Webhook 自动部署 | <https://www.jinyuttsrz.top/project/pocketpaas/guide/webhook/> |
| 系统架构 | <https://www.jinyuttsrz.top/project/pocketpaas/reference/architecture/> |
| 配置项清单 | <https://www.jinyuttsrz.top/project/pocketpaas/reference/config/> |
| API 与 WebSocket | <https://www.jinyuttsrz.top/project/pocketpaas/reference/api/> |
| 常见问题 | <https://www.jinyuttsrz.top/project/pocketpaas/about/faq/> |

## 目录结构

```text
├── backend/            后端服务与零构建前端（FastAPI + 原生 JS）
│   ├── app/api/        路由层
│   ├── app/services/   应用、构建、Docker、路由、指标、告警等
│   ├── app/web/        index.html / app.js / styles.css / login.*
│   └── Dockerfile      平台镜像（python:3.12-slim + nginx）
├── demo/               4 个内置示例源码包
├── deploy/             双层 Nginx 配置（console.conf / baota-front.conf）
├── wiki/               Astro 文档站源码（构建产物部署于主站子路径）
└── docker-compose.yml  线上编排
```

## 许可证

采用 **GNU General Public License v3.0（GPL-3.0）**，全文见 [LICENSE](LICENSE)。
可自由使用、修改、分发；分发衍生作品时需同样以 GPL-3.0 开源并提供完整源码。
