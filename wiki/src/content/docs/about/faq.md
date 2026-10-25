---
title: 常见问题
description: 登录、部署、路由、示例包、终端与前端缓存相关的常见问题与处理方法。
---

## 登录与账号

### 默认口令 `admin / admin12345` 登不进？

默认管理员**仅在数据库中不存在任何用户时**才会自动创建（`AUTH_BOOTSTRAP_ADMIN=true`）。
若 `paas.db` 已存在且有用户，请使用当时设置的口令。

忘记口令可由管理员在「用户管理」页重置；本机开发也可以按[本机运行](/start/local/#重置数据库回到初始状态)的方式重置数据库。
登录页还需要填写图形验证码，验证码 120 秒过期，过期后刷新页面重新获取。

### 注册入口在哪？

默认允许自助注册（`AUTH_ALLOW_REGISTER=true`）。对外部署时建议关闭，
改为由管理员在「用户管理」页创建账号。

## 应用交付

### 改了环境变量，为什么没生效？

环境变量与启动命令的可视化编辑（`PATCH /api/apps/{app_id}/config`）**只写入数据库**，
必须**重新部署**应用后才会注入到新容器。

### 部署应用失败怎么办？

1. 先看 `/api/ready` 返回的 docker 状态，确认 Docker 守护进程可用；
2. 再看详情抽屉的「构建日志」是否有镜像构建错误；
3. 在详情抽屉「诊断」tab 跑一次一键诊断，按 `fail` / `warn` 项定位；
4. 最后看「实时日志」是否有容器启动即退出的报错。

### 端口为什么和我填的不一样？

用户填的是**容器内监听端口**；对外访问使用的是平台从 `20000~29999` 区间分配的**宿主端口**。
两级端口不同，线上通过域名访问时用户感知不到宿主端口。

## 示例包与模板

### 「模板与指南」页示例包下拉显示「（未找到示例包）」？

按以下顺序排查：

| 检查 | 处理 |
|---|---|
| `demo/` 目录是否存在、是否含 4 个 zip | 本地确认 `F:\test\demo`；服务器确认 `/opt/pocketpaas/demo` |
| compose 是否保留 `./demo:/demo:ro` | 补上该挂载 |
| `.env.prod` 的 `DEMO_DIR` 是否为 `/demo` | 改为 `/demo` |
| 容器内是否可见 | `docker exec pocketpaas ls /demo` |

任意改动后执行 `docker compose up -d --build` 重建容器。

:::note[根因]
平台镜像只 `COPY app`，**不含 demo 目录**；`DEMO_DIR` 的默认值 `../demo` 在容器内 `WORKDIR=/app` 下解析为 `/demo`，
完全依赖挂载。
:::

## 路由与域名

### 应用打不开，但容器是运行中的？

按[验收与故障排查](/deploy/verify/)的六层链路逐层 `curl`，第一个非 200 的环节就是断点。
常见原因：内层 nginx 未 reload、`BASE_DOMAIN` 填错、DNS 未解析、防火墙未放行。

### 改了域名后所有应用都打不开？

旧 `paas-data/nginx/paas-*.conf` 里仍是旧域名：

```bash
rm -f paas-data/nginx/paas-*.conf
docker compose up -d --force-recreate
# 然后在控制台对每个应用点「重新部署」
```

### 宝塔的反向代理列表里看不到平台配置？

平台用的是「裸配置文件」方式放在宝塔 vhost 目录，UI 列表里看不到是正常的。
判断依据是：

```bash
/www/server/nginx/sbin/nginx -T 2>/dev/null | grep 'server_name' | grep jinyuttsrz
```

## 终端与文件

### 容器 Web 终端连不上？

1. 目标应用容器必须处于 `running`；
2. 链路中的 nginx 需配置 WebSocket 升级头；
3. **全局同一时间只允许一个容器会话**，先关闭另一个入口（详情抽屉 / 项目文件管理页）的终端再试。

### 文件编辑提示超出上限？

文本在线编辑上限为 `FILE_MAX_READ_KB`（默认 512 KB），超出只能下载后本地修改。
上传单文件上限 `FILE_MAX_UPLOAD_MB`（默认 50 MB），整目录打包下载上限 `FILE_MAX_ARCHIVE_MB`（默认 200 MB）。

## 前端与界面

### 改了前端代码看不到效果？

前端静态资源**没有缓存指纹**，浏览器可能仍使用旧缓存。请**硬刷新**（`Ctrl+F5` / `Ctrl+Shift+R`）。

### 页面上的数据像旧的？

同样是静态资源缓存问题；硬刷新即可。若仍不生效，确认访问的是正确的后端实例端口。

## 本地开发

### `Cannot connect to the Docker daemon`

Docker Desktop 未启动或未就绪。启动后等 30~90 秒，再用 `wsl -d Ubuntu -- docker version` 确认。

### `ModuleNotFoundError: No module named 'app'`

不在 `backend` 目录启动。切到 `/mnt/f/test/backend` 再运行。

### WSL 里 `pip` 报路径不存在

venv 搬家导致脚本 shebang 失效。改用 `python -m pip`，或重建 venv（见[本机运行](/start/local/)）。

### 8000 端口被占用

```bash
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

或先停掉占用 8000 的旧进程。
