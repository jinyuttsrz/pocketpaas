---
title: 本站（Wiki）部署
description: 把 Astro 构建出的静态文档站发布到主站 www 的子路径，服务器无需安装 Node。
---

本站由 **Astro + Starlight + Tailwind CSS** 构建，产物是纯静态文件（`wiki/dist/`）。
服务器只需要 nginx，**不需要安装 Node**：构建在本地完成，上传的是构建结果。

当前部署地址：`https://www.jinyuttsrz.top/project/pocketpaas/`。

:::note[没有引入分析/广告]
按项目要求，本站**不接入** Google Analytics、Google Tag Manager、Microsoft Advertising 等
分析与广告服务，也没有任何外部 CDN 依赖。字体使用系统字体栈，离线与内网环境同样可用。
:::

## 为什么必须配置 `base`

部署在子路径下时，构建产物里的资源如果仍写成 `/_astro/...`，浏览器会去
`https://www.jinyuttsrz.top/_astro/...` 请求，结果全部 404，表现为「页面能打开但没有样式、脚本报错」。

因此 `wiki/astro.config.mjs` 中固定为：

```js
const base = '/project/pocketpaas';

export default defineConfig({
  site: 'https://www.jinyuttsrz.top',
  base,
  // ...
});
```

配置后：

- 资源、favicon、sitemap、侧边栏、分页、站内搜索索引会自动带上 `/project/pocketpaas` 前缀；
- 正文 Markdown 里的 `[文字](/start/quickstart/)` 由 `src/plugins/remark-base-links.mjs` 统一补前缀；
- 首页 Hero 按钮的 `link` 是 frontmatter 字段，需要手工写成带前缀的路径。

:::caution[换部署路径要一起改]
若以后改到独立子域（如 `docs.jinyuttsrz.top`），要把 `base` 改为 `'/'`、
首页 Hero 链接去掉前缀，并重新构建，否则资源路径会对不上。
:::

## 一、本地构建

要求 Node.js **>= 22.23.4**（本机验证版本 24.16）。

```bash
cd F:\test\wiki
npm install          # 首次执行
npm run build        # 产物输出到 dist/
```

构建成功的标志：

```text
[starlight:pagefind] Finished building search index
[build] Complete!
```

自检（可选）：确认首页资源已带前缀。

```powershell
Select-String -Path wiki\dist\index.html -Pattern '/project/pocketpaas/_astro/' | Select-Object -First 1
```

产物结构：

```text
wiki/dist/
├── index.html
├── 404.html
├── start/ deploy/ guide/ reference/ about/
├── _astro/           # CSS / JS 资源
└── pagefind/         # 站内搜索索引（构建期生成，纯静态）
```

本地预览：

```bash
npm run preview      # http://localhost:4321/project/pocketpaas/
```

## 二、上传到服务器

站点的物理根目录取决于宝塔中 `www.jinyuttsrz.top` 的配置，常见为
`/www/wwwroot/www.jinyuttsrz.top`。确认方式：

```bash
grep -n 'root ' /www/server/panel/vhost/nginx/www.jinyuttsrz.top.conf
```

假设根目录是 `/www/wwwroot/www.jinyuttsrz.top`，把产物放到其下的子目录：

```bash
# 本地项目根目录执行
ssh root@<服务器IP> "mkdir -p /www/wwwroot/www.jinyuttsrz.top/project/pocketpaas"
scp -r wiki/dist/* root@<服务器IP>:/www/wwwroot/www.jinyuttsrz.top/project/pocketpaas/
```

宝塔文件管理方式：把 `dist` 压成 zip → 进入
`/www/wwwroot/www.jinyuttsrz.top/project/pocketpaas/` → 上传并解压 →
确认该目录下**直接**是 `index.html`，而不是又套了一层 `dist/`。

## 三、nginx 侧

不需要新建站点，也**不需要改主站配置**：文件位于主站根目录的子目录内，
nginx 默认的 `location / { try_files $uri $uri/ =404; }` 会自然命中。

只需要确认两点：

```nginx
server {
    listen 443 ssl;
    server_name www.jinyuttsrz.top;

    root /www/wwwroot/www.jinyuttsrz.top;
    index index.html;

    location / {
        try_files $uri $uri/ =404;
    }

    error_page 404 /404.html;
}
```

- 主站 `root` 与上传路径一致；
- `location /` 是 `try_files`，没有把 `/project/` 反代到平台 8000。

:::danger[不要反向代理]
文档站是纯静态目录，若把 `/project/pocketpaas/` 配成 `proxy_pass http://127.0.0.1:8000`，
会把文档请求打到 PocketPaaS 控制台，页面会完全变样。
:::

## 四、DNS 与端口

复用主站 `www.jinyuttsrz.top`，**无需新增解析记录**，也无需额外放行端口。

## 五、验收

```bash
# 页面本身
curl -s -o /dev/null -w '%{http_code}\n' https://www.jinyuttsrz.top/project/pocketpaas/
# 期望 200

# 资源是否带前缀且可访问（从 HTML 里取一个 CSS 路径）
curl -s https://www.jinyuttsrz.top/project/pocketpaas/ | grep -o '/project/pocketpaas/_astro/[^"]*\.css' | head -1
curl -s -o /dev/null -w '%{http_code}\n' https://www.jinyuttsrz.top/project/pocketpaas/_astro/<实际文件名>.css
# 期望 200
```

浏览器检查清单：

1. 打开 `https://www.jinyuttsrz.top/project/pocketpaas/`，样式、字体、暗色模式正常；
2. 点左侧导航任意页面，URL 始终带 `/project/pocketpaas/`；
3. 正文里的链接、上一页/下一页都能跳转；
4. 搜索「部署」「Webhook」，结果能正常跳转；
5. 直接刷新深层页面（如 `/project/pocketpaas/guide/webhook/`）不 404。

:::tip[页面异常时先看控制台]
打开开发者工具，若看到大量 `/_astro/...` 的 404，说明构建时没有带上 `base`，
或上传的是旧产物；重新构建并覆盖上传即可。
:::

## 六、更新流程

```bash
# 1. 本地重新构建
cd F:\test\wiki
npm run build

# 2. 清理旧产物（避免残留已删除页面的 HTML）
ssh root@<服务器IP> "rm -rf /www/wwwroot/www.jinyuttsrz.top/project/pocketpaas/*"

# 3. 上传新产物
scp -r wiki/dist/* root@<服务器IP>:/www/wwwroot/www.jinyuttsrz.top/project/pocketpaas/
```

不需要重载 nginx，也不需要动 PocketPaaS 容器。

## 七、与主站的关系

| 项 | 说明 |
|---|---|
| 域名 | 复用 `www.jinyuttsrz.top`，不占用 `*.beta` 泛解析 |
| 路径 | `/project/pocketpaas/`，由 `base` 决定 |
| 证书 | 与主站共用同一张 `www.jinyuttsrz.top` 证书 |
| 依赖 | 无。文档站不读取平台数据库，也不与 `docker.sock` 交互 |
| 故障影响 | 文档站不可用不影响 PocketPaaS 控制台与应用路由 |
