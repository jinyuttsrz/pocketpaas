---
title: 监控、告警与审计
description: 指标采样、HTTP 就绪探针四态、自愈策略、阈值告警中心与操作审计。
---

平台的可观测能力由四部分组成：指标采样、就绪探针、告警中心、操作审计，另有一键诊断作为排障入口。

## 指标采样

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `METRICS_INTERVAL` | `5` | 应用资源采样间隔（秒） |
| `METRICS_KEEP` | `720` | 每个应用保留的采样条数 |
| `HOST_METRICS_INTERVAL` | `2` | 宿主机性能采样间隔（秒） |
| `HOST_METRICS_KEEP` | `90` | 宿主指标保留帧数（约 3 分钟滚动窗口） |
| `HOST_CONTAINER_INTERVAL` | `5` | 受管容器占用（CPU / 内存 / 网络）刷新间隔 |

前端据此绘制：

- 应用详情抽屉 → 「负载曲线」tab：该应用的 CPU / 内存曲线；
- 「资源与节点」页：宿主 CPU / 内存 / 磁盘实时曲线、受管容器资源占用、端口区间用量。

## HTTP 就绪探针（四态）

| 状态 | 判定 | 处理 |
|---|---|---|
| `ready` | HTTP 状态码 < 500 | 视为可服务 |
| `degraded` | HTTP 状态码 ≥ 500 | **只展示，不重启**，避免重启风暴 |
| `unready` | 连接被拒 / 超时 | 计入连续失败次数，达阈值才触发重启 |
| `unknown` | 尚未探测或无法判断 | 仅展示 |

探活目标为 `127.0.0.1:{宿主端口}{HEALTH_HTTP_PATH}`。

相关配置：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ENABLE_HEALTH_WATCHDOG` | `true` | 自愈巡检总开关 |
| `HEALTH_CHECK_INTERVAL` | `15` | 巡检间隔（秒） |
| `ENABLE_HEALTH_HTTP` | `true` | HTTP 就绪探针开关 |
| `HEALTH_HTTP_PATH` | `/` | 探活路径 |
| `HEALTH_HTTP_TIMEOUT` | `2.0` | 单次探活超时（秒） |
| `HEALTH_HTTP_MAX_FAILS` | `3` | 连续失败多少次触发重启 |
| `HEALTH_MAX_RESTARTS` | `3` | 连续自动重启上限，超过则置 `failed` |

:::note[为什么 5xx 不触发重启]
应用自身返回 5xx 说明进程还在、只是业务出错，重启通常无效且会造成重启风暴。
只有**连接层**不可达才计入失败并重启。
:::

## 指标阈值告警中心

五类规则：

| 规则 | 配置项 | 默认阈值 |
|---|---|---|
| 宿主 CPU | `ALERT_CPU_PERCENT` | `85.0` |
| 宿主内存 | `ALERT_MEM_PERCENT` | `85.0` |
| 宿主磁盘 | `ALERT_DISK_PERCENT` | `90.0` |
| 应用自动重启 | `ALERT_RESTART_COUNT` | `3` |
| 应用失败 | — | 状态置 `failed` 时触发 |

状态流转：

```text
active（待处理） --手动确认--> acked（已确认，指标持续超限仅刷新观测值）
active / acked  --指标回落--> resolved（巡检自动置为已恢复）
```

:::tip[确认后不会重复打扰]
告警确认后，即使指标持续超限也不会反复产生新告警，只会刷新观测值；
指标回落后由巡检自动置 `resolved`。
:::

巡检开关与间隔：`ENABLE_ALERTS=true`、`ALERT_CHECK_INTERVAL=30`。

## 操作审计日志

覆盖 **21 个动作**：

- 认证：登录 / 注册 / 登出
- 应用：创建 / 构建 / 部署 / 停止 / 重启 / 配额 / 配置 / 源码 / 文件 / 诊断 / 健康探针 / Webhook 及其重置 / 销毁
- 用户：创建 / 更新 / 重置密码 / 删除

「审计日志」页提供动作字典与审计流水，普通用户按租户过滤，管理员可见全部。
接口为 `GET /api/audit/logs`（`limit ≤ 500`）与 `GET /api/audit/actions`。

## 一键诊断

对单个应用执行 **8 项只读检查**：

| 检查项 | 关注点 |
|---|---|
| 应用状态 | 数据库中的状态是否与预期一致 |
| 源码目录 | `workspace/{app_id}/src` 是否存在且非空 |
| 镜像 | 应用镜像是否已构建 |
| 容器 | 容器是否存在、是否运行 |
| 宿主端口 | 分配的端口是否被占用 / 是否可连 |
| 反向代理路由 | `paas-{id}.conf` 是否生成且 `listen` 正确 |
| 最近部署 | 最近一次部署记录与错误信息 |
| 资源配额 | 配额设置是否合理 |

单项结果为 `ok` / `warn` / `fail` / `skip`，全部只读，不产生副作用。
适合作为「应用打不开」时的第一条排查命令。

## 相关接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/system/host-metrics` | 宿主机实时性能 |
| GET | `/api/apps/{app_id}/runtime` | 容器实时状态与资源占用 |
| GET | `/api/apps/{app_id}/readiness` | HTTP 就绪探测结论（只读） |
| GET | `/api/apps/{app_id}/metrics` | 应用资源曲线 |
| GET | `/api/apps/{app_id}/diagnose` | 一键诊断（8 项检查） |
| GET | `/api/alerts` | 告警列表（`status` / `level` 过滤） |
| POST | `/api/alerts/{alert_id}/ack` | 确认告警 |
| GET | `/api/audit/logs` | 审计日志 |
