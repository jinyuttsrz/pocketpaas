---
title: 服务器部署
description: Ubuntu + 宝塔面板 + Docker Compose 的完整上线步骤，含端口规划、配置与启动顺序。
---

适用环境：Ubuntu 22.04 / 24.04 + 宝塔面板，使用 Docker Compose 部署。
本文示例配置为：控制台 `app.jinyuttsrz.top`，应用泛域名 `*.beta.jinyuttsrz.top`。

## 架构总览

平台在宿主机上管理应用容器，这要求「容器内的 `127.0.0.1` 必须等于宿主机」，
因此平台容器使用 `network_mode: host`；而宿主已有的宝塔 nginx 会占用 80 端口，
所以采用**双层 Nginx**：

```text
浏览器 ──:80──▶ 宝塔 nginx ──:8080──▶ 平台内层 nginx ─┬─ 127.0.0.1:8000   平台控制台
                                                      └─ 127.0.0.1:2000x  应用容器
```

平台容器（`network_mode: host`）内同时运行：

- 平台本体 uvicorn，监听 `:8000`
- 内层 nginx，监听 `:8080`，按应用动态生成 server 块

### 端口占用

| 端口 | 归属 | 是否对公网开放 |
|---|---|---|
| 80 | 宝塔 nginx（外层） | 必须开放 |
| 8080 | 平台内层 nginx | **不要开放**（仅内网跳板） |
| 8000 | 平台本体 uvicorn | 不要开放 |
| 20000-29999 | 应用容器发布到宿主 | 不要开放（nginx 走回环） |

:::caution[部署前先确认端口空闲]
确认宿主机上没有其他进程占用 8080。例如内网穿透工具 `nps` 常占用 8081，宝塔面板本身占用 8888，都要避开。
:::

### 域名规划

| 用途 | 域名 | `server_name` 写法 |
|---|---|---|
| 控制台 | `app.jinyuttsrz.top` | `app.jinyuttsrz.top` |
| 应用 | `{应用名}.beta.jinyuttsrz.top` | `*.beta.jinyuttsrz.top` |

对应 `BASE_DOMAIN=beta.jinyuttsrz.top`。

:::danger[BASE_DOMAIN 只能填泛域名去掉 * 的部分]
若误填成 `app.beta.jinyuttsrz.top`，应用域名会拼成 `xxx.app.beta.jinyuttsrz.top`，
而泛解析 `*.beta.jinyuttsrz.top` 只匹配一层，覆盖不到二级子域，所有应用都会打不开。
:::

## 前置条件

- 一台 Ubuntu 服务器，root 权限
- 已装好宝塔面板（其 nginx 占用 80）
- 一个已备案 / 可解析的域名，且 DNS 服务商支持**泛解析**

## 部署步骤

### 1. 安装 Docker

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker version
```

### 2. 上传代码

```bash
mkdir -p /opt/pocketpaas
# 从本地把项目目录内容上传到 /opt/pocketpaas
scp -r ./设计/* root@<服务器IP>:/opt/pocketpaas/
cd /opt/pocketpaas
ls    # 应看到 docker-compose.yml、backend/、deploy/、demo/
```

:::caution[`demo/` 必须一并上传]
平台镜像里**不含**示例包，控制台「模板与指南」页完全依赖宿主目录 `demo/` 通过 `./demo:/demo:ro` 挂载。
漏传就会导致示例包下拉显示「（未找到示例包）」。
:::

与部署无关的产物不必上传：`backend(10xxxxx).zip` 之类打包残留、`.idea/`、本地构建工作区 `backend/workspace/`。

### 3. 配置 `.env.prod`

```bash
cp backend/.env.prod.example backend/.env.prod
vi backend/.env.prod
```

线上模板已给出与本环境匹配的取值，逐项确认即可。

**① 域名与路由**

| 配置项 | 本环境取值 | 默认值 | 说明 |
|---|---|---|---|
| `BASE_DOMAIN` | `beta.jinyuttsrz.top` | 空 | 泛域名去掉 `*` |
| `ENABLE_NGINX` | `true` | `false` | 开启动态路由 |
| `USE_HTTPS` | `false` | `false` | 先跑通 HTTP |
| `NGINX_LISTEN_PORT` | `8080` | `80` | 内层 nginx 监听端口（宝塔占 80 时的取值） |
| `NGINX_PUBLIC_PORT` | `80` | `80` | 用户实际访问的端口 |
| `NGINX_CONF_DIR` | `/etc/nginx/conf.d` | 同 | 动态路由落盘目录 |

:::note[两个端口不是一回事]
`NGINX_LISTEN_PORT` 是平台内层 nginx 监听的口，`NGINX_PUBLIC_PORT` 是外层 nginx 对外的口。
两层部署时前者 8080、后者 80，所以控制台展示的应用地址不会带上 `:8080`。
:::

**② 数据持久化（必须与 compose 挂载一一对应）**

| 配置项 | 本环境取值 | 默认值 | 说明 |
|---|---|---|---|
| `DB_URL` | `sqlite:////data/paas.db` | `sqlite:///./paas.db` | 数据库文件，落在 `./paas-data/data:/data` 挂载内 |
| `WORKSPACE_DIR` | `/data/workspace` | `./workspace` | 源码解压与构建工作区 |
| `DEMO_DIR` | `/demo` | `../demo` | 内置示例包目录，对应 `./demo:/demo:ro` |

这三项**必须与 compose 的挂载对齐**：`/data` 之外的路径都写在容器可写层里，
容器一重建（`docker compose up -d --build`）数据库记录、应用源码就会全部丢失。

**③ Docker 与镜像构建**

| 配置项 | 本环境取值 | 默认值 | 说明 |
|---|---|---|---|
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | 同 | 对应 `docker.sock` 挂载 |
| `DEFAULT_MEM_LIMIT` / `DEFAULT_CPU_QUOTA` | `256m` / `1.0` | 同 | 新建应用默认配额 |
| `PORT_RANGE_START` / `PORT_RANGE_END` | `20000` / `29999` | 同 | 应用端口区间（不要放行公网） |
| `MAX_PORT_RETRY` | — | `5` | 端口分配冲突时的重试次数 |

**④ 登录鉴权（务必改口令）**

| 配置项 | 本环境取值 | 默认值 | 说明 |
|---|---|---|---|
| `ENABLE_AUTH` | `true` | `true` | 登录鉴权总开关 |
| `AUTH_ALLOW_REGISTER` | `true` | `true` | 是否允许自助注册（对外可关） |
| `AUTH_ENABLE_CAPTCHA` | `true` | `true` | 登录/注册图形验证码 |
| `AUTH_BOOTSTRAP_ADMIN` | `true` | `true` | 首次启动自动创建管理员 |
| `AUTH_DEFAULT_PASSWORD` | **改成强密码** | `admin12345` | 引导管理员口令 |

其余加固、阈值与文件上限类配置保持默认即可，完整清单见[配置项](/reference/config/)。

### 4. 容器挂载与持久化

compose 里 5 条 `volumes` 各有明确用途：

| 挂载 | 作用 |
|---|---|
| `/var/run/docker.sock:/var/run/docker.sock` | 把宿主 Docker 交给平台，平台才能创建 / 构建 / 管理应用容器 |
| `./paas-data/data:/data` | 持久化数据库与构建工作区 |
| `./paas-data/nginx:/etc/nginx/conf.d` | 持久化平台按应用动态生成的 server 块 |
| `./deploy/console.conf:/etc/nginx/conf.d/console.conf:ro` | 只读挂载控制台域名路由 |
| `./demo:/demo:ro` | 只读挂载内置示例代码包 |

验证容器内确实可见示例包：

```bash
docker exec pocketpaas ls /demo
# 应列出 static-demo.zip  python-demo.zip  node-demo.zip  custom-demo.zip
```

### 5. 检查端口占用

```bash
ss -lntp | grep -E ':80 |:8080 '
```

- `:80` 有输出是**正常的**（宝塔 nginx）
- `:8080` **必须为空**，被占用就改 `NGINX_LISTEN_PORT`（同时改 `deploy/console.conf` 的 `listen`）

### 6. 构建并启动

```bash
cd /opt/pocketpaas
docker compose up -d --build
docker compose logs -f
```

看到 `Application startup complete` 即启动成功。日志中**不应出现**：

```text
nginx: [emerg] bind() to 0.0.0.0:80 failed (98: Address already in use)
```

:::caution[重建与数据安全]
`docker compose up -d --build` 会同时重建镜像并应用 `volumes` / `env_file` 的变更。
只改了挂载或 `.env.prod` 也必须执行它（compose 会自动重建容器），无需手工 `docker rm`。
**不要**用 `docker compose down -v`：`-v` 会连卷一起删除，数据会丢。
:::

### 7. 配置宝塔侧外层反代

把 `deploy/baota-front.conf` 复制到宝塔的 vhost 目录：

```bash
cp deploy/baota-front.conf /www/server/panel/vhost/nginx/pocketpaas-front.conf
/www/server/nginx/sbin/nginx -t && /www/server/nginx/sbin/nginx -s reload
```

验证配置确实被加载（这一步不能跳过）：

```bash
/www/server/nginx/sbin/nginx -T 2>/dev/null | grep 'server_name' | grep jinyuttsrz
```

必须看到：

```text
server_name *.beta.jinyuttsrz.top app.jinyuttsrz.top;
```

:::tip[宝塔 UI 看不到这条规则是正常的]
这里用的是「裸配置文件」方式，只要 `nginx -T` 的输出里有这条 `server_name` 就说明生效。
:::

### 8. 配置 DNS

主机记录一般填相对主域名的部分：

| 类型 | 主机记录 | 记录值 |
|---|---|---|
| A | `app` | 服务器 IP |
| A | `*.beta` | 服务器 IP |

:::danger[不要填成完整域名]
填成 `*.beta.jinyuttsrz.top` 会变成 `xxx.beta.jinyuttsrz.top.jinyuttsrz.top`，永远解析不到。
:::

### 9. 放行端口

两个地方都要放行 80，缺一不可：

1. 宝塔面板 → 安全 → 放行 `80/tcp`
2. 云服务商控制台 → 安全组 → 入方向放行 `80/tcp`

**不要**放行 8080 / 8000 / 20000-29999。

## 下一步

- 域名、双层 Nginx 与 HTTPS 规划：[域名与双层 Nginx](/deploy/nginx/)
- 上线后逐层验收：[验收与故障排查](/deploy/verify/)
