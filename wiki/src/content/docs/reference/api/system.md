---
title: 告警、审计与系统
description: 告警列表与确认、审计日志与动作字典、宿主指标、示例包、资源预校验与健康探针。
---

## 接口清单

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/api/alerts` | 告警列表，按状态与级别过滤 | 登录用户 |
| POST | `/api/alerts/{id}/ack` | 确认告警 | 登录用户 |
| GET | `/api/audit/logs` | 审计日志，支持多租户过滤与关键字检索 | 登录用户 |
| GET | `/api/audit/actions` | 审计动作字典 | 登录用户 |
| GET | `/api/system/host-metrics` | 宿主机实时性能 | 登录用户 |
| GET | `/api/system/demos` | 内置示例包列表 | 登录用户 |
| POST | `/api/system/resource-check` | 端口、内存与 CPU 资源预校验 | 登录用户 |
| GET | `/api/health` | 存活探针 | 匿名 |
| GET | `/api/ready` | 就绪探针，包含 Docker 连通性 | 匿名 |

## 查询参数

- 告警列表支持 `status`、`level` 与 `limit`。
- 审计日志支持 `action`、`target_type`、`username`、`limit` 与 `offset`，其中 `limit` 最大 500。
- 普通用户仅能看到自己的应用告警与自己的审计记录，管理员可见全量。
- 资源预校验请求包含 `container_port`、`mem_limit`、`cpu_quota` 与可选的 `app_id`；
  端口、内存或 CPU 越界时接口返回 400 与中文提示。

## 告警规则

告警中心每 30 秒评估一次五类规则：宿主 CPU、内存、磁盘占用，以及应用连续重启与应用失败。

告警状态为 `active`、`acked`、`resolved`；同一规则与同一目标至多保留一条未消除记录。

## 审计动作

审计动作字典共 21 项，覆盖登录、注册、登出、应用创建、构建、部署、停止、重启、配额调整、
配置更新、源码上传、文件管理、诊断、健康探针、自动部署、Webhook 密钥重置、销毁应用以及用户管理相关操作。

审计日志支持按 `limit` 限制返回条数，最大 500 条。

## 相关页面

- 返回[接口总览](/reference/api/)
- 告警、审计与诊断的使用说明：[监控、告警与审计](/guide/observability/)
- 阈值与采样配置：[配置项](/reference/config/)
