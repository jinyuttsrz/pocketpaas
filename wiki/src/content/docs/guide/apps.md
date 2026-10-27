---
title: 应用交付全流程
description: 从创建应用到对外访问的完整链路：源码接入、类型识别、构建、部署与配置调整。
---

这一页按操作顺序说明平台交付一个应用的完整流程。

## 交付链路

```text
创建应用 → 引入源码 → 项目类型识别 → 生成 Dockerfile → 构建镜像
        → 分配端口 → 启动容器 → 渲染 Nginx 路由 → 域名 / 端口访问
```

## 1. 创建应用

在「应用实例」页新建应用，需要填写：

- **应用名**：用于派生全局唯一的子域名，创建后不建议修改；
- **资源配额**：CPU 与内存，可先取默认值（`DEFAULT_MEM_LIMIT=256m`、`DEFAULT_CPU_QUOTA=1.0`）；
- **端口**：容器内监听端口，平台会将其映射到宿主的端口区间（`20000~29999`）。

:::caution[多租户可见性]
普通用户只能看到并操作自己创建的应用；管理员可查看与管理全部应用。
:::

## 2. 引入源码

平台支持三种来源：

| 来源 | 说明 |
|---|---|
| 上传 zip | 源码压缩包，上传后解压到 `workspace/{app_id}/src` |
| Git 仓库 | 填写仓库地址，由平台 `git clone`；私有网段地址会被 SSRF 防护拦截 |
| 内置示例包 | 「模板与指南」页从 `demo/` 选择，适合快速体验 |

源码包大小上限由 `MAX_UPLOAD_MB`（默认 `100`）控制。

:::note[Docker Compose]
源码根目录包含 `docker-compose.yml` / `compose.yaml` 时，平台会优先按 compose 解析，
既支持单服务，也支持 `web + db` 这类多服务项目：

- 所有带 `ports` 的服务默认都会公开；也可以用顶层 `x-paas.public: [web, api]` 显式指定公开集合，
  用 `x-paas.primary` 指定主入口（缺省取公开集合第一个）；
- 主入口沿用应用域名 `{应用名}.{BASE_DOMAIN}`，其他公开服务使用
  `{应用名}-{服务名}.{BASE_DOMAIN}`；每个公开服务独立分配宿主端口；
- 按 `depends_on` 拓扑顺序启动，支持 `healthcheck` / `service_healthy` 条件；
- 支持 `image`、`build`、`environment`、`env_file`、`command`、`entrypoint`、命名卷、资源限制与容器加固；
- 拒绝宿主 bind mount、`privileged`、`network_mode: host`、`cap_add`、`secrets` 等突破隔离的字段。
:::

部署完成后，详情抽屉顶部会出现**服务选择器**。选中某个服务后，负载曲线、实时日志、Web 终端、
容器文件与一键诊断都会切换到该服务；单容器应用不显示选择器。多服务应用也可在「概览」的
服务标签上单独重启某个服务，用于排障时只重启数据库或缓存，而不影响公开服务。

### 多公开服务与蓝绿发布

多服务 compose 的每次部署会创建一套新的**版本槽位**（`v{部署ID}`）容器：

1. 新槽位在独立网络内启动，旧槽位继续提供服务；
2. 全部服务启动成功且路由切换成功后，流量切到新槽位；
3. 旧槽位容器停止但保留，可在详情抽屉点「回滚上一版本」一键切回；
4. 再下一次成功部署才会清理更早的槽位，因此始终保留最近两个版本。

命名卷由新旧槽位共享，数据库等有状态服务在切换窗口内可能被新旧实例同时挂载；对数据一致性要求高的
项目，建议在低峰期发布或先停止写入。单容器应用仍使用原有的重建部署方式。

## 3. 项目类型识别与 Dockerfile 生成

平台按源码特征识别四类项目并生成对应 Dockerfile：

| 类型 | 识别特征 | 默认端口 |
|---|---|---|
| `compose` | 根目录存在 `docker-compose.yml` / `compose.yaml` | 取 compose 的 `ports` / `expose` |
| `python` | 存在 `main.py` / `requirements.txt` 等 | 沿用用户填写值 |
| `static` | 纯静态 HTML 站点 | 沿用用户填写值 |
| `node` | 存在 `package.json` / `server.js` | `3000` |
| `custom` | 源码自带 `Dockerfile` | 缺省 `8000` |

容器端口推断优先级（从高到低）：

1. 单服务 compose 中的 `x-paas.port`（显式覆盖），其次是 `ports` / `expose`；
2. 源码包根目录 `paas.json` / `.paas.json` 中的 `port` 字段（显式声明）；
3. 自带 Dockerfile 中的 `EXPOSE` 指令（取第一个合法端口，兼容 `7070/tcp` 形式）；
4. 项目类型默认端口（Node 取 3000，自带 Dockerfile 的 custom 缺省取 8000）；
5. Python / 静态站类型不覆盖，沿用用户填写值。

:::danger[危险指令会被拦截]
构建前会扫描 Dockerfile 中的高危指令（`BLOCK_DANGEROUS_DOCKERFILE=true`），命中即拒绝构建。
:::

## 4. 构建镜像

点「构建」后平台执行镜像构建，构建输出**分行落库**，可在详情抽屉的「构建日志」tab 查看最近一次构建结果。

相关限制：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `BUILD_TIMEOUT` | `900` | 构建超时（秒） |
| `GIT_CLONE_TIMEOUT` | `300` | `git clone` 超时（秒） |
| `IMAGE_PREFIX` | `paas` | 镜像与容器名前缀 |

## 5. 部署

部署会分配宿主端口、启动容器并同步路由：

- 端口从 `PORT_RANGE_START~PORT_RANGE_END` 区间分配，冲突时按 `MAX_PORT_RETRY` 重试；
- 容器加固默认开启：`no-new-privileges`、`cap_drop=ALL`、`pids_limit`（默认 256）、内存硬限制；
- 网络隔离默认开启：应用容器接入专用网桥（默认 `paas-net`），**容器间互通被关闭**；
- 路由由 `route_service` 渲染、`nginx -t` 校验后 `nginx -s reload` 热载。

部署失败会做残留清理并标记状态，不会留下半启动的容器。

## 6. 访问应用

| 模式 | 访问方式 |
|---|---|
| `local` | `http://127.0.0.1:{宿主端口}` 或 `http://localhost:{宿主端口}` |
| `prod` | `http://{应用名}.{BASE_DOMAIN}`（端口由外层 nginx 承担） |

控制台应用列表展示的访问地址由 `NGINX_PUBLIC_PORT` 决定，线上形如
`http://demo.beta.jinyuttsrz.top`（不带 `:8080`）。

## 7. 调整运行时配置

### 环境变量与启动命令

在详情抽屉「概览」tab 可视化编辑：

- 环境变量：每行一条 `KEY=VALUE`，可留空；
- 启动命令：单行输入，使用等宽字体展示。

:::caution[改动只写库，需重新部署]
配置修改只写入数据库，**必须重新部署**应用后才会注入到新容器并生效。
:::

### CPU / 内存配额

在配额入口调整，提交前平台会**对照宿主真实资源做预校验**，超出可用资源会被拒绝。

## 8. 生命周期操作

| 操作 | 效果 |
|---|---|
| 停止 | 停止容器，保留镜像与源码 |
| 重启 | 重启应用内全部服务容器 |
| 重启单个服务 | 只重启选中的服务容器（多容器 compose） |
| 回滚上一版本 | 切回上一个蓝绿槽位的容器与端口（多容器 compose） |
| 销毁 | 删除应用及其容器、镜像与源码记录 |

正常清理请使用控制台的停止 / 销毁，让数据库记录与容器状态保持一致。

## 9. 内置示例包怎么用

「模板与指南」页提供 5 个示例：

| 示例 | 类型 | 体验点 |
|---|---|---|
| `static-demo` | static | 最简静态站交付 |
| `python-demo` | python | Python 项目识别与依赖安装 |
| `node-demo` | node | Node 项目识别与端口推断 |
| `custom-demo` | custom | 自带 Dockerfile 的自定义构建路径 |
| `compose-demo` | compose | 多公开服务、内部 DNS、健康检查、命名卷与蓝绿回滚 |

一键创建后即可依次验证构建、部署、日志、诊断与文件管理。

## 相关页面

- 接口清单：[应用管理](/reference/api/apps/)与[源码与构建](/reference/api/source-build/)
- 文件与终端：[项目文件与终端](/guide/files-terminal/)
- 日志、探针与告警：[监控、告警与审计](/guide/observability/)
- 自动部署：[Git Webhook 自动部署](/guide/webhook/)
