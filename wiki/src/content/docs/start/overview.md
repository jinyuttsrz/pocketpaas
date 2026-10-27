---
title: 项目简介
description: PocketPaaS 的定位、核心能力、技术栈与目录结构总览。
---

PocketPaaS 是一套构建在**单机 Docker 引擎**之上的「控制面—数据面」分离的类 PaaS 平台。
用户只需提供源码（上传 zip、Git 仓库或内置示例包），平台即自动识别项目类型、生成 Dockerfile、
构建镜像、启动容器，并为其动态渲染 Nginx 反向代理路由。

整个交付闭环为：

```text
创建应用 → 引入源码 → 生成 Dockerfile → 构建镜像 → 起容器 → 动态反向代理 → 实时日志 → 监控与告警
```

## 为什么做这个

- **Kubernetes 的复杂度与本项目规模不匹配**：一套可用的 K8s 集群需要多节点、etcd、CNI/CSI、Ingress Controller、CRD/Operator 等一整套组件，学习与运维成本远超中小团队与教学场景的实际需要。
- **资源开销敏感**：K8s 控制面自身就要吃掉可观的 CPU 与内存，而本项目控制面是**单个 Python 进程**，可在单台机器上与业务容器共存。
- **单机即可跑通全链路**：不依赖集群编排，直接复用宿主 Docker 引擎（通过 `docker.sock`）管理应用容器，开箱即用。
- **前端零构建**：控制台为原生 JavaScript + 手写 SVG 图表的零依赖 SPA，由 FastAPI `StaticFiles` 直接托管，无 npm、无打包工具、无任何 CDN 引用。
- **取舍明确**：牺牲多节点调度与高可用，换取低开销、易部署、易理解；适合单机部署平台、教学演示与个人项目托管。

## 功能总览

| 分组 | 能力 |
|---|---|
| 应用交付 | 三种源码来源（zip / Git / 内置示例包）、项目类型识别、Dockerfile 自动生成、镜像构建与构建日志、容器生命周期（部署/停止/重启/销毁）、WebSocket 实时日志 |
| 运行时配置 | 环境变量与启动命令可视化编辑、CPU / 内存配额调整（调整前对照宿主真实资源预校验） |
| 路由与访问 | Nginx 动态反向代理、全局唯一应用子域名、本地端口直连 / 线上域名路由双模式 |
| 安全与隔离 | 容器加固（禁止提权、能力裁剪、进程数上限、内存硬限制）、专用隔离网桥、Dockerfile 危险指令扫描、Git 私有地址 SSRF 拦截、PBKDF2 口令哈希、HttpOnly 会话、零依赖图形验证码 |
| 多租户与权限 | 应用按属主隔离、管理员跨租户管理、最后管理员保护、用户 CRUD 与角色/状态管理、修改显示名、重置口令 |
| 可观测与运维 | 应用与宿主指标采样、HTTP 就绪探针四态、自愈重启、五类阈值告警、21 类操作审计、8 项一键诊断 |
| 文件与终端 | 源码目录与容器文件系统双通道管理（浏览/编辑/上传/下载/重命名/删除）、容器 Web 终端（全局单会话互斥） |
| 自动化交付 | 带 HMAC 签名校验的 Git Webhook 自动部署，构建期间旧容器继续服务 |

## 技术栈

| 层次 | 选型 | 说明 / 版本 |
|---|---|---|
| 后端 | Python | 3.12（`pyproject` 要求 `>=3.12,<3.13`，镜像 `python:3.12-slim`） |
| 后端框架 | FastAPI `0.115.6` + Uvicorn `0.34.0` | ASGI 服务与 Web 框架 |
| 数据校验 | Pydantic `2.10.4` / pydantic-settings `2.7.0` | 请求响应模型与配置加载 |
| 持久化 | SQLAlchemy `2.0.36` + SQLite | `DB_URL` 可切 MySQL |
| Docker 交互 | docker-py `7.1.0` | 容器生命周期 / 镜像构建 / 日志流 / exec 终端 |
| 其他依赖 | python-multipart `0.0.20`、websockets `14.1`、psutil `6.1.1` | 表单上传 / WS / 系统指标 |
| 前端 | 原生 JavaScript + 手写 SVG 图表 | 零构建、零外部依赖，FastAPI `StaticFiles` 托管 |
| 反向代理 | Nginx | `route_service` 渲染 server 块 + `nginx -t` 校验 + `nginx -s reload` 热载 |
| 实时通信 | WebSocket | 容器日志流、容器终端 |

## 目录结构

```text
F:\test/                        仓库根目录
├── backend/                    后端服务（含前端静态资源）
│   ├── app/
│   │   ├── api/                路由层：console, system, auth, users, apps,
│   │   │                       files, diagnose, webhook, audit, alerts, ws
│   │   ├── services/           服务层（19 个）
│   │   ├── models/             models.py：9 张数据表
│   │   ├── schemas/            Pydantic 请求 / 响应模型
│   │   ├── core/               config.py / database.py / deps.py
│   │   ├── web/                零构建前端：index.html / app.js / styles.css
│   │   └── main.py             FastAPI 实例、lifespan、router 注册、静态托管
│   ├── Dockerfile              平台镜像：python:3.12-slim + nginx/git/procps/curl
│   ├── entrypoint.sh           先拉起内层 nginx，再 exec 平台本体
│   ├── requirements.txt        依赖清单
│   ├── .env / .env.example     本地开发配置
│   └── .env.prod.example       线上配置模板（复制为 .env.prod）
├── demo/                       5 个内置示例代码包
├── deploy/                     双层 Nginx 配置：console.conf / baota-front.conf
├── docker-compose.yml          线上编排（单服务、host 网络）
├── wiki/                       本文档站（Astro + Starlight）
├── README.md                   项目说明
├── LICENSE                     开源许可证（GPL-3.0）
├── 本地运行说明.md               本机（WSL2 + Docker Desktop）运行手册
└── 云端运行说明.md               线上容器化部署手册
```

## 内置示例包

`demo/` 下提供 5 个示例包，服务于控制台「模板与指南」页的一键创建演示应用：

| 文件 | 内容 | 用途 |
|---|---|---|
| `static-demo.zip` | `static-demo/index.html` | 体验静态站点（`static` 类型）交付 |
| `python-demo.zip` | `python-demo/main.py` + `requirements.txt` | 体验 Python 应用交付 |
| `node-demo.zip` | `package.json` + `server.js` | 体验 Node 应用交付 |
| `custom-demo.zip` | `Dockerfile` + `index.html` | 体验「自带 Dockerfile」的自定义构建路径 |
| `compose-demo.zip` | `web + api + worker` 多服务 compose | 体验多公开服务、内部 DNS、健康检查、命名卷与蓝绿回滚 |

:::caution[示例包依赖挂载]
平台镜像**不包含** `demo/` 目录。容器化部署时必须保留 `./demo:/demo:ro` 挂载，
否则控制台「模板与指南」页的示例包下拉会显示「（未找到示例包）」。
:::

## 下一步

- 想先在本机跑起来：[本机运行](/start/local/)
- 要上线到服务器：[服务器部署](/deploy/server/)
- 想了解内部结构：[系统架构](/reference/architecture/)
