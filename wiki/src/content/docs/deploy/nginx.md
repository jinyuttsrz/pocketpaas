---
title: 域名与双层 Nginx
description: 理解并维护 PocketPaaS 的内外层 Nginx 分工、端口映射与域名规划。
---

PocketPaaS 本身不需要手工写应用路由，但**控制台自身的域名**需要一份手工配置。
这一页说明内外两层 Nginx 各自负责什么，以及换域名时该改哪里。

## 两层分工

| 层 | 配置文件 | 监听 | 负责 |
|---|---|---|---|
| 外层（宝塔 nginx） | `deploy/baota-front.conf` | 宿主 `:80` | 接收所有公网请求，把 `*.beta.域名` 与控制台域名整体转给内层 |
| 内层（平台 nginx） | `deploy/console.conf` + 平台自动生成的 `paas-{id}.conf` | 容器 `:8080` | 按 `Host` 分流：控制台 → `127.0.0.1:8000`，应用 → `127.0.0.1:2000x` |

```text
浏览器
  │  Host: app.jinyuttsrz.top
  ▼
宝塔 nginx :80 ──透传 Host──▶ 平台内层 nginx :8080
                                   ├─ server_name app.jinyuttsrz.top → proxy_pass 127.0.0.1:8000
                                   └─ server_name demo.beta.jinyuttsrz.top → proxy_pass 127.0.0.1:2000x
```

:::note[为什么外层必须透传 Host]
内层 nginx 靠 `Host` 区分控制台与各个应用。若外层没有 `proxy_set_header Host $host;`，
所有请求都会落到默认 server 块，表现为「打开的是宝塔默认页或 404」。
:::

## 外层配置（`deploy/baota-front.conf`）

关键内容：

```nginx
server {
    listen 80;
    server_name *.beta.jinyuttsrz.top app.jinyuttsrz.top;

    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket 升级：实时日志与容器终端依赖这两行
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

其中：

- `client_max_body_size 100m`：源码 zip 上传需要放宽请求体上限。
- `Upgrade` / `Connection`：控制台的实时日志流（`/ws/...`）与容器终端依赖 WebSocket 升级。
- `proxy_read_timeout 300s`：容器终端长时间无输出时不被提前断开。

## 内层控制台配置（`deploy/console.conf`）

```nginx
server {
    listen 8080;
    server_name app.jinyuttsrz.top;

    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

这份文件由 `docker-compose.yml` 以**只读**方式挂载进容器的 `/etc/nginx/conf.d/`。
换控制台域名时改这里的 `server_name`，然后：

```bash
docker compose restart pocketpaas
```

:::caution[server_name 不能与应用子域名重复]
应用域名形如 `{应用名}.{BASE_DOMAIN}`。若控制台域名与应用子域名相同，
nginx 会出现同名 server 块，其中一个静默失效。
:::

## 应用路由是自动生成的

平台只会为「应用」自动生成路由（`route_service.sync_route`）：

1. 渲染 server 块 → 写入 `/etc/nginx/conf.d/paas-{id}.conf`（宿主侧即 `./paas-data/nginx/`）
2. `nginx -t` 语法校验
3. `nginx -s reload` 热载

:::danger[不要手工编辑 `paas-data/nginx/paas-*.conf`]
这些文件由程序按数据库状态重新生成，手工修改会被覆盖，且可能导致 `nginx -t` 失败。
:::

## 域名规划与 DNS

| 用途 | 域名 | 主机记录 | 记录类型 |
|---|---|---|---|
| 控制台 | `app.jinyuttsrz.top` | `app` | A → 服务器 IP |
| 应用 | `{应用名}.beta.jinyuttsrz.top` | `*.beta` | A → 服务器 IP |
| 文档站（可选） | `www.jinyuttsrz.top/project/pocketpaas/` | 复用 `www` | 无需新增 |

文档站当前部署在主站 `www` 的子路径下，复用已有解析与证书；
若改为独立子域（如 `docs.jinyuttsrz.top`），需同时把 Astro 的 `base` 改回 `/` 并重新构建。

## 改域名时要做的事

1. 改 `backend/.env.prod` 的 `BASE_DOMAIN`、`NGINX_LISTEN_PORT`（如端口变化）；
2. 改 `deploy/console.conf` 的 `server_name`；
3. 改 `deploy/baota-front.conf` 的 `server_name`，重新放到宝塔 vhost 目录并 reload；
4. **清掉旧的应用路由并重新部署应用**：

```bash
rm -f paas-data/nginx/paas-*.conf
docker compose up -d --force-recreate
# 然后在控制台对每个应用点「重新部署」
```

旧 `paas-*.conf` 里仍写着旧域名，不清理会导致新域名访问不到。

## 控制台 HTTPS（证书部署）

TLS 在**外层宝塔 nginx 终止**：平台内层 nginx 与平台本体继续走 HTTP，
`backend/.env.prod` 的 `USE_HTTPS` 保持 `false`，不需要重建平台容器。

`deploy/baota-front.conf` 已按三个 server 块组织：

| server 块 | 端口 | 行为 |
|---|---|---|
| `*.beta.jinyuttsrz.top` | 80 | 应用子域名，**继续走 HTTP** |
| `app.jinyuttsrz.top` | 80 | 301 跳转到 HTTPS（保留 ACME 校验路径） |
| `app.jinyuttsrz.top` | 443 | 控制台，`ssl_certificate` 指向证书文件 |

:::danger[应用子域名不要跳到 HTTPS]
单域名证书只覆盖 `app.jinyuttsrz.top`，不含 `*.beta`。
把应用块也 301 到 HTTPS 会导致浏览器证书不匹配。
只有申请了泛域名证书（`*.beta.jinyuttsrz.top`）后才能给应用启用 HTTPS。
:::

### 证书文件放置

| 服务商文件 | 服务器路径 |
|---|---|
| `*_bundle.crt` / `*_bundle.pem`（已含中间证书） | `/www/server/panel/vhost/cert/app.jinyuttsrz.top/fullchain.pem` |
| `*.key` | `/www/server/panel/vhost/cert/app.jinyuttsrz.top/privkey.pem` |
| `*.csr` | 不需要上传 |

```bash
mkdir -p /www/server/panel/vhost/cert/app.jinyuttsrz.top
# 上传后设置权限
chmod 644 /www/server/panel/vhost/cert/app.jinyuttsrz.top/fullchain.pem
chmod 600 /www/server/panel/vhost/cert/app.jinyuttsrz.top/privkey.pem
```

### 生效与放行

```bash
cp deploy/baota-front.conf /www/server/panel/vhost/nginx/pocketpaas-front.conf
/www/server/nginx/sbin/nginx -t && /www/server/nginx/sbin/nginx -s reload
```

宝塔 → 安全 与云服务商安全组都要放行 `443/tcp`。

### 验收

```bash
curl -sI http://app.jinyuttsrz.top/ | head -n 3          # 期望 301 → https
curl -sI https://app.jinyuttsrz.top/login | head -n 1    # 期望 200
openssl s_client -connect 127.0.0.1:443 -servername app.jinyuttsrz.top </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -dates
```

浏览器打开控制台，确认地址栏为锁形图标，登录、实时日志与容器终端（WebSocket 自动切到 `wss`）均正常。

:::note[证书续期与 HSTS]
当前证书为 90 天期单域名证书，到期前替换上述两个文件后 `nginx -s reload` 即可，无需改动平台。
HSTS 建议在 HTTPS 验收稳定后再启用，配置文件中已留出注释位。
自动 HTTPS（证书申请与续期自动化）仍不在当前交付范围内。
:::
