---
title: API 概览
description: 按模块列出 PocketPaaS 的代表性接口，完整端点以运行时 Swagger UI 为准。
---

完整端点与参数以运行时 **Swagger UI** 为准：`http://<host>:8000/docs`。

## 系统与探针（`/api`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 存活探针 |
| GET | `/api/ready` | 就绪探针（含 Docker 连通性） |
| GET | `/api/system/host-metrics` | 宿主机实时性能 |
| POST | `/api/system/resource-check` | 端口 / 内存 / CPU 资源校验 |
| GET | `/api/system/demos` | 内置示例包列表 |

## 认证（`/api/auth`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/auth/captcha` | 图形验证码（SVG，匿名） |
| POST | `/api/auth/login` | 登录（写 HttpOnly Cookie） |
| POST | `/api/auth/register` | 注册并自动登录 |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/me` | 当前登录用户 |

## 用户管理（`/api/users`，全部 `require_admin`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/users` | 用户列表 |
| POST | `/api/users` | 新建用户 |
| PATCH | `/api/users/{user_id}` | 改显示名 / 角色 / 启用状态 |
| PUT | `/api/users/{user_id}/password` | 重置口令 |
| DELETE | `/api/users/{user_id}` | 删除用户 |

## 应用与交付（`/api/apps`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/api/apps` | 应用列表 / 创建应用 |
| POST | `/api/apps/{app_id}/source` | 上传 zip 源码包 |
| POST | `/api/apps/{app_id}/source/git` | 以 Git 仓库作为源码 |
| POST | `/api/apps/{app_id}/source/demo` | 以内置示例包作为源码 |
| POST | `/api/apps/{app_id}/build` | 构建镜像 |
| POST | `/api/apps/{app_id}/deploy` | 部署（起容器） |
| POST | `/api/apps/{app_id}/stop` | 停止容器 |
| POST | `/api/apps/{app_id}/restart` | 重启容器 |
| PATCH | `/api/apps/{app_id}/quota` | 调整 CPU / 内存配额 |
| PATCH | `/api/apps/{app_id}/config` | 更新环境变量与启动命令 |
| GET | `/api/apps/{app_id}/runtime` | 容器实时状态与资源占用 |
| GET | `/api/apps/{app_id}/readiness` | HTTP 就绪探测结论（只读） |
| GET | `/api/apps/{app_id}/metrics` | 应用资源曲线 |
| GET | `/api/apps/{app_id}/build/logs` | 最近一次构建日志 |

## 项目文件（`/api/apps/{app_id}/files`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/apps/{app_id}/files` | 浏览目录（`target=source\|container`） |
| GET / PUT | `/api/apps/{app_id}/files/content` | 读取 / 保存文本文件 |
| POST | `/api/apps/{app_id}/files/upload` | 上传文件（支持多选） |
| POST | `/api/apps/{app_id}/files` | 新建 / 重命名 / 删除（`op=mkdir\|rename\|delete`） |
| GET | `/api/apps/{app_id}/files/download` | 下载文件或目录打包 zip |

## 诊断

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/apps/{app_id}/diagnose` | 一键诊断（8 项检查，只读） |

## Webhook

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/apps/{app_id}/webhook` | 接收 Git 推送（匿名，HMAC 校验） |
| GET | `/api/apps/{app_id}/webhook` | 查看 Webhook 配置 |
| POST | `/api/apps/{app_id}/webhook/reset` | 重置签名密钥 |
| POST | `/api/apps/{app_id}/webhook/trigger` | 手动触发自动部署 |

## 审计与告警

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/audit/logs` | 审计日志（多租户过滤，`limit ≤ 500`） |
| GET | `/api/audit/actions` | 动作字典（21 个动作） |
| GET | `/api/alerts` | 告警列表（`status` / `level` 过滤） |
| POST | `/api/alerts/{alert_id}/ack` | 确认告警 |

## WebSocket

| 协议 | 路径 | 说明 |
|---|---|---|
| WS | `/ws/apps/{app_id}/logs` | 容器日志实时流（`?tail=` 回放；Cookie 或 `?token=` 鉴权） |
| WS | `/ws/apps/{app_id}/shell` | 容器 Web 终端（交互式 TTY） |

:::note[鉴权方式]
HTTP 接口使用 HttpOnly Cookie 会话；WebSocket 支持 Cookie 鉴权，
也可用查询参数 `?token=` 传递会话令牌。
:::
