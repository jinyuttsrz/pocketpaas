---
title: 源码与构建
description: zip、Git、示例包与多服务 Docker Compose 的源码接入、逐服务镜像构建、依赖启动与端口推断规则。
---

## 源码来源

- 上传 zip 压缩包，服务端安全解压到应用工作目录。
- 填写 Git 仓库地址，请求字段为 `git_url`，平台克隆源码并拦截内网地址与云元数据地址。
- 选用内置示例包，请求字段为 `demo`，可选 `static`、`python`、`node`、`custom` 与 `compose` 五类。

## Docker Compose（单服务 / 多服务）

源码根目录存在 `docker-compose.yml` / `compose.yaml` 时，构建接口会自动按 compose 解析，
单服务与多服务均支持，无需额外的源码类型字段。

支持的 compose 字段：

| 字段 | 映射 |
|---|---|
| `image` / `build` | 服务镜像；构建阶段逐服务构建或拉取 |
| `ports` / `expose` | 公开服务的容器端口（当前仅发布一个 TCP 端口） |
| `environment` / `env_file` | 服务环境变量；公开服务可被控制台配置覆盖 |
| `command` / `entrypoint` | 服务启动命令与入口 |
| `mem_limit` / `cpus` / `deploy.resources.limits` | 每个服务的 CPU / 内存配额 |
| `depends_on` | 启动顺序，支持 `service_started` / `service_healthy` |
| `healthcheck` | 健康检查；`service_healthy` 依赖会等待其变为 healthy |
| `volumes` | 命名卷 / 匿名卷 / tmpfs；拒绝宿主 bind mount |

多服务至少需要一个带 `ports` 的公开服务，或通过顶层 `x-paas.public: <服务名>` 指定；
其余服务作为内部服务运行在应用独立网络中，不分配宿主端口、不渲染路由。
`x-paas.port` 可显式指定公开服务端口，优先于 `ports`。

以下字段会被拒绝：宿主 bind mount、`privileged`、`network_mode: host`、`pid/ipc/uts`、
`cap_add`、`devices`、`secrets`、`configs`、`profiles`、多副本 `deploy.replicas > 1` 等。
未生效字段会写入构建日志，便于排查"配置为什么没起作用"。

:::note[当前阶段]
多服务已支持构建、依赖启动与整体生命周期；平台指标、日志、Web 终端与一键诊断默认指向公开服务。
内部服务依靠 Docker `restart` 策略自愈，服务级观测将在后续阶段补齐。
:::

## 接口清单

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/api/apps/{id}/source` | 上传 zip 源码包 | 属主或管理员 |
| POST | `/api/apps/{id}/source/git` | 以 Git 仓库作为源码 | 属主或管理员 |
| POST | `/api/apps/{id}/source/demo` | 以内置示例包作为源码 | 属主或管理员 |
| POST | `/api/apps/{id}/build` | 构建镜像，返回端口识别提示 | 属主或管理员 |
| GET | `/api/apps/{id}/build/logs` | 查询构建日志 | 属主或管理员 |

## 构建与端口推断

构建引擎按自带 Dockerfile、Node、Python、静态站的优先级识别项目类型，并生成对应 Dockerfile。

容器端口按以下优先级推断：

1. `paas.json` 中的 `port` 字段；
2. Dockerfile 的 `EXPOSE` 指令；
3. 项目类型的默认端口。

Python 与静态站不覆盖用户填写值。推断结果写入构建日志首行，并通过 `port_notice` 字段返回。

## 相关页面

- 返回[接口总览](/reference/api/)
- 构建失败排查：[验收与故障排查](/deploy/verify/)
- 部署与路由：[部署与运行观测](/reference/api/runtime/)
