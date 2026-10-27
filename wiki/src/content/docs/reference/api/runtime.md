---
title: 部署与运行观测
description: 部署流程、路由查询、资源曲线与一键诊断接口及其行为约定。
---

## 接口清单

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/api/apps/{id}/deploy` | 部署应用并同步路由（多容器为蓝绿发布） | 属主或管理员 |
| POST | `/api/apps/{id}/rollback` | 回滚到上一个蓝绿版本 | 属主或管理员 |
| GET | `/api/apps/{id}/route` | 查询应用主入口路由 | 属主或管理员 |
| GET | `/api/apps/{id}/routes` | 查询全部公开服务路由 | 属主或管理员 |
| GET | `/api/apps/{id}/metrics` | 查询资源曲线，`service` 可选 | 属主或管理员 |
| GET | `/api/apps/{id}/runtime` | 容器实时状态，`service` 可选 | 属主或管理员 |
| GET | `/api/apps/{id}/diagnose` | 一键诊断，8 项只读检查（多服务逐服务分组） | 属主或管理员 |

## 部署流程

部署时平台先校验应用状态，再原子预留宿主端口，随后创建并启动容器，落库部署记录，
最后渲染 Nginx server 块、执行 `nginx -t` 校验并热载。

- 起容器失败时清理残留容器、摘除已写路由并置为 failed；
- 端口冲突时换端口重试，最多 5 轮；
- 支持 `reassign_port=true` 丢弃原宿主端口并重新分配。

多容器 compose 部署采用蓝绿槽位：新版本容器在独立网络中启动，全部健康后切换路由，
旧槽位停止但保留；部署或路由切换失败会自动回退旧槽位。部署记录中的 `slot` 与 `strategy`
（`rolling` / `bluegreen` / `bluegreen_failed` / `rollback`）可用于追溯发布方式。

## 诊断与指标

一键诊断依次检查应用状态、源码目录、镜像、容器、宿主端口、反向代理路由、最近部署与资源配额，
每项结论取 `ok`、`warn`、`fail`、`skip` 之一。

指标采样每 5 秒一次，保留最近 720 条，用于控制台绘制负载曲线。多容器应用按服务分别采样，
`GET /metrics?service=<服务名>` 返回指定服务的曲线；不传 `service` 时取公开服务，
无服务记录的历史应用回退到应用整体采样。

## 相关页面

- 返回[接口总览](/reference/api/)
- 监控、告警与诊断的使用说明：[监控、告警与审计](/guide/observability/)
- 分层验收方法：[验收与故障排查](/deploy/verify/)
