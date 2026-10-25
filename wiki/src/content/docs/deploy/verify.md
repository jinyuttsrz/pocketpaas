---
title: 验收与故障排查
description: 六层链路验收法、状态码含义、DNS 与防火墙排查，以及日常运维要点。
---

部署完成后不要只看「控制台能打开」，按链路逐层验收能把问题定位到具体环节。

## 控制台验收

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: app.jinyuttsrz.top' http://127.0.0.1/login
```

返回 `200` 即可。浏览器访问 `http://app.jinyuttsrz.top/login`。

## 应用验收：六层链路

把 `demo` 换成实际应用名，按顺序逐层执行：

```bash
# 第 1 层：平台是否生成了路由配置
grep -E 'listen|server_name|proxy_pass' paas-data/nginx/paas-*.conf

# 第 2 层：应用容器与宿主端口
docker ps --filter "name=paas-" --format '{{.Names}}  {{.Status}}'
ss -lntp | grep ':<宿主端口> '

# 第 3 层：直连应用容器（绕过所有 nginx）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<宿主端口>/

# 第 4 层：内层 nginx
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: demo.beta.jinyuttsrz.top' http://127.0.0.1:8080/

# 第 5 层：宝塔 nginx
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: demo.beta.jinyuttsrz.top' http://127.0.0.1/

# 第 6 层：真实域名
curl -s -o /dev/null -w '%{http_code}\n' http://demo.beta.jinyuttsrz.top/
```

第 1 层输出里 `listen` 必须是 **8080**。

## 分层定位法

从第 3 层往下，**第一个不是 200 的环节就是断点**：

| 断在 | 结论 | 处理 |
|---|---|---|
| 第 3 层 | 应用本身没起来，与 nginx 无关 | 查应用构建日志与容器日志 |
| 第 3 层通、第 4 层断 | 内层 nginx 路由未生效 | 查平台日志中的 nginx 报错；确认配置已 reload |
| 第 4 层通、第 5 层断 | 宝塔外层规则有问题 | 回到部署第 7 步验证 `nginx -T` |
| 第 5 层通、第 6 层断 | DNS 或防火墙 | 见下方「外网访问不了」 |

## 状态码含义

| 现象 | 含义 |
|---|---|
| `200` | 该层通过 |
| `502 Bad Gateway` | nginx 路由生效了，但上游应用容器没跑 / 端口不对 |
| 宝塔默认页或 `404` | Host 没匹配到任何 `server_name`，落到了默认站 |
| `Connection refused` | 该端口没有进程监听 |
| 挂起直到超时 | 被防火墙拦截 |

## 外网访问不了，但服务器本地正常

问题只可能在两处：

**① DNS 没解析** —— 浏览器提示「无法访问此网站 / `DNS_PROBE_FINISHED_NXDOMAIN`」

```bash
# 在你自己电脑上执行
nslookup demo.beta.jinyuttsrz.top 223.5.5.5
```

返回的 IP 必须是服务器 IP。

**② 防火墙没放行** —— 浏览器提示「连接超时 / `ERR_CONNECTION_TIMED_OUT`」

```powershell
# 在你自己电脑上执行（PowerShell）
Test-NetConnection demo.beta.jinyuttsrz.top -Port 80
```

需为 `TcpTestSucceeded : True`；为 False 就回到宝塔与云安全组放行 80。

## 常见部署问题

### 修改域名后应用打不开

旧 `paas-data/nginx/paas-*.conf` 里还写着旧域名：

```bash
rm -f paas-data/nginx/paas-*.conf
docker compose up -d --force-recreate
# 然后在控制台对每个应用点「重新部署」
```

### 内层 nginx 启动失败

```bash
docker logs pocketpaas 2>&1 | grep -i nginx
```

若报 `bind() to 0.0.0.0:80 failed`，说明有配置写死了 `listen 80`。
检查 `paas-data/nginx/*.conf` 是否残留旧配置。

### 示例包下拉为空

```bash
# ① 宿主机上示例包目录是否存在、是否含 zip
ls /opt/pocketpaas/demo/

# ② compose 是否挂载了 demo
grep -n './demo:/demo' /opt/pocketpaas/docker-compose.yml

# ③ .env.prod 的 DEMO_DIR 是否指向挂载点
grep -n 'DEMO_DIR' /opt/pocketpaas/backend/.env.prod

# ④ 容器内是否真的可见
docker exec pocketpaas ls /demo
```

| 检查结果 | 处理 |
|---|---|
| ① 目录不存在 / 没有 zip | 把本地 `demo/`（4 个 zip）上传到 `/opt/pocketpaas/demo/` |
| ② 无输出 | 在 compose 中补上 `./demo:/demo:ro` 挂载 |
| ③ 取值不是 `/demo` | 改为 `DEMO_DIR=/demo` |
| ④ 仍为空 | 挂载或目录没对上，回到 ①/② |

任意改动后都要执行 `docker compose up -d --build` 才会生效。

## 功能自检清单

登录控制台后做一次自检：

1. 「模板与指南」页示例包下拉能看到 4 个示例包：`static-demo`、`python-demo`、`node-demo`、`custom-demo`；
2. 任选一个一键创建应用，随后按入口逐项验证：

| 入口 | 可验证的能力 |
|---|---|
| 应用详情抽屉 → 诊断 | 一键诊断（只读体检清单） |
| 应用详情抽屉 → 终端 / 实时日志 | 容器 Web 终端、容器日志实时流 |
| 左侧导航 → 项目文件管理 | 源码与容器文件双通道浏览 / 编辑 / 上传 / 下载 |
| 左侧导航 → 告警中心 | 阈值告警列表与确认处置 |
| 左侧导航 → 审计日志 | 操作审计与安全追溯 |
| 左侧导航 → 用户管理 | 修改显示名、角色与启用状态（仅管理员） |

:::tip[页面没更新就硬刷新]
控制台前端为静态资源且无缓存指纹，请用 `Ctrl+F5`。
:::

## 日常运维

### 更新代码后重新部署

```bash
cd /opt/pocketpaas
docker compose up -d --build
```

只改了 compose 挂载或 `.env.prod` 时同样执行它。不要用 `down -v`。

### 常用观察命令

```bash
docker compose logs -f pocketpaas                          # 平台日志
docker exec pocketpaas tail -n 20 /var/log/nginx/access.log # 内层 nginx 访问日志
docker exec pocketpaas nginx -t                             # 内层 nginx 配置校验
```

### 数据与持久化

| 路径 | 内容 |
|---|---|
| `./paas-data/data/` | 数据库 `paas.db`、构建工作区 |
| `./paas-data/nginx/` | 平台自动生成的应用路由配置 |
| `./demo/` | 内置示例代码包（只读挂载到容器 `/demo`） |

:::danger[不要删数据目录，不要执行 down -v]
`paas-data/data/` 里是用户与所有应用记录。
:::

### 管理员口令

`AUTH_DEFAULT_PASSWORD` 仅在**首次启动、库中无用户**时生效。
若数据库中已存在管理员，改这个配置不会覆盖它；需要时在「用户管理」页重置口令。
