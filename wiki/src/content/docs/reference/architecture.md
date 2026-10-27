---
title: 系统架构
description: 控制面与数据面的职责划分、四层结构、流量路径、模块与数据表。
---

## 四层架构

![PocketPaaS 四层总体架构](/images/diagrams/arch.png)

<div class="diagram-caption">图　PocketPaaS 四层总体架构</div>

## 控制面与数据面

| 平面 | 承担者 | 职责 |
|---|---|---|
| 控制面 | PocketPaaS 本体（单个 FastAPI 进程） | 应用编排、源码接收、镜像构建、容器生命周期、路由渲染、指标采集、告警与审计。**不承载用户业务流量**，只通过 `docker.sock` 指挥宿主 Docker |
| 数据面 | 用户应用容器 | 真正对外提供服务，端口从 `20000~29999` 区间分配并发布到宿主机 |

另外两条边界：

- 应用容器与平台控制面之间通过**专用隔离网桥**（默认 `paas-net`）分隔，容器间互通被关闭；
- 平台容器自身使用 `network_mode: host`，这样容器内的 `127.0.0.1` 才等于宿主机，
  内层 nginx 才能反代到宿主端口上的应用容器。

## 流量路径

1. 外部请求进入宝塔 nginx（`:80`），按 `server_name` 匹配控制台或应用泛域名；
2. 请求透传 `Host` 转发到平台内层 nginx（`:8080`）；
3. 内层 nginx 再按 `Host` 分流：
   - 控制台域名 → `127.0.0.1:8000`
   - 应用子域名 → `127.0.0.1:{分配的宿主端口}`

![部署拓扑与流量路径](/images/diagrams/topology.png)

<div class="diagram-caption">图　部署拓扑与流量路径</div>

## 本地 / 线上双模式

由 `DEPLOY_MODE` 切换，代码层面不需要改动：

| 模式 | 取值 | 行为 |
|---|---|---|
| 本地 | `local` | 应用走宿主端口直连，通常不启用 nginx 动态路由（`ENABLE_NGINX=false`） |
| 线上 | `prod` | 启用泛域名 + 反向代理，应用通过子域名访问 |

## 服务层模块

`backend/app/services/` 共 19 个服务，代表模块：

| 模块 | 职责 |
|---|---|
| `app_service` | 应用主流程编排 |
| `builder_service` | Dockerfile 生成与镜像构建 |
| `docker_service` | 容器生命周期、日志流、exec |
| `source_service` | zip / Git / 示例包源码接入 |
| `compose_service` | 多服务 Docker Compose 解析、安全字段白名单与依赖排序 |
| `compose_engine` | 逐服务构建、应用网络 / 命名卷、依赖启动、蓝绿槽位与多容器生命周期 |
| `route_service` | 渲染 server 块、`nginx -t`、reload |
| `metrics_service` | 应用服务级指标采样 |
| `watchdog_service` | 服务级 HTTP 就绪探针与自愈重启 |
| `alert_service` | 阈值告警规则与状态流转 |
| `audit_service` | 操作审计记录 |
| `auth_service` / `captcha_service` | 登录鉴权与图形验证码 |
| `security_service` | 容器加固、Dockerfile 扫描、SSRF 防护 |
| `file_service` | 双通道文件管理 |
| `diagnose_service` | 一键诊断 8 项检查 |
| `ai_service` | AI 只读运行分析、上下文脱敏与提示词注入防护 |
| `webhook_service` | Webhook 签名校验与自动部署 |
| `demo_service` | 内置示例包 |
| `port_allocator` | 端口分配与竞态重试 |
| `resource_service` | 宿主资源预校验 |

## 数据表（10 张）

| 表 | 用途 |
|---|---|
| `apps` | 应用主表 |
| `app_services` | 应用内服务（多容器 compose 的运行时单元） |
| `deployments` | 部署记录 |
| `routes` | 路由表 |
| `build_logs` | 构建日志 |
| `metrics` | 指标采样 |
| `users` | 用户表 |
| `auth_sessions` | 登录会话 |
| `alerts` | 告警记录 |
| `audit_logs` | 操作审计 |

:::note[无迁移框架]
项目未引入 Alembic，新增字段采用幂等的 `ALTER TABLE` 方式补齐，兼容已有数据库。
:::

## 关键实现要点

| 主题 | 做法 |
|---|---|
| 端口分配 | 在区间内分配宿主端口，冲突按 `MAX_PORT_RETRY` 重试，并用原子分配避免 TOCTOU 竞态 |
| 资源隔离 | 依赖 cgroup v2 限制 CPU / 内存，内存为硬约束；CPU 按配额比例生效 |
| 容器加固 | `no-new-privileges`、`cap_drop=ALL` 后按需 `cap_add`、`pids_limit`、内存硬限制 |
| 路由热载 | 渲染 → `nginx -t` 校验 → `nginx -s reload`，校验失败不生效 |
| 稳定性 | 部署失败残留清理、端口竞态重试、就绪探针 + 自愈巡检、路由同步降级 |
| 并发部署 | Webhook 使用进程内并发锁，重入返回 409 |

## 代码规模

含空行，已排除 `.venv` 与 `__pycache__`：

- 后端 `app/**/*.py`：48 个文件 / 约 7759 行
- 前端 `index.html` 999 行、`app.js` 2558 行、`styles.css` 1629 行
- 风格约定：ruff `line-length = 100`、`target-version = "py312"`
