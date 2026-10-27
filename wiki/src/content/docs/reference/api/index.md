---
title: 接口文档
description: PocketPaaS 控制面 REST 与 WebSocket 接口的基础地址、鉴权方式、权限模型、错误码与端点总览。
---

PocketPaaS 控制面对外提供 REST 与 WebSocket 两类接口。本组页面按业务域拆分为 8 个分册，
覆盖认证、应用、构建、运行观测、文件、Webhook、系统能力与实时通道，供前端开发、运维与第三方集成参考。


## 基础地址

- 本地环境：`http://localhost:8000`
- 线上环境：`https://app.jinyuttsrz.top`
- REST 接口以 `/api` 开头，WebSocket 接口以 `/ws` 开头。
- 完整端点与在线调试以运行时 Swagger UI 为准：`http://<host>:8000/docs`。

## 请求与响应约定

- 请求体与响应体均为 UTF-8 编码的 JSON，文件上传使用 `multipart/form-data`。
- 成功响应为 2xx；创建类接口返回 201，删除与登出返回 200 或 204。
- 错误响应返回 4xx 或 5xx，响应体为 `{"detail": "错误说明"}`。
- 时间字段采用 ISO 8601 字符串。

## 鉴权方式

| 场景 | 方式 |
|---|---|
| 浏览器 | 登录后写入 HttpOnly Cookie `paas_token`，同源请求自动携带 |
| 脚本调用 | 请求头 `Authorization: Bearer <token>` |
| WebSocket | 优先使用 Cookie，其次使用 `?token=<token>` |
| 匿名接口 | 健康探针、就绪探针、认证状态、验证码、登录、注册、Webhook 回调 |

## 权限模型

平台采用应用属主隔离与基于角色的访问控制。普通用户仅能查看和操作自己的应用；
管理员可管理全部应用与用户账号。越权访问他人应用统一返回 404，不泄露应用是否存在。

## 错误码

| 状态码 | 含义 | 典型场景 |
|---|---|---|
| 400 | 请求参数错误 | 应用名非法、配额越界、文件路径不合法 |
| 401 | 未登录或会话失效 | 缺少 Cookie 或令牌过期 |
| 403 | 权限不足 | 普通用户访问管理员接口 |
| 404 | 资源不存在 | 应用不存在或无权访问 |
| 409 | 状态冲突 | 重复部署、容器未运行、Webhook 重复触发 |
| 422 | 参数校验失败 | 请求体字段缺失或类型错误 |
| 500 | 服务器内部错误 | Docker 调用失败、构建异常 |

## 分册导航

| 分册 | 内容 |
|---|---|
| [认证与用户](/reference/api/auth-users/) | 登录、注册、验证码、会话与用户管理 |
| [应用管理](/reference/api/apps/) | 应用 CRUD、部署、停启、配额与运行状态 |
| [源码与构建](/reference/api/source-build/) | zip / Git / 示例包接入、镜像构建与端口推断 |
| [部署与运行观测](/reference/api/runtime/) | 部署流程、路由、指标与一键诊断 |
| [文件管理](/reference/api/files/) | 源码与容器双通道、在线编辑、上传下载 |
| [Webhook 自动部署](/reference/api/webhook/) | 签名校验、配置、密钥重置与流水线 |
| [告警、审计与系统](/reference/api/system/) | 告警、审计、宿主指标、资源预校验与探针 |
| [WebSocket](/reference/api/websocket/) | 实时日志与容器 Web 终端协议 |

## 端点总览

| 方法 | 路径 | 分册 |
|---|---|---|
| GET | `/api/health` | 系统 |
| GET | `/api/ready` | 系统 |
| POST | `/api/system/resource-check` | 系统 |
| GET | `/api/system/host-metrics` | 系统 |
| GET | `/api/system/demos` | 系统 |
| GET | `/api/auth/status` | 认证 |
| GET | `/api/auth/captcha` | 认证 |
| POST | `/api/auth/register` | 认证 |
| POST | `/api/auth/login` | 认证 |
| POST | `/api/auth/logout` | 认证 |
| GET | `/api/auth/me` | 认证 |
| GET | `/api/users` | 用户 |
| POST | `/api/users` | 用户 |
| PATCH | `/api/users/{id}` | 用户 |
| PUT | `/api/users/{id}/password` | 用户 |
| DELETE | `/api/users/{id}` | 用户 |
| GET | `/api/apps` | 应用 |
| POST | `/api/apps` | 应用 |
| GET | `/api/apps/{id}` | 应用 |
| DELETE | `/api/apps/{id}` | 应用 |
| POST | `/api/apps/{id}/deploy` | 应用 |
| POST | `/api/apps/{id}/stop` | 应用 |
| POST | `/api/apps/{id}/restart` | 应用 |
| POST | `/api/apps/{id}/rollback` | 应用 |
| GET | `/api/apps/{id}/services` | 应用 |
| POST | `/api/apps/{id}/services/{name}/restart` | 应用 |
| PATCH | `/api/apps/{id}/quota` | 应用 |
| PATCH | `/api/apps/{id}/config` | 应用 |
| GET | `/api/apps/{id}/runtime` | 应用 |
| GET | `/api/apps/{id}/readiness` | 应用 |
| POST | `/api/apps/{id}/source` | 构建 |
| POST | `/api/apps/{id}/source/git` | 构建 |
| POST | `/api/apps/{id}/source/demo` | 构建 |
| POST | `/api/apps/{id}/build` | 构建 |
| GET | `/api/apps/{id}/build/logs` | 构建 |
| GET | `/api/apps/{id}/route` | 运行观测 |
| GET | `/api/apps/{id}/routes` | 运行观测 |
| GET | `/api/apps/{id}/metrics` | 运行观测 |
| GET | `/api/apps/{id}/diagnose` | 运行观测 |
| GET | `/api/ai/health` | AI |
| POST | `/api/ai/analyze` | AI |
| POST | `/api/ai/chat` | AI |
| GET | `/api/apps/{id}/files` | 文件 |
| GET | `/api/apps/{id}/files/content` | 文件 |
| PUT | `/api/apps/{id}/files/content` | 文件 |
| POST | `/api/apps/{id}/files/upload` | 文件 |
| POST | `/api/apps/{id}/files` | 文件 |
| GET | `/api/apps/{id}/files/download` | 文件 |
| POST | `/api/apps/{id}/webhook` | Webhook |
| GET | `/api/apps/{id}/webhook` | Webhook |
| POST | `/api/apps/{id}/webhook/reset` | Webhook |
| POST | `/api/apps/{id}/webhook/trigger` | Webhook |
| GET | `/api/alerts` | 告警 |
| POST | `/api/alerts/{id}/ack` | 告警 |
| GET | `/api/audit/logs` | 审计 |
| GET | `/api/audit/actions` | 审计 |

## 约定与限制

- 除匿名接口外，所有请求需携带 Cookie 或 Bearer 令牌。
- 应用相关接口统一进行属主校验，越权访问返回 404。
- 涉及容器操作的接口在容器未运行时返回 409。
- 同一应用同时只允许一条自动部署流水线，重复触发返回 409。
