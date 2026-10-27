---
title: 应用管理
description: 应用创建、详情、销毁、部署、停启、配额调整、配置更新与运行状态查询。
---

## 接口清单

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/api/apps` | 应用列表；普通用户仅见自己的应用 | 登录用户 |
| POST | `/api/apps` | 创建应用，归属当前用户 | 登录用户 |
| GET | `/api/apps/{id}` | 应用详情 | 属主或管理员 |
| DELETE | `/api/apps/{id}` | 销毁应用 | 属主或管理员 |
| POST | `/api/apps/{id}/deploy` | 部署应用，起容器并同步路由 | 属主或管理员 |
| POST | `/api/apps/{id}/stop` | 停止应用 | 属主或管理员 |
| POST | `/api/apps/{id}/restart` | 重启应用 | 属主或管理员 |
| POST | `/api/apps/{id}/rollback` | 回滚到上一个蓝绿版本（多容器 compose） | 属主或管理员 |
| PATCH | `/api/apps/{id}/quota` | 调整 CPU 与内存配额 | 属主或管理员 |
| PATCH | `/api/apps/{id}/config` | 更新环境变量与启动命令 | 属主或管理员 |
| GET | `/api/apps/{id}/runtime` | 容器实时状态与资源占用，可选 `service` | 属主或管理员 |
| GET | `/api/apps/{id}/readiness` | HTTP 就绪探测结论 | 属主或管理员 |
| GET | `/api/apps/{id}/metrics` | 资源曲线，可选 `service` 过滤 | 属主或管理员 |

## 主要参数

- 创建应用：`name`、`source_type`、`git_url`、`image`、`container_port`、`mem_limit`、`cpu_quota`、`env` 与 `command`。
- 部署应用：`image`、`env`、`command` 均可选；`reassign_port=true` 时丢弃原宿主端口并重新分配。
- 配额调整：`mem_limit` 为内存上限字符串，`cpu_quota` 为 CPU 核数，需先通过资源校验。
- 配置更新：`env` 为键值对对象，`command` 为可选启动命令；保存后需重新部署生效。

## 配额约束

配额以宿主真实资源为上限。内存使用硬限制，超限容器会被内核终止；CPU 使用 CFS 配额，
受限应用占用率被压制在配额附近。端口在 20000–29999 区间内分配，冲突时自动重试。

## 服务列表（多容器 compose）

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/api/apps/{id}/services` | 应用内服务列表（名称、镜像、容器、端口、状态、是否公开） | 登录用户 |
| POST | `/api/apps/{id}/services/{name}/restart` | 只重启指定服务的容器 | 属主或管理员 |

应用详情响应 `AppOut` 也包含 `services` 数组。单容器应用同样有一条 `name="default"` 的服务记录，
便于前端统一展示；多容器 compose 会为每个服务各生成一条记录，`is_public=true` 的服务负责对外访问。
每个公开服务携带 `is_primary` 与 `access_url`（主入口用应用域名，其他公开服务用独立子域名）。
应用响应中的 `active_slot` / `previous_slot` 表示当前与上一蓝绿槽位，`previous_slot` 非空时前端展示回滚入口。
服务级指标、日志、终端与文件接口均可通过 `service=<名称>` 指定目标，缺省取公开服务。

## 使用流程

应用从创建到可访问的完整操作步骤见[应用交付全流程](/guide/apps/)；
涉及配额与容量的排障见[验收与故障排查](/deploy/verify/)。

## 相关页面

- 返回[接口总览](/reference/api/)
- 源码接入与构建：[源码与构建](/reference/api/source-build/)
- 部署、路由与观测：[部署与运行观测](/reference/api/runtime/)
