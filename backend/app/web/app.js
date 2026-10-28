/* PocketPaaS 控制台前端逻辑（零构建 · 原生 JS）
 * 数据来源：/api/apps*、/api/apps/{id}/metrics、/ws/apps/{id}/logs
 */

const API = "/api";
let apps = [];
let current = null;      // 当前详情应用
let currentService = null; // 详情抽屉当前选中的服务（多容器 compose）
let ws = null;           // 实时日志 WebSocket
let metricsTimer = null; // 负载曲线定时刷新
let listTimer = null;    // 列表自动刷新
let me = null;           // 当前登录用户（null 表示未开启鉴权）
let users = [];          // 管理员：用户列表
let ownerFilter = "";    // 管理员：按所属用户筛选（空=全部）

// 多视图状态
let activeView = "apps"; // apps | ai | routes | templates | files | nodes | alerts | audit | users
let readyInfo = null;    // /api/ready 缓存
let poolInfo = null;     // /api/system/resource-check 端口池与宿主资源缓存
let routeQuery = "";     // 路由表搜索词
let hostMetrics = null;  // /api/system/host-metrics 宿主机性能缓存
let hostTimer = null;    // 宿主机性能轮询定时器
let alertList = [];      // /api/alerts 告警列表缓存
let alertTimer = null;   // 告警轮询定时器
let auditLogs = [];      // /api/audit/logs 审计日志缓存
let auditActions = null; // /api/audit/actions 动作字典缓存
let auditQuery = "";     // 审计日志搜索词
let cfgEditing = false;  // 详情页正在编辑环境变量 / 启动命令（避免自动刷新回填覆盖输入）
let diagRunning = false; // 一键诊断进行中（避免重复触发）

const isAdmin = () => !!me && me.role === "admin";

/* ---------------- 基础工具 ---------------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function toast(msg, type = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast " + type;
  setTimeout(() => el.classList.add("hidden"), 2600);
}

async function copyText(text, label = "已复制到剪贴板") {
  try {
    await navigator.clipboard.writeText(text);
    toast(label, "ok");
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast(label, "ok");
  }
}

/* 未登录时集中跳转登录页（仅触发一次） */
let redirecting = false;
function gotoLogin() {
  if (redirecting) return;
  redirecting = true;
  if (listTimer) clearInterval(listTimer);
  if (metricsTimer) clearInterval(metricsTimer);
  stopHostPoll();
  stopAlertPoll();
  stopWs();
  location.href = "/login";
}

/* 把非 2xx 响应转成可读错误：
   优先取 JSON 里的 detail，其次剥掉 nginx 错误页的 HTML 标签，
   最后退回状态码——避免 HTTP/2 下 statusText 为空时只显示「请求失败：」。 */
async function httpErrorText(res) {
  let raw = "";
  try { raw = await res.text(); } catch (_) { /* 读体失败按空处理 */ }
  let detail = "";
  try {
    const body = JSON.parse(raw);
    if (body && body.detail != null) {
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } else if (body && body.message) {
      detail = String(body.message);
    }
  } catch (_) { /* 非 JSON，多为网关返回的 HTML 错误页 */ }
  if (!detail) {
    detail = raw
      .replace(/<(script|style)[\s\S]*?<\/\1>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }
  return `HTTP ${res.status}：${detail ? detail.slice(0, 300) : "无响应内容"}`;
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, Object.assign({ credentials: "same-origin" }, opts));
  if (res.status === 401) { gotoLogin(); throw new Error("未登录或登录已过期"); }
  if (!res.ok) throw new Error(await httpErrorText(res));
  if (res.status === 204) return null;
  return res.json();
}

const STATUS_TEXT = {
  created: "待构建", building: "构建中", running: "运行中",
  stopped: "已停止", failed: "失败",
};
const statusBadge = (s) =>
  `<span class="badge st-${s} ${s === "building" ? "pulse" : ""}">${STATUS_TEXT[s] || s}</span>`;

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------------- 视图切换 ---------------- */
const VIEW_TITLES = {
  apps: "应用交付与实例管理",
  ai: "AI 运行分析",
  routes: "网关路由与端口映射",
  templates: "项目模板与接入指南",
  files: "项目文件浏览与编辑",
  nodes: "宿主机资源池与节点监控",
  alerts: "阈值告警与处置中心",
  audit: "操作审计与安全追溯",
  users: "平台用户与权限管理",
};

function switchWorkspaceView(view) {
  if (view === "users" && !isAdmin()) return;
  activeView = view;
  if (view !== "files") filesTerm.stop(); // 离开文件管理页即回收容器终端会话
  $$("#sideNav .nav-item[data-view]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });
  $$(".ws-view").forEach((pane) => {
    pane.classList.toggle("active", pane.dataset.wsview === view);
  });
  $("#workspaceTitle").textContent = VIEW_TITLES[view] || VIEW_TITLES.apps;

  const showFilter = isAdmin() && (view === "apps" || view === "routes");
  $("#ownerFilter").classList.toggle("hidden", !showFilter);
  $("#listHint").classList.toggle("hidden", !(view === "apps" || view === "routes"));

  if (view === "routes") {
    loadPoolResources();
    renderRoutesView();
  } else if (view === "ai") {
    renderAiView();
  } else if (view === "files") {
    renderFilePage();
  } else if (view === "nodes") {
    loadPoolResources();
    renderNodesView();
    loadHostMetrics();
    startHostPoll();
  } else if (view === "alerts") {
    loadAlerts();
    startAlertPoll();
  } else if (view === "audit") {
    loadAuditLogs();
  } else if (view === "users") {
    hideUserForm();
    loadUsers();
  } else if (view === "apps") {
    renderList();
  }
  if (view !== "nodes") stopHostPoll();
  if (view !== "alerts") stopAlertPoll();
}

/* ---------------- 登录态 ---------------- */
async function loadAuth() {
  try {
    const r = await api("/auth/status");
    if (r && r.auth_enabled && r.user) {
      me = r.user;
      const name = r.user.display_name || r.user.username;
      $("#userName").textContent = name;
      $("#userAvatar").textContent = String(name).charAt(0).toUpperCase();
      $("#userBox").classList.remove("hidden");
      const admin = isAdmin();
      $("#userRole").classList.toggle("hidden", !admin);
      $("#usersBtn").classList.toggle("hidden", !admin);
      $("#ownerFilter").classList.toggle("hidden", !(admin && (activeView === "apps" || activeView === "routes")));
      // 若当前处于用户管理视图但被降级为普通用户，自动切回应用列表
      if (!admin && activeView === "users") {
        switchWorkspaceView("apps");
      }
      syncOwnerOptions();
      renderList();
      if (activeView === "routes") renderRoutesView();
      if (activeView === "users" && admin) renderUsers();
    }
  } catch (_) { /* 401 已由 api() 跳转登录页 */ }
}

async function doLogout() {
  try { await api("/auth/logout", { method: "POST" }); } catch (_) {}
  location.href = "/login";
}

/* ---------------- 左下角环境状态 / 概览 ---------------- */
async function loadMode() {
  const el = $("#modeBadge");
  try {
    const r = await api("/ready");
    readyInfo = r;
    const isProd = r.deploy_mode !== "local";
    const modeText = isProd ? "线上生产" : "本地开发";
    const routeText = r.nginx_enabled ? "Nginx 动态路由" : "端口直连";
    const dockerOk = !!r.docker?.available;
    const dockerText = dockerOk ? "运行正常" : "连接异常";

    el.innerHTML = `
      <div class="env-row">
        <span class="env-k">运行模式</span>
        <span class="env-pill ${isProd ? "is-prod" : "is-local"}">${modeText}</span>
      </div>
      <div class="env-row">
        <span class="env-k">网络路由</span>
        <span class="env-v">${routeText}</span>
      </div>
      <div class="env-row">
        <span class="env-k">Docker 引擎</span>
        <span class="env-status ${dockerOk ? "ok" : "err"}">
          <i class="dot"></i>${dockerText}
        </span>
      </div>
    `;
    if (activeView === "routes") renderRoutesView();
    if (activeView === "nodes") renderNodesView();
  } catch (_) {
    el.innerHTML = `
      <div class="env-row">
        <span class="env-k">系统状态</span>
        <span class="env-status err"><i class="dot"></i>后端连接异常</span>
      </div>
    `;
  }
}

async function loadPoolResources() {
  try {
    const r = await checkResourceFields({ container_port: null, mem_limit: "256m", cpu_quota: 1.0 });
    if (r && r.resources) {
      poolInfo = r.resources;
      if (activeView === "routes") renderRoutesView();
      if (activeView === "nodes") renderNodesView();
    }
  } catch (_) {}
}

function renderStats() {
  const total = apps.length;
  const running = apps.filter((a) => a.status === "running").length;
  const stopped = apps.filter((a) => a.status === "stopped").length;
  const failed = apps.filter((a) => a.status === "failed").length;
  $("#stats").innerHTML = [
    ["应用总数", total], ["运行中", running], ["已停止", stopped], ["异常", failed],
  ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
}

/* ---------------- 视图 2：网关路由 ---------------- */
function buildNginxSnippet(a) {
  const domain = a.access_url
    ? (() => { try { return new URL(a.access_url).hostname; } catch (_) { return `${a.subdomain}.localhost`; } })()
    : `${a.subdomain}.localhost`;
  const upPort = a.host_port || "<待分配宿主端口>";
  return `# PocketPaaS 动态路由配置: paas-${a.id}.conf
server {
    listen 80;
    server_name ${domain};

    location / {
        proxy_pass http://127.0.0.1:${upPort};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }
}`;
}

function openNginxPreview(appId) {
  const a = apps.find((x) => x.id === appId);
  if (!a) return;
  $("#nginxPreviewTitle").textContent = `路由配置预览 · ${a.name}`;
  $("#nginxPreviewMeta").textContent =
    `配置文件路径：/etc/nginx/conf.d/paas-${a.id}.conf · 上游映射：127.0.0.1:${a.host_port ?? "未分配"} → 容器:${a.container_port}`;
  $("#nginxPreviewCode").textContent = buildNginxSnippet(a);
  $("#nginxPreviewMask").classList.remove("hidden");
}

function renderRoutesView() {
  const rows = visibleApps();
  const activeRoutes = rows.filter((a) => a.host_port != null).length;
  const gatewayMode = readyInfo?.nginx_enabled ? "Nginx 反代" : "端口直连";
  const portRange = poolInfo?.port_range || "20000-29999";
  const portsUsed = poolInfo?.ports_used ?? activeRoutes;

  $("#routeStats").innerHTML = [
    ["转发模式", gatewayMode],
    ["已分配端口实例", activeRoutes],
    ["宿主端口池占用", `${portsUsed} 个`],
    ["可用端口区间", portRange],
  ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v stat-v-sm">${esc(v)}</div></div>`).join("");

  const q = routeQuery.trim().toLowerCase();
  const filtered = rows.filter((a) => {
    if (!q) return true;
    return (
      (a.name || "").toLowerCase().includes(q) ||
      (a.subdomain || "").toLowerCase().includes(q) ||
      String(a.host_port || "").includes(q) ||
      String(a.container_port || "").includes(q)
    );
  });

  if (!filtered.length) {
    $("#routesBody").innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:32px">暂无匹配的路由记录</td></tr>`;
    return;
  }

  $("#routesBody").innerHTML = filtered.map((a) => {
    const urlHtml = a.access_url
      ? `<a href="${esc(a.access_url)}" target="_blank" rel="noopener" class="route-link">${esc(a.access_url)}</a>`
      : `<span class="muted">尚未部署</span>`;
    const portMap = a.host_port
      ? `<span class="tag">${a.host_port} → ${a.container_port}/tcp</span>`
      : `<span class="muted">— → ${a.container_port}/tcp</span>`;
    return `
      <tr data-id="${a.id}">
        <td><strong>${esc(a.name)}</strong>${isAdmin() && a.owner_name ? ` <span class="muted">(${esc(a.owner_name)})</span>` : ""}</td>
        <td><code>${esc(a.subdomain)}</code></td>
        <td>${urlHtml}</td>
        <td>${portMap}</td>
        <td>${statusBadge(a.status)}</td>
        <td>
          <div class="u-actions">
            ${a.access_url ? `<button class="btn btn-ghost btn-sm" data-ract="copy" data-url="${esc(a.access_url)}">复制链接</button>` : ""}
            <button class="btn btn-ghost btn-sm" data-ract="nginx">Nginx 配置</button>
            <button class="btn btn-ghost btn-sm" data-ract="detail">应用详情</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

/* ---------------- 视图 4：资源与节点 (Infrastructure) ---------------- */
function renderNodesView() {
  const d = readyInfo?.docker || {};
  const p = poolInfo || {};

  const cpuTotal = p.cpu_total || 1;
  const cpuCommitted = Number(p.cpu_committed || 0).toFixed(1);
  const cpuPct = Math.min(100, Math.round(((p.cpu_committed || 0) / cpuTotal) * 100));

  const memTotal = p.mem_total || 1;
  const memPct = Math.min(100, Math.round(((p.mem_committed || 0) / memTotal) * 100));

  const portsUsed = p.ports_used ?? apps.filter((a) => a.host_port != null).length;
  const portRangeStr = p.port_range || "20000-29999";
  const [pStart, pEnd] = portRangeStr.split("-").map((x) => parseInt(x, 10));
  const portTotal = (pEnd - pStart + 1) || 10000;
  const portPct = Math.min(100, Math.max(1, Math.round((portsUsed / portTotal) * 100)));

  $("#nodeTopStats").innerHTML = [
    ["宿主机 CPU 总核数", `${cpuTotal} 核`],
    ["宿主机物理总内存", p.mem_total_h || "探测中…"],
    ["当前空闲可用内存", p.mem_available_h || "探测中…"],
    ["运行中容器 / 镜像数", `${d.containers_running ?? 0} / ${d.images ?? 0}`],
  ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v stat-v-sm">${esc(v)}</div></div>`).join("");

  $("#nodeGauges").innerHTML = `
    <div class="surface-card gauge-card">
      <div class="gauge-top">
        <span class="gauge-title">CPU 核心承诺水位</span>
        <span class="gauge-val">${cpuCommitted} / ${cpuTotal} 核 (${cpuPct}%)</span>
      </div>
      <div class="progress-bar"><div class="progress-fill ${cpuPct > 85 ? "warn" : ""}" style="width:${cpuPct}%"></div></div>
    </div>

    <div class="surface-card gauge-card">
      <div class="gauge-top">
        <span class="gauge-title">内存配额承诺水位</span>
        <span class="gauge-val">${esc(p.mem_committed_h || "0 B")} / ${esc(p.mem_total_h || "—")} (${memPct}%)</span>
      </div>
      <div class="progress-bar"><div class="progress-fill ${memPct > 85 ? "warn" : ""}" style="width:${memPct}%"></div></div>
    </div>

    <div class="surface-card gauge-card">
      <div class="gauge-top">
        <span class="gauge-title">宿主机动态端口池 (${esc(portRangeStr)})</span>
        <span class="gauge-val">已分配 ${portsUsed} / ${portTotal}</span>
      </div>
      <div class="progress-bar"><div class="progress-fill" style="width:${portsUsed ? portPct : 0}%"></div></div>
    </div>
  `;

  $("#dockerDaemonInfo").innerHTML = `
    <table class="info-table" style="margin-bottom:0">
      <tr><td>Daemon 状态</td><td>${d.available ? '<span class="badge st-running">在线 (Available)</span>' : '<span class="badge st-failed">离线 / 异常</span>'}</td></tr>
      <tr><td>Docker Server 版本</td><td><code>${esc(d.server_version || "—")}</code></td></tr>
      <tr><td>Engine API 版本</td><td><code>${esc(d.api_version || "—")}</code></td></tr>
      <tr><td>运行中容器总数</td><td><strong>${d.containers_running ?? 0}</strong> 个</td></tr>
      <tr><td>本地缓存镜像数</td><td><strong>${d.images ?? 0}</strong> 个</td></tr>
      <tr><td>受管容器隔离标签</td><td><code>paas.managed=true</code></td></tr>
      <tr><td>专用内网隔离网桥</td><td><code>paas-net (br-paas)</code></td></tr>
    </table>
  `;

  if (!apps.length) {
    $("#nodeQuotaBody").innerHTML = `<tr><td colspan="5" class="muted" style="text-align:center;padding:24px">暂无应用实例</td></tr>`;
  } else {
    $("#nodeQuotaBody").innerHTML = apps.map((a) => `
      <tr>
        <td><strong>${esc(a.name)}</strong></td>
        <td>${statusBadge(a.status)}</td>
        <td><code>${a.cpu_quota} 核</code></td>
        <td><code>${esc(a.mem_limit)}</code></td>
        <td>${a.host_port ? `<code>${a.host_port}</code>` : '<span class="muted">未分配</span>'}</td>
      </tr>
    `).join("");
  }
}

/* ---------------- 视图 4：宿主机实时性能（仿性能监控模板 · 浅色适配） ---------------- */
function startHostPoll() {
  stopHostPoll();
  hostTimer = setInterval(() => { if (activeView === "nodes") loadHostMetrics(); }, 2000);
}

function stopHostPoll() {
  if (hostTimer) { clearInterval(hostTimer); hostTimer = null; }
}

async function loadHostMetrics() {
  try {
    hostMetrics = await api("/system/host-metrics");
    renderHostMetrics();
  } catch (_) { /* 401 已由 api() 跳转登录页 */ }
}

/* 网格线与纵轴刻度（100 / 75 / 50 / 25 / 0），仅初始化一次 */
function ensureHostChart() {
  const chart = $("#hostCpuChart");
  if (!chart || chart.dataset.ready) return;
  [100, 75, 50, 25, 0].forEach((v) => {
    const top = (100 - v) + "%";
    const gl = document.createElement("div");
    gl.className = "host-gridline";
    gl.style.top = top;
    chart.appendChild(gl);
    const yl = document.createElement("div");
    yl.className = "host-ylabel";
    yl.style.top = top;
    yl.textContent = v;
    chart.appendChild(yl);
  });
  chart.dataset.ready = "1";
}

/* CPU 利用率折线：x 轴按帧数等比铺满 100，y 轴直接取利用率数值 */
function drawHostCpuChart(history) {
  const line = $("#hostCpuLine");
  const dot = $("#hostCpuCursor");
  if (!line || !dot) return;
  ensureHostChart();
  const n = history.length;
  if (!n) { line.setAttribute("points", ""); dot.style.display = "none"; return; }
  dot.style.display = "";
  const span = n > 1 ? n - 1 : 1;
  const clamp = (v) => 100 - Math.min(Math.max(v, 0), 100);
  line.setAttribute("points", history.map((v, i) => `${(i / span) * 100},${clamp(v)}`).join(" "));
  dot.style.top = clamp(history[n - 1]) + "%";
}

function fmtUptime(sec) {
  if (!sec || sec <= 0) return "—";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d} 天 ${h} 小时`;
  if (h > 0) return `${h} 小时 ${m} 分钟`;
  return `${m} 分钟`;
}

function renderContainerStats(list) {
  const body = $("#containerStatsBody");
  if (!body) return;
  if (!list.length) {
    body.innerHTML = `<tr><td colspan="5" class="muted" style="text-align:center;padding:24px">暂无运行中的受管容器</td></tr>`;
    return;
  }
  body.innerHTML = list.map((c) => {
    const running = c.status === "running";
    const badge = running
      ? '<span class="badge st-running">运行中</span>'
      : `<span class="badge st-stopped">${esc(c.status)}</span>`;
    return `
      <tr>
        <td><strong>${esc(c.app_name)}</strong> <span class="muted">${esc(c.name)}</span></td>
        <td>${badge}</td>
        <td><code>${Number(c.cpu_percent || 0).toFixed(1)}%</code></td>
        <td>${esc(c.mem_usage_h)} / ${esc(c.mem_limit_h)} <span class="muted">（${Number(c.mem_percent || 0).toFixed(1)}%）</span></td>
        <td>${esc(c.net_rx_h)} / ${esc(c.net_tx_h)}</td>
      </tr>`;
  }).join("");
}

function renderHostMetrics() {
  const m = hostMetrics;
  if (!m) return;

  const cpu = m.cpu || {};
  $("#hostCpuCores").textContent = cpu.cores ?? "—";
  const cpuPct = Math.min(100, Math.round(cpu.percent || 0));
  $("#hostCpuValue").textContent = cpuPct + "%";
  $("#hostCpuBar").style.width = cpuPct + "%";
  drawHostCpuChart(cpu.history || []);
  $("#hostLoad").textContent = Array.isArray(cpu.load) && cpu.load.length === 3
    ? cpu.load.map((x) => Number(x).toFixed(2)).join(" / ")
    : "暂不可用";

  const mem = m.memory || {};
  const memPct = Math.min(100, Math.round(mem.percent || 0));
  $("#hostMemValue").textContent = memPct + "%";
  $("#hostMemBar").style.width = memPct + "%";
  $("#hostMemSub").textContent = `${esc(mem.used_h || "—")} / ${esc(mem.total_h || "—")}`;

  const disk = m.disk || {};
  const diskPct = Math.min(100, Math.round(disk.percent || 0));
  $("#hostDiskValue").textContent = diskPct + "%";
  $("#hostDiskBar").style.width = diskPct + "%";
  $("#hostDiskSub").textContent = `${esc(disk.used_h || "—")} / ${esc(disk.total_h || "—")}`;

  $("#hostUptime").textContent = fmtUptime(m.uptime_seconds);
  $("#hostContainers").textContent = m.containers_running ?? 0;
  renderContainerStats(m.containers || []);
}

/* ---------------- 应用列表 ---------------- */
function cardHtml(a) {
  const access = a.access_url
    ? `<a href="${esc(a.access_url)}" target="_blank" rel="noopener">${esc(a.access_url)}</a>`
    : `<span class="muted">未部署</span>`;
  const src = a.source_type === "git" ? "Git" : "上传包";
  const ownerRow = isAdmin()
    ? `<div class="row"><span class="k">所属用户</span><span class="v card-owner">${esc(a.owner_name || "—")}</span></div>`
    : "";
  return `
  <div class="app-card" data-id="${a.id}">
    <div class="card-top">
      <div>
        <div class="card-title">${esc(a.name)}</div>
        <div class="card-meta">${esc(a.subdomain)} · ${src}${a.project_type ? " · " + esc(a.project_type) : ""}</div>
      </div>
      ${statusBadge(a.status)}
    </div>
    <div class="card-rows">
      <div class="row"><span class="k">访问入口</span><span class="v">${access}</span></div>
      <div class="row"><span class="k">宿主端口</span><span class="v">${a.host_port ?? "—"}</span></div>
      <div class="row"><span class="k">配额</span><span class="v">${esc(a.mem_limit)} · ${a.cpu_quota} 核</span></div>
      ${ownerRow}
    </div>
    <div class="card-actions">
      <button class="btn btn-ghost btn-sm" data-act="detail">详情</button>
      <button class="btn btn-primary btn-sm" data-act="deploy">${a.status === "created" ? "构建部署" : "重新部署"}</button>
      ${a.status === "running" ? `<button class="btn btn-ghost btn-sm" data-act="stop">停止</button>` : ""}
      ${a.status === "stopped" || a.status === "failed" ? `<button class="btn btn-ghost btn-sm" data-act="restart">启动</button>` : ""}
      <button class="btn btn-danger btn-sm" data-act="delete">销毁</button>
    </div>
  </div>`;
}

function visibleApps() {
  if (!isAdmin() || !ownerFilter) return apps;
  return apps.filter((a) => String(a.owner_id ?? "") === ownerFilter);
}

function syncOwnerOptions() {
  if (!isAdmin()) return;
  const sel = $("#ownerSelect");
  const seen = new Map();
  apps.forEach((a) => {
    if (a.owner_id != null && !seen.has(String(a.owner_id))) {
      seen.set(String(a.owner_id), a.owner_name || `用户#${a.owner_id}`);
    }
  });
  const opts = [`<option value="">全部用户</option>`]
    .concat([...seen.entries()].map(([id, name]) => `<option value="${id}">${esc(name)}</option>`))
    .join("");
  if (sel.innerHTML !== opts) sel.innerHTML = opts;
  if (ownerFilter && !seen.has(ownerFilter)) ownerFilter = "";
  sel.value = ownerFilter;
}

function renderList() {
  renderStats();
  const rows = visibleApps();
  $("#listHint").textContent = rows.length
    ? (isAdmin() && ownerFilter ? `筛选出 ${rows.length} / 共 ${apps.length} 个` : `共 ${rows.length} 个`)
    : "";
  $("#appList").innerHTML = rows.map(cardHtml).join("");
  $("#emptyState").classList.toggle("hidden", rows.length > 0);
  if (activeView === "routes") renderRoutesView();
  if (activeView === "files") renderFilePage();
  if (activeView === "nodes") renderNodesView();
  if (activeView === "users" && isAdmin()) renderUsers();
}

async function loadApps() {
  try {
    apps = await api("/apps");
    syncOwnerOptions();
    renderList();
    if (activeView === "ai") renderAiAppOptions();
  } catch (e) {
    toast("加载应用列表失败：" + e.message, "err");
  }
}

/* ---------------- 卡片操作 ---------------- */
async function onCardAction(id, act) {
  const a = apps.find((x) => x.id === id);
  if (!a) return;
  try {
    if (act === "detail") return openDrawer(a);
    if (act === "delete") {
      if (!confirm(`确定销毁应用「${a.name}」？将停止并删除容器、摘除路由。`)) return;
      await api(`/apps/${id}`, { method: "DELETE" });
      toast("已销毁", "ok");
      if (current?.id === id) closeDrawer();
      return loadApps();
    }
    if (act === "deploy") {
      if (a.status === "created") return doDeploy(id, false);
      return openRedeploy(a);
    }
    if (act === "stop") { await api(`/apps/${id}/stop`, { method: "POST" }); toast("已停止", "ok"); return loadApps(); }
    if (act === "restart") { await api(`/apps/${id}/restart`, { method: "POST" }); toast("已启动", "ok"); return loadApps(); }
    if (act === "rollback") {
      if (!confirm(`确定将应用「${a.name}」回滚到上一版本？`)) return;
      await api(`/apps/${id}/rollback`, { method: "POST" });
      toast("已回滚到上一版本", "ok");
      return current?.id === id ? refreshCurrent() : loadApps();
    }
  } catch (e) {
    toast(act + " 失败：" + e.message, "err");
    loadApps();
  }
}

/* ---------------- 部署流水线（构建 → 部署） ---------------- */
function checkResourceFields({ container_port = null, mem_limit, cpu_quota, app_id = null }) {
  return api("/system/resource-check", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ container_port, mem_limit, cpu_quota, app_id }),
  });
}

async function doDeploy(id, redeploy) {
  toast(redeploy ? "正在重新构建，并重新分配资源与端口…" : "正在构建并部署…");
  const dep = await api(`/apps/${id}/build`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
  });
  if (dep.build_status === "failed") throw new Error("构建失败，请查看构建日志");
  if (dep.port_notice) toast(dep.port_notice);
  const deployed = await api(`/apps/${id}/deploy`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reassign_port: !!redeploy }),
  });
  if (redeploy && deployed) toast(`重新部署成功，已分配宿主端口 ${deployed.host_port}`, "ok");
  else toast("部署成功", "ok");
  return current && current.id === id ? refreshCurrent() : loadApps();
}

/* ---------------- 重新部署弹窗（沿用配额 / 更改配额） ---------------- */
let redeployTargetId = null;
const rdConfirm = { mem: false, cpu: false };
const RD_FIELDS = {
  mem: { input: "#rdMem", btn: "#rdMemConfirm", hint: "#rdMemHint" },
  cpu: { input: "#rdCpu", btn: "#rdCpuConfirm", hint: "#rdCpuHint" },
};

const rdMode = () => $("input[name='rdMode']:checked").value;

function updateRdGate() {
  const change = rdMode() === "change";
  $("#rdQuotaBox").classList.toggle("hidden", !change);
  const ok = !change || (rdConfirm.mem && rdConfirm.cpu);
  $("#redeployGo").disabled = !ok;
  const gate = $("#rdGate");
  gate.classList.toggle("hidden", ok);
  gate.textContent = change && !ok ? "请先确认内存与 CPU 配额" : "";
}

function setRdField(key, confirmed, level, message) {
  rdConfirm[key] = confirmed;
  const cfg = RD_FIELDS[key];
  const btn = $(cfg.btn);
  btn.textContent = confirmed ? "已确认 ✓" : "确认";
  btn.classList.toggle("confirmed", confirmed);
  const hint = $(cfg.hint);
  hint.textContent = message || "";
  hint.className = "field-hint" + (level ? " " + level : "");
  updateRdGate();
}

function showRdError(msg) {
  const el = $("#rdError");
  el.textContent = msg;
  el.classList.toggle("hidden", !msg);
}

function openRedeploy(app) {
  redeployTargetId = app.id;
  $("input[name='rdMode'][value='keep']").checked = true;
  $("#rdMem").value = app.mem_limit;
  $("#rdCpu").value = app.cpu_quota;
  Object.keys(RD_FIELDS).forEach((k) => setRdField(k, false, "", ""));
  showRdError("");
  $("#redeployMask").classList.remove("hidden");
}

async function confirmRdField(key) {
  const mem = $("#rdMem").value.trim();
  const cpu = parseFloat($("#rdCpu").value);
  if (key === "mem" && !mem) { setRdField("mem", false, "error", "请填写内存上限"); return; }
  if (key === "cpu" && !Number.isFinite(cpu)) { setRdField("cpu", false, "error", "请填写 CPU 核数"); return; }
  const btn = $(RD_FIELDS[key].btn);
  btn.disabled = true; btn.textContent = "校验中…";
  try {
    const r = await checkResourceFields({
      mem_limit: mem || "0",
      cpu_quota: Number.isFinite(cpu) ? cpu : 0,
      app_id: redeployTargetId,
    });
    const res = r[key];
    setRdField(key, res.level !== "error", res.level, res.message);
    toast(res.message, res.level === "ok" ? "ok" : res.level === "error" ? "err" : "");
  } catch (e) {
    setRdField(key, false, "error", "校验失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

async function runRedeploy() {
  const id = redeployTargetId;
  if (!id) return;
  showRdError("");
  try {
    if (rdMode() === "change") {
      await api(`/apps/${id}/quota`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mem_limit: $("#rdMem").value.trim(),
          cpu_quota: parseFloat($("#rdCpu").value),
        }),
      });
    }
    $("#redeployMask").classList.add("hidden");
    await doDeploy(id, true);
  } catch (e) {
    showRdError("部署失败：" + e.message);
  }
}

/* ---------------- 详情抽屉 ---------------- */
function openDrawer(app) {
  current = app;
  quotaEditing = false;
  cfgEditing = false;
  currentService = defaultService(app);
  $("#drawerMask").classList.remove("hidden");
  $("#drawer").classList.remove("hidden");
  switchTab("overview");
  renderDrawerHead();
  syncServiceSelect();
  renderOverview();
  renderActions();
  loadBuildLogs();
  loadMetrics();
  loadWebhook();
  clearInterval(metricsTimer);
  metricsTimer = setInterval(() => { if (current) { loadMetrics(); loadReadiness(); } }, 5000);
}

function closeDrawer() {
  $("#drawerMask").classList.add("hidden");
  $("#drawer").classList.add("hidden");
  stopWs();
  drawerTerm.stop();
  stopWhPoll();
  clearInterval(metricsTimer);
  metricsTimer = null;
  resetDrawerFiles();
  closeEditor();
  current = null;
  currentService = null;
}

/* 多容器服务：默认选中公开服务，否则取第一个 */
function defaultService(app) {
  const list = (app && app.services) || [];
  return (list.find((s) => s.is_public) || list[0] || {}).name || null;
}

/* 刷新详情抽屉的服务选择器；单服务应用隐藏该控件 */
function syncServiceSelect() {
  const bar = $("#drawerServiceBar");
  const sel = $("#drawerService");
  if (!bar || !sel) return;
  const list = (current && current.services) || [];
  if (list.length <= 1) {
    bar.classList.add("hidden");
    sel.innerHTML = "";
    currentService = list[0] ? list[0].name : null;
    return;
  }
  bar.classList.remove("hidden");
  if (!list.some((s) => s.name === currentService)) currentService = defaultService(current);
  sel.innerHTML = list
    .map(
      (s) =>
        `<option value="${esc(s.name)}">${esc(s.name)}${s.is_public ? "（公开）" : ""} · ${esc(STATUS_TEXT[s.status] || s.status || "未知")}</option>`,
    )
    .join("");
  sel.value = currentService || "";
}

/* 切换服务：断开旧连接，按当前 tab 重建对应视图 */
function onServiceChange(name) {
  currentService = name || null;
  stopWs();
  drawerTerm.stop();
  loadMetrics();
  const tab = ($("#drawerTabs .tab.active") || {}).dataset;
  if (!tab) return;
  if (tab.tab === "livelogs") startWs();
  else if (tab.tab === "terminal") drawerTerm.start();
  else if (tab.tab === "files") {
    const inst = fmInstances.drawer;
    if (inst) fmLoad(inst);
  }
  else if (tab.tab === "diagnose") runDiagnose();
}

function renderDrawerHead() {
  $("#drawerTitle").textContent = current.name;
  $("#drawerSub").innerHTML = `${esc(current.subdomain)} · ${STATUS_TEXT[current.status] || current.status}`;
}

/* 多容器服务：列表展示服务名、公开入口、状态、宿主端口；可单独重启 */
function renderServiceTags(services) {
  if (!services || !services.length) return "<span class='muted'>—</span>";
  const publicCount = services.filter((s) => s.is_public).length;
  const summary =
    services.length > 1
      ? `<div class="svc-summary">${services.length} 个服务 · ${publicCount} 个公开</div>`
      : "";
  const rows = services
    .map((s) => {
      const statusKey = s.status === "restarting" ? "building" : STATUS_TEXT[s.status] ? s.status : "created";
      const statusText = s.status === "restarting" ? "重启中" : STATUS_TEXT[statusKey] || s.status || "未知";
      const name = s.access_url
        ? `<a class="svc-name" href="${esc(s.access_url)}" target="_blank" rel="noopener" title="打开 ${esc(s.access_url)}">${esc(s.name)}</a>`
        : `<span class="svc-name">${esc(s.name)}</span>`;
      const badge = s.is_primary
        ? `<span class="svc-badge primary">主入口</span>`
        : s.is_public
          ? `<span class="svc-badge public">公开</span>`
          : `<span class="svc-badge internal">内部</span>`;
      const port = s.host_port
        ? `<span class="svc-port" title="宿主端口">:${s.host_port}</span>`
        : `<span class="svc-port muted">仅内网</span>`;
      const pulse = statusKey === "building" ? " pulse" : "";
      return `<div class="svc-item" title="${esc(s.container_name || "")}">
        <span class="svc-dot ${statusKey}${pulse}" title="${statusText}"></span>
        <div class="svc-main">${name}${badge}<span class="svc-state">${statusText}</span></div>
        ${port}
        <button class="svc-restart" type="button" data-svc-restart="${esc(s.name)}" title="重启服务 ${esc(s.name)}">↻</button>
      </div>`;
    })
    .join("");
  return `<div class="svc-list">${summary}<div class="svc-rows">${rows}</div></div>`;
}

async function restartService(name) {
  if (!current) return;
  try {
    const updated = await api(`/apps/${current.id}/services/${encodeURIComponent(name)}/restart`, { method: "POST" });
    current = updated;
    syncServiceSelect();
    renderOverview();
    renderActions();
    toast(`服务 ${name} 已重启`, "ok");
    loadApps();
  } catch (e) {
    toast("重启失败：" + e.message, "err");
  }
}

function renderOverview() {
  const a = current;
  const access = a.access_url
    ? `<a href="${esc(a.access_url)}" target="_blank" rel="noopener">${esc(a.access_url)}</a>` : "<span class='muted'>未部署</span>";
  $("#overviewInfo").innerHTML = `
    <table class="info-table">
      <tr><td>状态</td><td>${statusBadge(a.status)}</td></tr>
      <tr><td>访问入口</td><td>${access}</td></tr>
      <tr><td>镜像</td><td>${a.image ? `<span class="tag">${esc(a.image)}</span>` : "<span class='muted'>尚未构建</span>"}</td></tr>
      <tr><td>项目类型</td><td>${a.project_type ? `<span class="tag">${esc(a.project_type)}</span>` : "—"}</td></tr>
      <tr><td>服务</td><td>${renderServiceTags(a.services)}</td></tr>
      <tr><td>容器端口</td><td>${a.container_port}</td></tr>
      <tr><td>宿主端口</td><td>${a.host_port ?? "—"}</td></tr>
      <tr><td>就绪状态</td><td id="readyCell"><span class="muted">探测中…</span></td></tr>
      <tr><td>代码来源</td><td>${a.source_type === "git" ? esc(a.git_url || "Git") : "上传 zip 包"}</td></tr>
      ${isAdmin() ? `<tr><td>所属用户</td><td><span class="card-owner">${esc(a.owner_name || "—")}</span></td></tr>` : ""}
      <tr><td>创建时间</td><td>${esc(a.created_at)}</td></tr>
    </table>`;
  loadReadiness();
  if (!quotaEditing) {
    $("#quotaMem").value = a.mem_limit;
    $("#quotaCpu").value = a.cpu_quota;
    resetQuotaConfirm();
  }
  if (!cfgEditing) {
    $("#cfgEnv").value = envToLines(a.env);
    $("#cfgCmd").value = a.command || "";
  }
}

function renderActions() {
  const a = current;
  const btns = [];
  btns.push(`<button class="btn btn-primary btn-sm" data-dact="deploy">${a.status === "created" ? "构建部署" : "重新部署"}</button>`);
  if (a.status === "running") btns.push(`<button class="btn btn-ghost btn-sm" data-dact="stop">停止</button>`);
  if (a.status === "stopped" || a.status === "failed") btns.push(`<button class="btn btn-ghost btn-sm" data-dact="restart">启动</button>`);
  if (a.previous_slot) btns.push(`<button class="btn btn-ghost btn-sm" data-dact="rollback">回滚上一版本</button>`);
  if (a.access_url) btns.push(`<a class="btn btn-ghost btn-sm" href="${esc(a.access_url)}" target="_blank" rel="noopener">打开应用</a>`);
  btns.push(`<button class="btn btn-danger btn-sm" data-dact="delete">销毁</button>`);
  $("#drawerActions").innerHTML = btns.join("");
}

async function refreshCurrent() {
  if (!current) return;
  const prevService = currentService;
  try {
    current = await api(`/apps/${current.id}`);
    currentService = prevService;
    renderDrawerHead(); syncServiceSelect(); renderOverview(); renderActions();
  } catch (_) {}
  loadApps();
}

/* ---------------- 选项卡 ---------------- */
function switchTab(name) {
  $$("#drawerTabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name)); $$
(".tab-pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === name));
  if (name !== "livelogs") stopWs();
  if (name !== "terminal") drawerTerm.stop();
  if (name === "files") renderDrawerFiles();
  if (name === "diagnose") runDiagnose();
  if (name === "terminal") drawerTerm.start();
}

/* ---------------- 一键诊断 ---------------- */
const DIAG_ICON = { ok: "✓", warn: "!", fail: "✕", skip: "–" };
const DIAG_TEXT = { ok: "通过", warn: "提示", fail: "失败", skip: "跳过" };
const DIAG_LEVEL_TEXT = { ok: "全部通过", warn: "存在提示", fail: "存在问题" };

async function runDiagnose() {
  if (!current || diagRunning) return;
  const appId = current.id;
  diagRunning = true;
  $("#diagLevel").className = "diag-level";
  $("#diagLevel").textContent = "诊断中…";
  $("#diagMeta").textContent = "";
  $("#diagHint").textContent = "";
  $("#diagnoseList").innerHTML = '<div class="empty-inline">正在逐项检查容器、端口、路由与部署记录…</div>';
  try {
    const d = await api(`/apps/${appId}/diagnose`);
    if (!current || current.id !== appId) return; // 期间切换了应用，丢弃过期结果
    renderDiagnose(d);
  } catch (e) {
    $("#diagLevel").className = "diag-level fail";
    $("#diagLevel").textContent = "诊断失败";
    $("#diagnoseList").innerHTML = `<div class="empty-inline">${esc(e.message)}</div>`;
  } finally {
    diagRunning = false;
  }
}

function renderDiagnose(d) {
  const s = d.summary;
  $("#diagLevel").className = "diag-level " + d.level;
  $("#diagLevel").textContent = DIAG_LEVEL_TEXT[d.level] || d.level;
  $("#diagMeta").textContent =
    `${s.passed}/${s.total} 项通过` + (s.skipped ? `，${s.skipped} 项跳过` : "");
  $("#diagHint").textContent = s.hint;
  $("#diagnoseList").innerHTML = d.checks
    .map(
      (c) => `
    <div class="diag-item ${c.status}">
      <span class="diag-ico" aria-hidden="true">${DIAG_ICON[c.status] || "?"}</span>
      <div class="diag-main">
        <div class="diag-label">${esc(c.label)}<span class="diag-st">${DIAG_TEXT[c.status] || c.status}</span></div>
        <div class="diag-detail">${esc(c.detail)}</div>
        ${c.hint ? `<div class="diag-advice">建议：${esc(c.hint)}</div>` : ""}
      </div>
    </div>`,
    )
    .join("");
}

/* ---------------- 自动部署（Git Webhook） ----------------
   展示 webhook 地址与签名密钥，并支持手动触发；自动部署在后台构建，
   因此触发后轮询状态直到流水线结束。
---------------------------------------------------------- */
let whTimer = null;

function stopWhPoll() {
  clearInterval(whTimer);
  whTimer = null;
}

function startWhPoll() {
  if (whTimer) return;
  whTimer = setInterval(async () => {
    const v = await loadWebhook();
    if (!v || !v.running) stopWhPoll();
  }, 4000);
}

async function loadWebhook() {
  if (!current) return null;
  try {
    const v = await api(`/apps/${current.id}/webhook`);
    renderWebhook(v);
    return v;
  } catch (e) {
    $("#whState").textContent = `读取失败：${e.message}`;
    return null;
  }
}

function renderWebhook(v) {
  $("#whUrl").value = `${location.origin}${v.path}`;
  $("#whSecret").value = v.secret;
  $("#whSourceHint").textContent =
    v.source_type === "git"
      ? `代码来源为 Git 仓库，收到推送后会重新拉取最新源码再重建部署`
      : "当前源码来自上传 / 示例包：收到推送后会重建镜像并重新部署（不会拉取远端代码）";

  const at = v.last_at ? v.last_at.slice(0, 19).replace("T", " ") : "";
  $("#whDot").className =
    "wh-dot" + (v.running ? " busy" : v.last_stage === "success" ? " ok" : v.last_stage === "failed" ? " bad" : "");
  const state = $("#whState");
  if (v.running) state.textContent = "自动部署进行中…（构建期间旧容器仍在服务）";
  else if (v.last_stage === "success") state.textContent = `最近一次自动部署成功 · ${at}`;
  else if (v.last_stage === "failed") state.textContent = `最近一次自动部署失败 · ${v.last_error || "未知原因"}`;
  else if (v.last_stage === "received") state.textContent = `已收到推送，等待执行 · ${at}`;
  else state.textContent = "尚未收到推送";

  $("#whTriggerBtn").disabled = !!v.running;
  if (v.running) startWhPoll();
}

async function triggerWebhook() {
  if (!current) return;
  $("#whTriggerBtn").disabled = true;
  try {
    await api(`/apps/${current.id}/webhook/trigger`, { method: "POST" });
    toast("已受理，正在后台重新构建并部署", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
  await loadWebhook();
  startWhPoll();
}

async function resetWebhook() {
  if (!current) return;
  if (!confirm("重置后旧密钥立即失效，需要到 Git 平台更新配置。确认重置？")) return;
  try {
    await api(`/apps/${current.id}/webhook/reset`, { method: "POST" });
    toast("密钥已重置", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
  await loadWebhook();
}

/* ---------------- Web 终端（容器内交互式 shell） ----------------
   零依赖实现：后端 WS 转发 docker exec(TTY) 的原始字节（base64），
  前端先流式解码 UTF-8，再剥离 ANSI 控制序列后写入 <pre>；
  输入按行发送（TTY 自带回显，无需本地行编辑）。
   抽屉「终端」tab 与「项目文件管理」页各挂一个实例（id 前缀 sh / fmSh），
   同一时间只保留一个容器会话，避免多份 exec 进程并存。
------------------------------------------------------------------------ */
const SH_MAX_CHARS = 60000; // 回滚缓冲上限，避免长时间会话把 DOM 撑爆

function stripAnsi(s) {
  return s
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, "") // OSC
    .replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, "") // CSI（颜色/光标/清屏）
    .replace(/\u001b[@-Z\\-_]/g, "") // 其它单字符转义
    .replace(/\u001b/g, "");
}

let activeTerm = null;      // 当前已连接的终端实例（全局唯一）

/**
 * 创建终端实例。
 * @param {string} p DOM id 前缀，对应 `${p}Dot|State|Screen|Input|Toggle|Interrupt|Clear`
 * @param {() => (number|null)} getAppId 目标应用解析器（抽屉取 current，文件页取 fmCurrentAppId）
 * @param {() => (string|null)} [getService] 目标服务解析器（多容器应用）
 */
function createTerminal(p, getAppId, getService) {
  const q = (suffix) => document.getElementById(p + suffix);
  let sock = null;  // 当前 WebSocket
  let dec = null;   // 流式 UTF-8 解码器（跨分片保留半个多字节字符）

  function append(text) {
    const box = q("Screen");
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
    box.textContent += text;
    if (box.textContent.length > SH_MAX_CHARS) box.textContent = box.textContent.slice(-SH_MAX_CHARS / 2);
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  function send(obj) {
    if (sock && sock.readyState === 1) sock.send(JSON.stringify(obj));
  }

  /** 会话终止（服务端结束或连接异常）时的统一收尾。 */
  function finish(label) {
    sock = null;
    dec = null;
    if (activeTerm === term) activeTerm = null;
    q("Dot").classList.remove("on");
    q("Toggle").textContent = "连接";
    q("Input").disabled = true;
    q("State").textContent = label;
  }

  /** 用户主动断开 / 离开终端所在视图。 */
  function stop() {
    if (sock) { try { sock.close(); } catch (_) {} }
    finish("未连接");
  }

  function start() {
    if (sock) return;
    const appId = getAppId();
    if (!appId) return;
    if (activeTerm && activeTerm !== term) activeTerm.stop(); // 同一时间只保留一个容器会话
    const service = getService ? getService() : null;
    const svcQ = service ? `?service=${encodeURIComponent(service)}` : "";
    const s = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/apps/${appId}/shell${svcQ}`);
    sock = s;
    dec = new TextDecoder("utf-8");
    activeTerm = term;
    q("Screen").textContent = "";
    q("State").textContent = "连接中…";
    q("Toggle").textContent = "断开";
    s.onopen = () => {
      q("Dot").classList.add("on");
      q("State").textContent = "已连接";
      q("Input").disabled = false;
      const box = q("Screen");
      send({ type: "resize", height: Math.max(10, Math.floor(box.clientHeight / 18)), width: Math.max(20, Math.floor(box.clientWidth / 8)) });
      q("Input").focus();
    };
    s.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.type === "open") append(`—— 已连接服务 ${m.service || "default"} · 容器 ${m.container} ——\n`);
      else if (m.type === "output") {
        const bytes = Uint8Array.from(atob(m.data), (c) => c.charCodeAt(0));
        const text = dec.decode(bytes, { stream: true });
        // 部分 shell 会发光标位置查询（\x1b[6n）并等待应答，这里代答避免卡住
        if (text.includes("\u001b[6n")) send({ type: "input", data: "\u001b[1;1R" });
        const clean = stripAnsi(text).replace(/\r\n?/g, "\n");
        if (clean) append(clean);
      } else if (m.type === "eof") { append(`—— ${m.message} ——\n`); finish("会话已结束"); }
      else if (m.type === "error") { append(`错误：${m.message}\n`); finish(m.message); }
    };
    s.onclose = () => { if (sock === s) finish("已断开"); };
    s.onerror = () => { if (sock === s) q("State").textContent = "连接失败（容器可能未运行）"; };
  }

  const term = {
    start,
    stop,
    isOpen: () => !!sock,
    send,
    interrupt: () => send({ type: "interrupt" }),
    clear: () => { q("Screen").textContent = ""; },
    /** 绑定该实例所在面板的按钮与输入框（两个实例的 DOM 前缀不同） */
    bind() {
      q("Toggle").onclick = () => (sock ? stop() : start());
      q("Interrupt").onclick = () => send({ type: "interrupt" });
      q("Clear").onclick = () => { q("Screen").textContent = ""; };
      q("Screen").onclick = () => q("Input").focus();
      q("Input").onkeydown = (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        const line = q("Input").value;
        q("Input").value = "";
        send({ type: "input", data: line + "\n" });
      };
    },
  };
  return term;
}

// 抽屉「终端」tab 实例 + 「项目文件管理」页实例
const drawerTerm = createTerminal("sh", () => (current ? current.id : null), () => currentService);
const filesTerm = createTerminal("fmSh", () => fmCurrentAppId);

/* ---------------- 实时日志（WebSocket） ---------------- */
function wsUrl(appId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const svc = currentService ? `&service=${encodeURIComponent(currentService)}` : "";
  return `${proto}://${location.host}/ws/apps/${appId}/logs?tail=200${svc}`;
}

function appendLog(line, cls = "") {
  const box = $("#liveLogs");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  const span = document.createElement("span");
  span.textContent = line + "\n";
  if (cls) span.className = cls;
  box.appendChild(span);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function startWs() {
  if (ws || !current) return;
  ws = new WebSocket(wsUrl(current.id));
  $("#wsState").textContent = "连接中…";
  ws.onopen = () => { $("#wsDot").classList.add("on"); $("#wsState").textContent = "已连接"; $("#wsToggle").textContent = "断开"; };
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
    if (m.type === "open") appendLog(`—— 已连接服务 ${m.service || "default"} · 容器 ${m.container} ——`);
    else if (m.type === "log") appendLog(m.line);
    else if (m.type === "eof") { appendLog(`—— ${m.message} ——`); stopWs(); $("#wsState").textContent = "容器已停止"; }
    else if (m.type === "error") { appendLog("错误：" + m.message, "err"); stopWs(); $("#wsState").textContent = m.message; }
  };
  ws.onclose = () => { $("#wsDot").classList.remove("on"); $("#wsToggle").textContent = "连接"; ws = null; };
  ws.onerror = () => { $("#wsState").textContent = "连接失败（应用可能未部署）"; };
}

function stopWs() {
  if (ws) { ws.close(); ws = null; }
  $("#wsDot").classList.remove("on");
  $("#wsToggle").textContent = "连接";
  if ($("#wsState").textContent === "已连接") $("#wsState").textContent = "未连接";
}

/* ---------------- 构建日志 ---------------- */
async function loadBuildLogs() {
  if (!current) return;
  const box = $("#buildLogs");
  box.textContent = "加载中…";
  try {
    const rows = await api(`/apps/${current.id}/build/logs`);
    box.textContent = rows.length ? rows.map((r) => `[${r.ts}] ${r.log_line}`).join("\n") : "暂无构建日志（尚未构建）";
    box.scrollTop = box.scrollHeight;
  } catch (e) { box.textContent = "加载失败：" + e.message; }
}

/* ---------------- 负载曲线（SVG 适配浅色极简主题） ---------------- */
function drawChart(rows) {
  const svg = $("#metricsChart");
  const W = 640, H = 240, PAD = { t: 16, r: 16, b: 24, l: 38 };
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const parts = [];
  for (let p = 0; p <= 100; p += 25) {
    const y = PAD.t + (1 - p / 100) * ih;
    parts.push(`<line x1="${PAD.l}" y1="${y}" x2="${W - PAD.r}" y2="${y}" stroke="#e2e8f0" stroke-width="1"/>`);
    parts.push(`<text x="${PAD.l - 8}" y="${y + 4}" fill="#64748b" font-size="10" text-anchor="end">${p}%</text>`);
  }
  if (rows.length >= 2) {
    const n = rows.length;
    const x = (i) => PAD.l + (i / (n - 1)) * iw;
    const line = (key) => rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${(PAD.t + (1 - Math.min(r[key], 100) / 100) * ih).toFixed(1)}`).join(" ");
    const area = (key) =>
      `M${PAD.l},${PAD.t + ih} ` +
      rows.map((r, i) => `L${x(i).toFixed(1)},${(PAD.t + (1 - Math.min(r[key], 100) / 100) * ih).toFixed(1)}`).join(" ") +
      ` L${(W - PAD.r).toFixed(1)},${PAD.t + ih} Z`;
    parts.push(`<path d="${area("cpu")}" fill="rgba(2,132,199,.10)"/>`);
    parts.push(`<path d="${area("mem")}" fill="rgba(124,58,237,.08)"/>`);
    parts.push(`<path d="${line("cpu")}" fill="none" stroke="#0284c7" stroke-width="2"/>`);
    parts.push(`<path d="${line("mem")}" fill="none" stroke="#7c3aed" stroke-width="2"/>`);
    const last = rows[rows.length - 1];
    parts.push(`<text x="${W - PAD.r}" y="${PAD.t + 12}" fill="#0284c7" font-size="11" font-weight="600" text-anchor="end">CPU ${last.cpu}%</text>`);
    parts.push(`<text x="${W - PAD.r}" y="${PAD.t + 28}" fill="#7c3aed" font-size="11" font-weight="600" text-anchor="end">内存 ${last.mem}%</text>`);
  } else {
    parts.push(`<text x="${W / 2}" y="${H / 2}" fill="#64748b" font-size="13" text-anchor="middle">暂无采样数据（应用运行后自动采集）</text>`);
  }
  svg.innerHTML = parts.join("");
}

async function loadMetrics() {
  if (!current) return;
  try {
    const svc = currentService ? `&service=${encodeURIComponent(currentService)}` : "";
    const rows = await api(`/apps/${current.id}/metrics?limit=120${svc}`);
    drawChart(rows);
    $("#metricsHint").textContent = rows.length
      ? `${currentService || "公开服务"} · ${rows.length} 个采样点（每 ${5}s 一次）`
      : "";
  } catch (_) {}
}

/* ---------------- HTTP 就绪探针 ---------------- */
const READY_TEXT = { ready: "就绪", degraded: "服务异常", unready: "未就绪", unknown: "未知" };
const READY_CLASS = {
  ready: "st-running",
  degraded: "st-building",
  unready: "st-failed",
  unknown: "st-created",
};

async function loadReadiness() {
  if (!current) return;
  const appId = current.id;
  const cell = $("#readyCell");
  if (!cell) return;
  try {
    const r = await api(`/apps/${appId}/readiness`);
    if (!current || current.id !== appId) return; // 期间切换了应用，丢弃过期结果
    const cls = READY_CLASS[r.state] || "st-created";
    const extra = [];
    if (r.consecutive_failures) extra.push(`连续未就绪 ${r.consecutive_failures} 次`);
    if (r.restarts) extra.push(`自动重启 ${r.restarts} 次`);
    if (r.last_probe_at) extra.push(`最近探测 ${r.last_probe_at.slice(0, 19).replace("T", " ")}`);
    cell.innerHTML =
      `<span class="badge ${cls}">${READY_TEXT[r.state] || r.state}</span> ` +
      `<span class="muted">${esc(r.detail)}</span>` +
      (extra.length ? `<div class="field-hint">${esc(extra.join(" · "))}</div>` : "");
  } catch (_) {
    cell.innerHTML = '<span class="muted">就绪状态读取失败</span>';
  }
}

/* ---------------- 新建应用向导 ---------------- */
let wizardSrc = "upload";
let demoOptions = null; // /api/system/demos 内置示例包缓存
let demoOptionsPending = null;

const wizardConfirm = { port: false, mem: false, cpu: false };
const WZ_FIELDS = {
  port: { input: "#wzPort", btn: "#wzPortConfirm", hint: "#wzPortHint" },
  mem: { input: "#wzMem", btn: "#wzMemConfirm", hint: "#wzMemHint" },
  cpu: { input: "#wzCpu", btn: "#wzCpuConfirm", hint: "#wzCpuHint" },
};

function updateWizardSubmit() {
  const all = wizardConfirm.port && wizardConfirm.mem && wizardConfirm.cpu;
  const busy = $("#wizardSubmit").dataset.busy === "1";
  $("#wizardSubmit").disabled = !all || busy;
  const gate = $("#wizardGate");
  gate.classList.toggle("hidden", all);
}

function setWzFieldState(key, confirmed, level, message) {
  wizardConfirm[key] = confirmed;
  const cfg = WZ_FIELDS[key];
  const btn = $(cfg.btn);
  btn.textContent = confirmed ? "已确认 ✓" : "确认";
  btn.classList.toggle("confirmed", confirmed);
  const hint = $(cfg.hint);
  hint.textContent = message || "";
  hint.className = "field-hint" + (level ? " " + level : "");
  updateWizardSubmit();
}

function resetWizardConfirm() {
  Object.keys(WZ_FIELDS).forEach((k) => setWzFieldState(k, false, "", ""));
}

async function confirmWzField(key) {
  const port = parseInt($("#wzPort").value, 10);
  const mem = $("#wzMem").value.trim();
  const cpu = parseFloat($("#wzCpu").value);
  if (key === "port" && !Number.isFinite(port)) { setWzFieldState("port", false, "error", "请填写容器端口"); return; }
  if (key === "mem" && !mem) { setWzFieldState("mem", false, "error", "请填写内存上限"); return; }
  if (key === "cpu" && !Number.isFinite(cpu)) { setWzFieldState("cpu", false, "error", "请填写 CPU 核数"); return; }

  const btn = $(WZ_FIELDS[key].btn);
  btn.disabled = true;
  btn.textContent = "校验中…";
  try {
    const r = await api("/system/resource-check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        container_port: Number.isFinite(port) ? port : 0,
        mem_limit: mem || "0",
        cpu_quota: Number.isFinite(cpu) ? cpu : 0,
      }),
    });
    if (r && r.resources) poolInfo = r.resources;
    const res = r[key];
    const ok = res.level !== "error";
    setWzFieldState(key, ok, res.level, res.message);
    toast(res.message, res.level === "ok" ? "ok" : res.level === "error" ? "err" : "");
  } catch (e) {
    setWzFieldState(key, false, "error", "校验失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

function resetWizard() {
  wizardSrc = "upload";
  $("#wzName").value = "";
  $("#wzFile").value = "";
  $("#wzGit").value = "";
  $("#wzPort").value = "8000";
  $("#wzMem").value = "256m";
  $("#wzCpu").value = "1.0";
  $("#wzEnv").value = "";
  $("#wzCmd").value = "";
  $("#wzAutoDeploy").checked = true;
  $("#wzError").classList.add("hidden");
  $("#wzProgress").classList.add("hidden");
  $("#wzSteps").innerHTML = "";
  $("#wzLog").textContent = "";
  $("#wizardSubmit").dataset.busy = "0"; resetWizardConfirm();
  setWizardSource("upload");
}

/* 切换代码来源：同步分段控件高亮与对应输入区显隐 */
function setWizardSource(src) {
  wizardSrc = src;
  $$(".seg-item").forEach((s) => s.classList.toggle("active", s.dataset.src === src));
  $("#wzUploadBox").classList.toggle("hidden", src !== "upload");
  $("#wzGitBox").classList.toggle("hidden", src !== "git");
  $("#wzDemoBox").classList.toggle("hidden", src !== "demo");
  if (src === "demo") loadDemoOptions();
}

/* 拉取内置示例包列表并填充下拉（首次拉取后缓存） */
async function loadDemoOptions() {
  const sel = $("#wzDemo");
  if (!sel) return;
  if (!demoOptions) {
    if (!demoOptionsPending) {
      demoOptionsPending = api("/system/demos").catch(() => []).finally(() => { demoOptionsPending = null; });
    }
    demoOptions = (await demoOptionsPending) || [];
  }
  sel.innerHTML = demoOptions.length
    ? demoOptions.map((d) => `<option value="${esc(d.file)}">${esc(d.name)}</option>`).join("")
    : '<option value="">（未找到示例包）</option>';
}

function stepHtml(steps, activeIdx) {
  return steps.map((name, i) => {
    const cls = i < activeIdx ? "done" : i === activeIdx ? "active" : "";
    const mark = i < activeIdx ? "✓" : i + 1;
    return `<div class="step ${cls}"><span class="s-dot">${mark}</span>${name}</div>`;
  }).join("");
}

async function runWizard() {
  const name = $("#wzName").value.trim();
  if (!name) { showWzError("请填写应用名称"); return; }

  const srcLabel = wizardSrc === "git" ? "拉取 Git 仓库" : wizardSrc === "demo" ? "载入内置示例" : "上传代码包";
  const steps = ["创建应用", srcLabel, "构建镜像", "启动容器"];
  const setStep = (i) => { $("#wzSteps").innerHTML = stepHtml(steps, i); };
  const logln = (s) => { $("#wzLog").textContent += s + "\n"; $("#wzLog").scrollTop = $("#wzLog").scrollHeight; };

  $("#wizardSubmit").dataset.busy = "1";
  updateWizardSubmit();
  $("#wzError").classList.add("hidden");
  $("#wzProgress").classList.remove("hidden");

  try {
    setStep(0); logln("▶ 创建应用 " + name);
    const app = await api("/apps", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        source_type: wizardSrc === "git" ? "git" : "upload",
        git_url: wizardSrc === "git" ? $("#wzGit").value.trim() : null,
        container_port: parseInt($("#wzPort").value, 10) || 8000,
        mem_limit: $("#wzMem").value.trim() || "256m",
        cpu_quota: parseFloat($("#wzCpu").value) || 1.0,
        env: parseEnvLines($("#wzEnv").value),
        command: $("#wzCmd").value.trim() || null,
      }),
    });
    logln(`  应用已创建 (id=${app.id})`);

    if (wizardSrc === "upload") {
      setStep(1); logln("▶ 上传代码包…");
      const file = $("#wzFile").files[0];
      if (!file) throw new Error("请选择 .zip 代码包");
      const fd = new FormData();
      fd.append("file", file);
      await api(`/apps/${app.id}/source`, { method: "POST", body: fd });
      logln("  解压完成");
    } else if (wizardSrc === "git") {
      setStep(1); logln("▶ 克隆 Git 仓库…");
      const gitUrl = $("#wzGit").value.trim();
      if (!gitUrl) throw new Error("请填写 Git 地址");
      await api(`/apps/${app.id}/source/git`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ git_url: gitUrl }),
      });
      logln("  克隆完成");
    } else {
      setStep(1); logln("▶ 载入内置示例…");
      const demo = $("#wzDemo").value;
      if (!demo) throw new Error("请选择内置示例代码包");
      await api(`/apps/${app.id}/source/demo`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ demo }),
      });
      logln("  示例包已解压");
    }

    if (!$("#wzAutoDeploy").checked) {
      toast("应用已创建（未自动部署）", "ok");
      $("#wizardMask").classList.add("hidden");
      loadApps();
      return;
    }

    setStep(2); logln("▶ 构建镜像（可能需要 1-3 分钟）…");
    const dep = await api(`/apps/${app.id}/build`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    if (dep.build_status === "failed") throw new Error("镜像构建失败");
    logln("  镜像构建成功：" + dep.image_tag);
    if (dep.port_notice) {
      logln("  ⚙ " + dep.port_notice);
      toast(dep.port_notice.length > 120 ? "已按 compose / 自动识别配置构建，详见构建日志" : dep.port_notice);
    }

    const logs = await api(`/apps/${app.id}/build/logs`);
    logs.slice(-12).forEach((r) => logln("    " + r.log_line));

    setStep(3); logln("▶ 启动容器…");
    await api(`/apps/${app.id}/deploy`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    setStep(4); logln("✓ 完成！");
    toast("部署成功 🎉", "ok");
    await loadApps();
    setTimeout(() => {
      $("#wizardMask").classList.add("hidden");
      const created = apps.find((a) => a.id === app.id);
      if (created) openDrawer(created);
    }, 700);
  } catch (e) {
    showWzError(e.message);
    logln("✗ 失败：" + e.message);
  } finally {
    $("#wizardSubmit").dataset.busy = "0";
    updateWizardSubmit();
  }
}

function showWzError(msg) {
  const el = $("#wzError");
  el.textContent = msg;
  el.classList.remove("hidden");
}

/* ---------------- 配额调整（确认按钮 + 真实资源校验） ---------------- */
const quotaConfirm = { mem: false, cpu: false };
let quotaEditing = false;
const QUOTA_FIELDS = {
  mem: { input: "#quotaMem", btn: "#quotaMemConfirm", hint: "#quotaMemHint" },
  cpu: { input: "#quotaCpu", btn: "#quotaCpuConfirm", hint: "#quotaCpuHint" },
};

function updateQuotaSave() {
  $("#quotaSaveBtn").disabled = !(quotaConfirm.mem && quotaConfirm.cpu);
}

function setQuotaField(key, confirmed, level, message) {
  quotaConfirm[key] = confirmed;
  const cfg = QUOTA_FIELDS[key];
  const btn = $(cfg.btn);
  btn.textContent = confirmed ? "已确认 ✓" : "确认";
  btn.classList.toggle("confirmed", confirmed);
  const hint = $(cfg.hint);
  hint.textContent = message || "";
  hint.className = "field-hint" + (level ? " " + level : "");
  updateQuotaSave();
}

function resetQuotaConfirm() {
  Object.keys(QUOTA_FIELDS).forEach((k) => setQuotaField(k, false, "", ""));
}

async function confirmQuotaField(key) {
  if (!current) return;
  quotaEditing = true;
  const mem = $("#quotaMem").value.trim();
  const cpu = parseFloat($("#quotaCpu").value);
  if (key === "mem" && !mem) { setQuotaField("mem", false, "error", "请填写内存上限"); return; }
  if (key === "cpu" && !Number.isFinite(cpu)) { setQuotaField("cpu", false, "error", "请填写 CPU 核数"); return; }
  const btn = $(QUOTA_FIELDS[key].btn);
  btn.disabled = true; btn.textContent = "校验中…";
  try {
    const r = await checkResourceFields({
      mem_limit: mem || "0",
      cpu_quota: Number.isFinite(cpu) ? cpu : 0,
      app_id: current.id,
    });
    const res = r[key];
    setQuotaField(key, res.level !== "error", res.level, res.message);
    toast(res.message, res.level === "ok" ? "ok" : res.level === "error" ? "err" : "");
  } catch (e) {
    setQuotaField(key, false, "error", "校验失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- 配额保存 ---------------- */
async function saveQuota() {
  if (!current) return;
  if (!(quotaConfirm.mem && quotaConfirm.cpu)) {
    toast("请先确认内存与 CPU 配额", "err");
    return;
  }
  try {
    await api(`/apps/${current.id}/quota`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mem_limit: $("#quotaMem").value.trim(),
        cpu_quota: parseFloat($("#quotaCpu").value),
      }),
    });
    toast("配额已更新并生效", "ok");
    quotaEditing = false;
    await refreshCurrent();
  } catch (e) { toast("配额调整失败：" + e.message, "err"); }
}

/* ---------------- 视图 6：用户管理（仅管理员，全屏内嵌视图） ---------------- */
function rolePill(role) {
  const admin = role === "admin";
  return `<span class="role-pill ${admin ? "admin" : "user"}">${admin ? "管理员" : "普通用户"}</span>`;
}

function userRow(u) {
  const appCount = apps.filter((a) => a.owner_id === u.id).length;
  const isSelf = me && me.id === u.id;
  return `
  <tr data-id="${u.id}">
    <td><strong>${esc(u.username)}</strong>${isSelf ? ' <span class="muted">(我)</span>' : ""}</td>
    <td>${esc(u.display_name || "—")}</td>
    <td>${rolePill(u.role)}</td>
    <td><span class="state-pill ${u.is_active ? "on" : "off"}">${u.is_active ? "启用" : "已禁用"}</span></td>
    <td>${appCount}</td>
    <td class="muted">${u.last_login_at ? esc(u.last_login_at) : "从未"}</td>
    <td>
      <div class="u-actions">
        <button class="btn btn-ghost btn-sm" data-uact="name">改显示名</button>
        <button class="btn btn-ghost btn-sm" data-uact="role">${u.role === "admin" ? "降为用户" : "升为管理员"}</button>
        <button class="btn btn-ghost btn-sm" data-uact="toggle">${u.is_active ? "禁用" : "启用"}</button>
        <button class="btn btn-ghost btn-sm" data-uact="reset">重置密码</button>
        <button class="btn btn-danger btn-sm" data-uact="delete" ${isSelf ? "disabled" : ""}>删除</button>
      </div>
    </td>
  </tr>`;
}

function renderUsers() {
  const totalUsers = users.length;
  const adminUsers = users.filter((u) => u.role === "admin").length;
  const activeUsers = users.filter((u) => u.is_active).length;
  const disabledUsers = totalUsers - activeUsers;

  const statsEl = $("#userTopStats");
  if (statsEl) {
    statsEl.innerHTML = [
      ["用户总数", totalUsers],
      ["启用中账号", activeUsers],
      ["平台管理员", adminUsers],
      ["已禁用账号", disabledUsers],
    ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  }

  $("#usersBody").innerHTML = users.map(userRow).join("");
  $("#usersHint").textContent = `共 ${users.length} 个用户 · 管理员可跨租户管理全部用户与容器实例`;
}

async function loadUsers() {
  try {
    users = await api("/users");
    renderUsers();
  } catch (e) {
    toast("加载用户失败：" + e.message, "err");
  }
}

async function onUserAction(id, act) {
  const u = users.find((x) => x.id === id);
  if (!u) return;
  try {
    if (act === "name") {
      const dn = prompt(`输入「${u.username}」新的显示名`, u.display_name || "");
      if (dn === null || !dn.trim()) return;
      await api(`/users/${id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: dn.trim() }),
      });
      toast("显示名已更新", "ok");
    } else if (act === "role") {
      const next = u.role === "admin" ? "user" : "admin";
      if (!confirm(`确定将「${u.username}」${next === "admin" ? "升为管理员" : "降为普通用户"}？`)) return;
      await api(`/users/${id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: next }),
      });
      toast("角色已更新", "ok");
    } else if (act === "toggle") {
      if (!confirm(`确定${u.is_active ? "禁用" : "启用"}账号「${u.username}」？`)) return;
      await api(`/users/${id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !u.is_active }),
      });
      toast("状态已更新", "ok");
    } else if (act === "reset") {
      const pwd = prompt(`为「${u.username}」设置新密码（至少 8 位，含字母与数字）：`);
      if (!pwd) return;
      await api(`/users/${id}/password`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pwd }),
      });
      toast("密码已重置，该用户需重新登录", "ok");
    } else if (act === "delete") {
      if (!confirm(`确定删除用户「${u.username}」？其名下的应用将保留并转为无归属（仅管理员可见）。`)) return;
      await api(`/users/${id}`, { method: "DELETE" });
      toast("用户已删除", "ok");
    }
    await loadUsers();
    loadApps();
    loadAuth();
  } catch (e) {
    toast("操作失败：" + e.message, "err");
  }
}

function showUserForm() {
  $("#userFormBox").classList.remove("hidden");
  $("#ufUsername").value = "";
  $("#ufDisplay").value = "";
  $("#ufPassword").value = "";
  $("#ufRole").value = "user";
  $("#ufError").classList.add("hidden");
}

function hideUserForm() { $("#userFormBox").classList.add("hidden"); }

async function submitUserForm() {
  const username = $("#ufUsername").value.trim();
  const password = $("#ufPassword").value;
  const display_name = $("#ufDisplay").value.trim();
  const role = $("#ufRole").value;
  const err = $("#ufError");
  if (!username) { err.textContent = "请填写用户名"; err.classList.remove("hidden"); return; }
  if (!password) { err.textContent = "请填写密码"; err.classList.remove("hidden"); return; }
  try {
    await api("/users", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, display_name: display_name || null, role }),
    });
    toast("用户已创建", "ok");
    hideUserForm();
    await loadUsers();
  } catch (e) {
    err.textContent = e.message;
    err.classList.remove("hidden");
  }
}

/* ---------------- 视图 7：告警中心 ---------------- */
const ALERT_STATUS_TEXT = { active: "待处理", acked: "已确认", resolved: "已恢复" };
const ALERT_LEVEL_TEXT = { critical: "严重", warning: "警告" };

function startAlertPoll() {
  stopAlertPoll();
  alertTimer = setInterval(() => { if (activeView === "alerts") loadAlerts(); }, 30000);
}

function stopAlertPoll() {
  if (alertTimer) { clearInterval(alertTimer); alertTimer = null; }
}

async function loadAlerts() {
  const q = $("#alertStatusFilter").value;
  const level = $("#alertLevelFilter").value;
  const params = new URLSearchParams();
  if (q) params.set("status", q);
  if (level) params.set("level", level);
  const qs = params.toString();
  try {
    alertList = await api(`/alerts${qs ? "?" + qs : ""}`);
    renderAlerts();
    updateAlertBadge();
  } catch (_) { /* 401 已由 api() 跳转登录页 */ }
}

/* 侧边栏未处理告警角标 */
async function updateAlertBadge() {
  const badge = $("#alertsNavBadge");
  if (!badge) return;
  try {
    const active = await api("/alerts?status=active&limit=500");
    if (active.length) {
      badge.textContent = active.length > 99 ? "99+" : String(active.length);
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  } catch (_) { /* 忽略 */ }
}

function renderAlerts() {
  const rows = alertList;
  $("#alertStats").innerHTML = [
    ["待处理", rows.filter((a) => a.status === "active").length],
    ["已确认", rows.filter((a) => a.status === "acked").length],
    ["已恢复", rows.filter((a) => a.status === "resolved").length],
    ["严重级别", rows.filter((a) => a.level === "critical").length],
  ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  const body = $("#alertsBody");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="7"><div class="empty-inline">当前筛选条件下没有告警记录</div></td></tr>`;
    return;
  }
  body.innerHTML = rows.map((a) => {
    const handle = a.status === "resolved"
      ? `<span class="muted">已自动恢复</span>`
      : a.status === "acked"
        ? `<span class="muted">${esc(a.acked_by || "—")} 已确认</span>`
        : `<span class="muted">待处理</span>`;
    const act = a.status === "active"
      ? `<button class="btn btn-ghost btn-sm" data-aact="ack">确认</button>`
      : "";
    return `<tr data-id="${a.id}">
      <td><span class="badge alert-${esc(a.level)}">${esc(ALERT_LEVEL_TEXT[a.level] || a.level)}</span></td>
      <td>${esc(a.target_name)}<div class="cell-sub">${a.target_type === "host" ? "宿主机" : "应用"}</div></td>
      <td>${esc(a.message)}</td>
      <td><span class="badge alert-${esc(a.status)}">${esc(ALERT_STATUS_TEXT[a.status] || a.status)}</span></td>
      <td>${esc(fmtTime(a.created_at))}</td>
      <td>${handle}</td>
      <td style="text-align:right">${act}</td>
    </tr>`;
  }).join("");
}

async function onAlertAction(id, act) {
  if (act !== "ack") return;
  try {
    await api(`/alerts/${id}/ack`, { method: "POST" });
    toast("告警已确认", "ok");
    await loadAlerts();
  } catch (e) { toast("操作失败：" + e.message, "err"); }
}

/* ---------------- 视图 8：审计日志 ---------------- */
const AUDIT_ANSWER_PREVIEW = 4000; // 与后端 AUDIT_ANSWER_LIMIT 保持一致

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/* 把审计 detail（JSON 文本）压成 "k=v" 一行 */
function fmtDetail(detail) {
  if (!detail) return "—";
  try {
    const obj = JSON.parse(detail);
    const parts = Object.entries(obj).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : v}`);
    return parts.length ? parts.join(" · ") : "—";
  } catch (_) { return String(detail); }
}

async function ensureAuditActions() {
  if (auditActions) return;
  try { auditActions = await api("/audit/actions"); } catch (_) { auditActions = []; }
  const sel = $("#auditActionFilter");
  sel.innerHTML = '<option value="">全部动作</option>' +
    auditActions.map((a) => `<option value="${esc(a.action)}">${esc(a.label)}</option>`).join("");
}

async function loadAuditLogs() {
  await ensureAuditActions();
  const action = $("#auditActionFilter").value;
  const params = new URLSearchParams({ limit: "200" });
  if (action) params.set("action", action);
  if (auditQuery) params.set("username", auditQuery);
  try {
    auditLogs = await api(`/audit/logs?${params.toString()}`);
    renderAuditLogs();
  } catch (_) { /* 401 已由 api() 跳转登录页 */ }
}

function renderAuditLogs() {
  let rows = auditLogs;
  if (auditQuery) {
    const q = auditQuery.toLowerCase();
    rows = rows.filter((r) =>
      String(r.username || "").toLowerCase().includes(q) ||
      String(r.target_name || "").toLowerCase().includes(q));
  }
  $("#auditStats").innerHTML = [
    ["记录条数", rows.length],
    ["涉及操作人", new Set(rows.map((r) => r.username)).size],
    ["应用操作", rows.filter((r) => String(r.action).startsWith("app.")).length],
    ["用户管理", rows.filter((r) => String(r.action).startsWith("user.")).length],
  ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  const body = $("#auditBody");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="7"><div class="empty-inline">暂无审计记录</div></td></tr>`;
    return;
  }
  body.innerHTML = rows.map((r) => `<tr>
    <td>${esc(fmtTime(r.ts))}</td>
    <td>${esc(r.username || "—")}</td>
    <td><span class="tag">${esc(r.action_label || r.action)}</span></td>
    <td>${r.target_name ? esc(r.target_name) : "—"}<div class="cell-sub">${esc(r.target_type)}${r.target_id != null ? " #" + r.target_id : ""}</div></td>
    <td>${esc(fmtDetail(r.detail))}</td>
    <td>${esc(r.client_ip || "—")}</td>
    <td><button class="btn btn-ghost btn-sm" type="button" data-audit-id="${r.id}">详情</button></td>
  </tr>`).join("");
}

/* ---------------- 审计详情弹窗 ---------------- */
function parseAuditDetail(raw) {
  if (!raw) return {};
  try {
    const obj = JSON.parse(raw);
    return obj && typeof obj === "object" ? obj : { detail: raw };
  } catch (_) { return { detail: raw }; }
}

function adValue(value) {
  if (value == null || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function openAuditDetail(id) {
  const log = auditLogs.find((item) => item.id === id);
  if (!log) return;
  const detail = parseAuditDetail(log.detail);
  $("#auditDetailTitle").textContent = log.action_label || log.action || "操作记录";

  const meta = [
    ["时间", fmtTime(log.ts)],
    ["操作人", log.username || "—"],
    ["动作", `${log.action_label || log.action}${log.action ? "（" + log.action + "）" : ""}`],
    ["对象", [log.target_name, `${log.target_type}${log.target_id != null ? " #" + log.target_id : ""}`].filter(Boolean).join(" · ")],
    ["来源 IP", log.client_ip || "—"],
  ];
  const metaHtml = `<div class="ad-meta">${meta
    .map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`)
    .join("")}</div>`;

  let bodyHtml = metaHtml;
  if (detail.question || detail.answer) {
    const chips = [];
    if (detail.model) chips.push(`模型 ${detail.model}`);
    if (detail.scope_app) chips.push(`范围 ${detail.scope_app}`);
    else if (detail.scope) chips.push(detail.scope === "admin" ? "范围 全平台" : "范围 本人应用");
    if (detail.usage && detail.usage.total_tokens != null) {
      chips.push(
        `tokens ${detail.usage.input_tokens ?? "?"} 入 / ${detail.usage.output_tokens ?? "?"} 出 / ${detail.usage.total_tokens} 合计`,
      );
    }
    if (detail.elapsed_ms != null) chips.push(`${(detail.elapsed_ms / 1000).toFixed(1)}s`);
    bodyHtml += `<div class="ad-block">
      <div class="ad-label">提问</div>
      <div class="ad-text">${esc(detail.question || "—")}</div>
    </div>
    <div class="ad-block">
      <div class="ad-label">输出</div>
      <div class="ad-text md ai-text">${renderAiMarkdown(detail.answer || "—")}</div>
      ${detail.truncated ? `<div class="ad-note">输出过长，仅保留前 ${AUDIT_ANSWER_PREVIEW} 字，完整内容请查看后端日志。</div>` : ""}
    </div>`;
    if (chips.length) bodyHtml += `<div class="ad-chips">${chips.map((c) => `<span class="ad-chip">${esc(c)}</span>`).join("")}</div>`;
  } else {
    const entries = Object.entries(detail);
    bodyHtml += entries.length
      ? `<table class="ad-kv"><tbody>${entries
          .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(adValue(v))}</td></tr>`)
          .join("")}</tbody></table>`
      : `<div class="empty-inline">该操作没有附加详情</div>`;
  }

  $("#auditDetailBody").innerHTML = bodyHtml;
  $("#auditDetailMask").classList.remove("hidden");
}

function closeAuditDetail() {
  $("#auditDetailMask").classList.add("hidden");
}

/* ---------------- 应用配置（环境变量 / 启动命令） ---------------- */
function parseEnvLines(text) {
  const out = {};
  (text || "").split("\n").forEach((line) => {
    const s = line.trim();
    if (!s || s.startsWith("#")) return;
    const i = s.indexOf("=");
    if (i <= 0) return;
    out[s.slice(0, i).trim()] = s.slice(i + 1).trim();
  });
  return out;
}

function envToLines(env) {
  return Object.entries(env || {}).map(([k, v]) => `${k}=${v}`).join("\n");
}

async function saveAppConfig() {
  if (!current) return;
  const btn = $("#cfgSaveBtn");
  btn.disabled = true;
  try {
    current = await api(`/apps/${current.id}/config`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        env: parseEnvLines($("#cfgEnv").value),
        command: $("#cfgCmd").value.trim() || null,
      }),
    });
    cfgEditing = false;
    renderOverview();
    toast("配置已保存，重新部署后生效", "ok");
  } catch (e) {
    toast("保存失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- 视图 9：项目文件管理（源码目录 / 容器文件） ----------------
 * 一套组件（fmMarkup + mountFileManager）同时挂载到两处：
 *   page   侧栏「项目文件管理」页面（项目由顶部上下文栏 + 选择浮层切换）
 *   drawer 应用详情抽屉的「文件」tab（应用即当前抽屉应用）
 * DOM 通过 data-fm-* 标注，事件在挂载根上委托一次，避免重复绑定。
 */
const FM_TARGET_LABEL = { source: "项目源码", container: "容器文件" };
const FM_TARGET_TIP = {
  source: "改动写入项目源码目录，需「重新构建并部署」后新版本才生效。",
  container: "改动直接作用于运行中的容器，立即生效；重新部署后恢复为镜像内容。",
};
const FM_FOLDER_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7a2 2 0 0 1 2-2h3.6l1.8 2.2H18a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7z"/></svg>';
const FM_FILE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h8l4 4v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M14 3v4h4"/></svg>';

const fmInstances = { page: null, drawer: null };
let fmDownloadFrame = null;   // 复用隐藏 iframe 触发下载，失败响应不会顶掉当前页面
let editorCtx = null;         // 编辑器上下文：{ inst, path, truncated }

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}

function fmMarkup() {
  return `
    <div class="fm-toolbar">
      <div class="seg fm-seg" data-fm="seg">
        <button class="seg-item active" data-fm-target="source" type="button">项目源码</button>
        <button class="seg-item" data-fm-target="container" type="button">容器文件</button>
      </div>
      <span class="fm-status" data-fm="status"></span>
      <span class="fm-spacer"></span>
      <button class="btn btn-ghost btn-sm" data-fm-act="rebuild" type="button">重新构建并部署</button>
      <button class="btn btn-ghost btn-sm" data-fm-act="mkdir" type="button">新建文件夹</button>
      <button class="btn btn-ghost btn-sm" data-fm-act="dirzip" type="button">打包下载目录</button>
      <button class="btn btn-primary btn-sm" data-fm-act="upload" type="button">上传文件</button>
      <input type="file" multiple hidden data-fm="file" />
    </div>
    <div class="fm-crumbs" data-fm="crumbs"></div>
    <div class="fm-tip" data-fm="tip"></div>
    <div class="fm-list" data-fm="list"></div>`;
}

/* 挂载（已挂载则复用，仅刷新 ctx 引用） */
function mountFileManager(mountEl, key, ctx) {
  let inst = fmInstances[key];
  if (!inst || !mountEl.dataset.fmBound) {
    mountEl.innerHTML = fmMarkup();
    inst = {
      key, root: mountEl, ctx,
      target: "source", path: "", parent: null,
      entries: [], targets: {}, caps: {}, truncated: false,
      loading: false, loaded: false, lastAppId: null,
    };
    fmInstances[key] = inst;
    fmBind(inst);
    mountEl.dataset.fmBound = "1";
  } else {
    inst.ctx = ctx;
  }
  return inst;
}

function fmBind(inst) {
  const root = inst.root;
  root.addEventListener("click", (e) => {
    const targetBtn = e.target.closest("[data-fm-target]");
    if (targetBtn) return fmSetTarget(inst, targetBtn.dataset.fmTarget);
    const crumb = e.target.closest("[data-fm-crumb]");
    if (crumb) return fmEnter(inst, crumb.dataset.fmCrumb);
    if (e.target.closest("[data-fm-up]")) return fmEnter(inst, inst.parent || "");
    const rowBtn = e.target.closest("button[data-fm-row]");
    if (rowBtn) {
      const entry = inst.entries.find((x) => x.path === rowBtn.dataset.fmEntry);
      if (!entry) return;
      const act = rowBtn.dataset.fmRow;
      if (act === "enter") fmEnter(inst, entry.path);
      else if (act === "edit") fmEdit(inst, entry);
      else if (act === "download") fmDownload(inst, entry);
      else if (act === "rename") fmRename(inst, entry);
      else if (act === "delete") fmDelete(inst, entry);
      return;
    }
    const actBtn = e.target.closest("button[data-fm-act]");
    if (!actBtn) return;
    const act = actBtn.dataset.fmAct;
    if (act === "mkdir") fmMkdir(inst);
    else if (act === "upload") root.querySelector('[data-fm="file"]').click();
    else if (act === "dirzip") fmDownload(inst, null);
    else if (act === "rebuild") fmRebuild(inst);
  });
  root.addEventListener("change", (e) => {
    if (!e.target.matches('[data-fm="file"]')) return;
    const files = [...e.target.files];
    e.target.value = "";
    if (files.length) fmUpload(inst, files);
  });
}

const fmEl = (inst, name) => inst.root.querySelector(`[data-fm="${name}"]`);

/* 容器目标才需要服务名；源码目录与具体服务无关 */
function fmService(inst) {
  if (!inst || inst.target !== "container") return null;
  return inst.ctx.getService ? inst.ctx.getService() : null;
}

function fmTargetLabel(inst, target) {
  const state = inst.targets[target];
  return (state && state.label) || FM_TARGET_LABEL[target] || target;
}

/* ---------------- 加载与渲染 ---------------- */

async function fmLoad(inst) {
  const appId = inst.ctx.getAppId();
  if (!appId) {
    fmEl(inst, "list").innerHTML = '<div class="empty-inline">请先选择应用</div>';
    return;
  }
  if (inst.loading) return;
  inst.loading = true;
  fmEl(inst, "list").innerHTML = '<div class="empty-inline">加载中…</div>';
  const q = new URLSearchParams({ target: inst.target, path: inst.path || "" });
  const svc = fmService(inst);
  if (svc) q.set("service", svc);
  try {
    const data = await api(`/apps/${appId}/files?${q.toString()}`);
    inst.entries = data.entries || [];
    inst.path = data.path || "";
    inst.parent = data.parent;
    inst.targets = data.targets || {};
    inst.caps = {
      max_read_kb: data.max_read_kb,
      max_upload_mb: data.max_upload_mb,
      max_archive_mb: data.max_archive_mb,
    };
    inst.truncated = !!data.truncated;
    inst.loaded = true;
    fmRender(inst);
  } catch (e) {
    inst.entries = [];
    inst.loaded = true;
    inst.truncated = false;
    const msg = await fmDescribeError(inst, appId, e.message);
    fmRender(inst);
    fmEl(inst, "list").innerHTML = `<div class="empty-inline">${esc(msg)}</div>`;
  } finally {
    inst.loading = false;
  }
}

/* 目标不可用时（如容器未运行）列表接口会直接报错，
 * 此时用另一个目标的响应补齐「哪个目标可用」的快照，避免分段控件误判。 */
async function fmDescribeError(inst, appId, message) {
  if (!Object.keys(inst.targets).length) {
    const other = inst.target === "source" ? "container" : "source";
    try {
      const snap = await api(`/apps/${appId}/files?target=${other}&path=`);
      inst.targets = snap.targets || inst.targets;
    } catch (_) { /* 两个目标都不通，保持原样 */ }
  }
  if (inst.target === "source" && !inst.path && /目录不存在/.test(message)) {
    return "源码目录还是空的，可直接上传文件，或前往「应用实例」上传代码包。";
  }
  return message;
}

function fmRender(inst) {
  const state = inst.targets[inst.target];
  const ready = !state || state.ready;

  inst.root.querySelectorAll("[data-fm-target]").forEach((btn) => {
    const t = btn.dataset.fmTarget;
    const st = inst.targets[t];
    const ok = !st || st.ready;
    btn.classList.toggle("active", t === inst.target);
    btn.disabled = !ok;
    btn.title = ok ? fmTargetLabel(inst, t) : st.reason || "";
    btn.textContent = fmTargetLabel(inst, t);
  });

  const tip = fmEl(inst, "tip");
  const notes = [FM_TARGET_TIP[inst.target]];
  if (!ready && state && state.reason) notes.push(state.reason);
  if (inst.truncated) notes.push("目录内容较多，仅显示部分条目。");
  if (inst.caps.max_upload_mb) {
    notes.push(`单文件上限 ${inst.caps.max_upload_mb}MB，在线编辑上限 ${inst.caps.max_read_kb}KB，打包下载上限 ${inst.caps.max_archive_mb}MB。`);
  }
  tip.textContent = notes.filter(Boolean).join(" ");
  tip.className = "fm-tip" + (ready ? "" : " warn");

  fmEl(inst, "crumbs").innerHTML = fmCrumbs(inst);
  fmEl(inst, "status").textContent = inst.loaded
    ? (ready ? `${inst.entries.length} 项` : "不可用")
    : "";
  fmEl(inst, "list").innerHTML = fmRows(inst, ready);
}

function fmCrumbs(inst) {
  const parts = (inst.path || "").split("/").filter(Boolean);
  const acc = [];
  const crumbs = ['<button class="fm-crumb' + (parts.length ? "" : " current") + '" data-fm-crumb="" type="button">根目录</button>'];
  parts.forEach((seg, i) => {
    acc.push(seg);
    const isLast = i === parts.length - 1;
    crumbs.push(`<span class="fm-sep">/</span>`);
    crumbs.push(`<button class="fm-crumb${isLast ? " current" : ""}" data-fm-crumb="${esc(acc.join("/"))}" type="button">${esc(seg)}</button>`);
  });
  return `<span class="fm-loc">位置</span>${crumbs.join("")}`;
}

function fmRows(inst, ready) {
  if (!ready) return '<div class="empty-inline">当前目标不可用</div>';
  if (!inst.entries.length && !inst.path) return '<div class="empty-inline">该目录为空</div>';
  const rows = [];
  if (inst.path) {
    rows.push(`<div class="fm-row up"><span class="fm-ic dir">${FM_FOLDER_SVG}</span>
      <button class="fm-name" data-fm-up="1" type="button">.. 返回上一级</button></div>`);
  }
  rows.push(...inst.entries.map((en) => {
    const acts = en.is_dir
      ? [`download|打包下载`, `rename|重命名`, `delete|删除`]
      : [`edit|编辑`, `download|下载`, `rename|重命名`, `delete|删除`];
    const btns = acts
      .map((a) => {
        const [act, label] = a.split("|");
        return `<button class="btn btn-ghost btn-xs" data-fm-row="${act}" data-fm-entry="${esc(en.path)}" type="button">${label}</button>`;
      })
      .join("");
    const name = en.is_dir
      ? `<button class="fm-name" data-fm-row="enter" data-fm-entry="${esc(en.path)}" type="button">${esc(en.name)}</button>`
      : `<span class="fm-name static">${esc(en.name)}</span>`;
    const meta = en.is_dir ? "目录" : fmtBytes(en.size);
    return `<div class="fm-row">
      <span class="fm-ic ${en.is_dir ? "dir" : "file"}">${en.is_dir ? FM_FOLDER_SVG : FM_FILE_SVG}</span>
      <span class="fm-main">${name}<span class="fm-meta">${meta}${en.mtime ? " · " + esc(en.mtime) : ""}</span></span>
      <span class="fm-acts">${btns}</span>
    </div>`;
  }));
  return rows.join("");
}

/* ---------------- 操作 ---------------- */

function fmSetTarget(inst, target) {
  if (!FM_TARGET_LABEL[target] || target === inst.target) return;
  inst.target = target;
  inst.path = "";
  fmLoad(inst);
}

function fmEnter(inst, path) {
  inst.path = path || "";
  fmLoad(inst);
}

function fmOpUrl(inst, path) {
  const q = new URLSearchParams({ target: inst.target, path: path || "" });
  const svc = fmService(inst);
  if (svc) q.set("service", svc);
  return `${API}/apps/${inst.ctx.getAppId()}/files/download?${q.toString()}`;
}

function fmDownload(inst, entry) {
  if (!inst.ctx.getAppId()) return;
  if (!fmDownloadFrame) {
    fmDownloadFrame = document.createElement("iframe");
    fmDownloadFrame.style.display = "none";
    fmDownloadFrame.setAttribute("aria-hidden", "true");
    document.body.appendChild(fmDownloadFrame);
  }
  fmDownloadFrame.src = fmOpUrl(inst, entry ? entry.path : inst.path);
  toast(entry && !entry.is_dir ? `正在下载 ${entry.name}` : "正在打包下载当前目录…");
}

async function fmFileOp(inst, path, op, newName) {
  try {
    const r = await api(`/apps/${inst.ctx.getAppId()}/files`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: inst.target, path, op, new_name: newName || null, service: fmService(inst) }),
    });
    toast(r.message || "操作完成", "ok");
    fmLoad(inst);
  } catch (e) {
    toast("操作失败：" + e.message, "err");
  }
}

function fmMkdir(inst) {
  const name = (prompt("新建文件夹名称：") || "").trim();
  if (!name) return;
  fmFileOp(inst, inst.path ? `${inst.path}/${name}` : name, "mkdir");
}

function fmRename(inst, entry) {
  const name = (prompt(`将「${entry.name}」重命名为：`, entry.name) || "").trim();
  if (!name || name === entry.name) return;
  fmFileOp(inst, entry.path, "rename", name);
}

function fmDelete(inst, entry) {
  const extra = entry.is_dir ? "及其全部内容" : "";
  if (!confirm(`确定删除「${entry.name}」${extra}？该操作不可恢复。`)) return;
  fmFileOp(inst, entry.path, "delete");
}

async function fmUpload(inst, files) {
  const appId = inst.ctx.getAppId();
  if (!appId) return;
  const fd = new FormData();
  fd.append("target", inst.target);
  fd.append("path", inst.path || "");
  const svc = fmService(inst);
  if (svc) fd.append("service", svc);
  files.forEach((f) => fd.append("files", f));
  toast(`正在上传 ${files.length} 个文件…`);
  try {
    const r = await api(`/apps/${appId}/files/upload`, { method: "POST", body: fd });
    toast(r.message || "上传完成", "ok");
    fmLoad(inst);
  } catch (e) {
    toast("上传失败：" + e.message, "err");
  }
}

async function fmEdit(inst, entry) {
  try {
    const q = new URLSearchParams({ target: inst.target, path: entry.path });
    const svc = fmService(inst);
    if (svc) q.set("service", svc);
    const data = await api(`/apps/${inst.ctx.getAppId()}/files/content?${q.toString()}`);
    if (!data.is_text) {
      toast("该文件不是文本文件，无法在线编辑，请下载后修改", "err");
      return;
    }
    openEditor(inst, entry.path, data);
  } catch (e) {
    toast("打开失败：" + e.message, "err");
  }
}

/* 一键重新构建并部署：镜像重建后保持原宿主端口与访问地址 */
async function fmRebuild(inst) {
  const appId = inst.ctx.getAppId();
  if (!appId) return;
  if (!confirm("将重新构建镜像并用新镜像重建容器（保持当前宿主端口与访问地址），确定继续？")) return;
  toast("正在重新构建并部署…");
  try {
    const dep = await api(`/apps/${appId}/build`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    if (dep.build_status === "failed") throw new Error("构建失败，请查看构建日志");
    if (dep.port_notice) toast(dep.port_notice);
    const a = apps.find((x) => x.id === appId);
    if (a && a.status === "running") await api(`/apps/${appId}/stop`, { method: "POST" });
    const deployed = await api(`/apps/${appId}/deploy`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reassign_port: false }),
    });
    toast(`已重新构建并部署${deployed && deployed.host_port ? `（宿主端口 ${deployed.host_port}）` : ""}`, "ok");
    await loadApps();
    if (current && current.id === appId) await refreshCurrent();
    fmLoad(inst);
  } catch (e) {
    toast("重新构建失败：" + e.message, "err");
  }
}

/* ---------------- 两处宿主 ---------------- */

/* 页面版当前项目：模块级变量，由顶部项目上下文栏与选择浮层共同维护 */
let fmCurrentAppId = null;
const fmPageCtx = { getAppId: () => fmCurrentAppId };
const fmDrawerCtx = {
  getAppId: () => (current ? current.id : null),
  getService: () => currentService,
};

function fmAppSummary(a) {
  const src = a.source_type === "git" ? "Git 源码" : "上传包";
  const port = a.host_port ? `宿主端口 ${a.host_port}` : "未分配端口";
  return `#${a.id} · ${port} · ${src}`;
}

function fmRenderCtx() {
  const app = apps.find((a) => a.id === fmCurrentAppId);
  $("#fmCtxName").textContent = app ? app.name : "暂无可管理的项目";
  $("#fmCtxBadge").innerHTML = app ? statusBadge(app.status) : "";
  $("#fmCtxSub").textContent = app
    ? fmAppSummary(app) + (app.access_url ? ` · ${app.access_url}` : "")
    : "请先在「应用实例」页新建应用";
}

function fmRenderPicker() {
  $("#fmPickerCount").textContent = `共 ${apps.length} 个`;
  $("#fmPickerList").innerHTML = apps
    .map(
      (a) => `
      <button class="fm-pick-row${a.id === fmCurrentAppId ? " active" : ""}" data-fm-pick="${a.id}"
              type="button" role="option" aria-selected="${a.id === fmCurrentAppId}">
        <span class="fm-pick-main">
          <span class="fm-pick-name">${esc(a.name)}</span>
          <span class="fm-pick-sub">${esc(fmAppSummary(a))}</span>
        </span>
        ${statusBadge(a.status)}
        <span class="fm-pick-check" aria-hidden="true">✓</span>
      </button>`
    )
    .join("");
}

function openFmPicker() {
  if (!apps.length) return;
  fmRenderPicker();
  $("#fmPicker").classList.remove("hidden");
  $("#fmSwitchBtn").setAttribute("aria-expanded", "true");
}

function closeFmPicker() {
  $("#fmPicker").classList.add("hidden");
  $("#fmSwitchBtn").setAttribute("aria-expanded", "false");
}

function toggleFmPicker() {
  if ($("#fmPicker").classList.contains("hidden")) openFmPicker();
  else closeFmPicker();
}

function renderFilePage(force = false) {
  const mount = $("#filesPageMount");
  if (!apps.length) {
    fmInstances.page = null;
    delete mount.dataset.fmBound;
    fmCurrentAppId = null;
    closeFmPicker();
    fmRenderCtx();
    mount.innerHTML = '<div class="empty-inline">还没有应用，请先在「应用实例」页新建应用。</div>';
    return;
  }
  // 当前项目失效（被删除或首次进入）时回落到列表首个项目
  if (!apps.some((a) => a.id === fmCurrentAppId)) {
    fmCurrentAppId = apps[0].id;
    force = true;
  }
  fmRenderCtx();
  if (!$("#fmPicker").classList.contains("hidden")) fmRenderPicker();

  const inst = mountFileManager(mount, "page", fmPageCtx);
  if (force || inst.lastAppId !== String(fmCurrentAppId)) {
    inst.lastAppId = String(fmCurrentAppId);
    inst.target = "source";
    inst.path = "";
    fmLoad(inst);
  }
}

function renderDrawerFiles(force = false) {
  if (!current) return;
  const mount = $("#filesDrawerMount");
  const inst = mountFileManager(mount, "drawer", fmDrawerCtx);
  if (force || inst.lastAppId !== String(current.id)) {
    inst.lastAppId = String(current.id);
    inst.target = "source";
    inst.path = "";
    fmLoad(inst);
  }
}

function resetDrawerFiles() {
  const inst = fmInstances.drawer;
  fmInstances.drawer = null;
  const mount = $("#filesDrawerMount");
  delete mount.dataset.fmBound;
  mount.innerHTML = "";
  if (inst) inst.loading = false;
}

/* ---------------- 文本文件编辑器 ---------------- */

function openEditor(inst, path, data) {
  editorCtx = { inst, path, truncated: !!data.truncated };
  $("#editorPath").textContent = `${fmTargetLabel(inst, inst.target)} · ${path}`;
  const hint = data.truncated
    ? `文件较大，仅载入前 ${inst.caps.max_read_kb}KB，保存将以当前可见内容覆盖`
    : `${fmtBytes(data.size)} · UTF-8 文本`;
  $("#editorHint").textContent = hint;
  $("#editorText").value = data.content || "";
  $("#editorMask").classList.remove("hidden");
  setTimeout(() => $("#editorText").focus(), 40);
}

function closeEditor() {
  $("#editorMask").classList.add("hidden");
  editorCtx = null;
}

async function saveEditor() {
  if (!editorCtx) return;
  const { inst, path } = editorCtx;
  const btn = $("#editorSaveBtn");
  btn.disabled = true;
  try {
    const r = await api(`/apps/${inst.ctx.getAppId()}/files/content`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: inst.target, path, content: $("#editorText").value, service: fmService(inst) }),
    });
    toast(r.message || "文件已保存", "ok");
    closeEditor();
    fmLoad(inst);
  } catch (e) {
    toast("保存失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- AI Markdown 渲染（无外部依赖，先转义再解析） ---------------- */
function aiInlineMarkdown(text) {
  let s = text;
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (all, label, url) => {
    const safe = /^https?:\/\//i.test(url) || /^\/(?!\/)/.test(url);
    return safe
      ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
      : all;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  // 模型偶尔输出未闭合的 ** ，清掉残留标记，避免把星号直接显示给用户
  s = s.replace(/\*\*/g, "");
  return s;
}

function aiTableSeparator(line) {
  const text = String(line || "").trim();
  if (!text || !text.includes("-")) return false;
  const cells = text.replace(/^\|/, "").replace(/\|$/, "").split("|");
  return cells.length > 0 && cells.every((cell) => /^\s*:?-{2,}:?\s*$/.test(cell));
}

function aiSplitRow(line) {
  let text = String(line || "").trim();
  if (text.startsWith("|")) text = text.slice(1);
  if (text.endsWith("|")) text = text.slice(0, -1);
  return text.split("|").map((cell) => cell.trim());
}

function aiIsBlockStart(line, nextLine) {
  const text = String(line || "").trim();
  if (!text) return true;
  if (/^#{1,6}\s+/.test(text)) return true;
  if (/^>\s?/.test(text) || /^&gt;\s?/.test(text)) return true;
  if (/^[-*+]\s+/.test(text)) return true;
  if (/^\d+\.\s+/.test(text)) return true;
  if (/^(-{3,}|\*{3,}|_{3,})$/.test(text)) return true;
  if (/^\u0000MD\d+\u0000$/.test(text)) return true;
  if (text.includes("|") && aiTableSeparator(nextLine)) return true;
  return false;
}

function renderAiMarkdown(text) {
  const tokens = [];
  const stash = (html) => {
    const key = `\u0000MD${tokens.length}\u0000`;
    tokens.push(html);
    return key;
  };

  let source = String(text ?? "").replace(/\r\n?/g, "\n");
  // 围栏代码块：自带语言 class，内容单独转义
  source = source.replace(/^```([^\n`]*)\n([\s\S]*?)\n?```[ \t]*$/gm, (_all, lang, code) => {
    const language = String(lang || "").trim();
    const cls = language ? ` class="language-${esc(language)}"` : "";
    return stash(`<pre class="ai-code"><code${cls}>${esc(code.replace(/\n$/, ""))}</code></pre>`);
  });
  // 行内代码：先占位，避免内部 * / _ 等被当作强调
  source = source.replace(/`([^`\n]+)`/g, (_all, code) => stash(`<code>${esc(code)}</code>`));
  source = esc(source);

  const lines = source.split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i].trim();
    if (!line) {
      i += 1;
      continue;
    }
    if (/^\u0000MD\d+\u0000$/.test(line)) {
      out.push(line);
      i += 1;
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) {
      out.push("<hr>");
      i += 1;
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      const level = Math.min(heading[1].length + 2, 6);
      out.push(`<h${level}>${aiInlineMarkdown(heading[2].trim())}</h${level}>`);
      i += 1;
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length && aiTableSeparator(lines[i + 1])) {
      const header = aiSplitRow(lines[i]);
      const aligns = aiSplitRow(lines[i + 1]).map((cell) =>
        /^:-+:$/.test(cell.replace(/\s/g, ""))
          ? "center"
          : /-+:$/.test(cell.replace(/\s/g, ""))
            ? "right"
            : "left",
      );
      i += 2;
      const rows = [];
      while (i < lines.length) {
        const row = lines[i].trim();
        if (!row || !row.includes("|")) break;
        rows.push(aiSplitRow(lines[i]));
        i += 1;
      }
      const head = header
        .map(
          (cell, idx) =>
            `<th style="text-align:${aligns[idx] || "left"}">${aiInlineMarkdown(cell)}</th>`,
        )
        .join("");
      const body = rows
        .map(
          (row) =>
            `<tr>${header
              .map(
                (_ignored, idx) =>
                  `<td style="text-align:${aligns[idx] || "left"}">${aiInlineMarkdown(
                    row[idx] || "",
                  )}</td>`,
              )
              .join("")}</tr>`,
        )
        .join("");
      out.push(
        `<div class="ai-table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`,
      );
      continue;
    }

    if (/^>\s?/.test(line) || /^&gt;\s?/.test(line)) {
      const quoted = [];
      while (
        i < lines.length &&
        (/^>\s?/.test(lines[i].trim()) || /^&gt;\s?/.test(lines[i].trim()))
      ) {
        quoted.push(lines[i].trim().replace(/^(?:>|&gt;)\s?/, ""));
        i += 1;
      }
      out.push(`<blockquote>${quoted.map(aiInlineMarkdown).join("<br>")}</blockquote>`);
      continue;
    }

    if (/^[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*+]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*+]\s+/, ""));
        i += 1;
      }
      out.push(`<ul>${items.map((item) => `<li>${aiInlineMarkdown(item)}</li>`).join("")}</ul>`);
      continue;
    }

    if (/^\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^\d+\.\s+/, ""));
        i += 1;
      }
      out.push(`<ol>${items.map((item) => `<li>${aiInlineMarkdown(item)}</li>`).join("")}</ol>`);
      continue;
    }

    const paragraph = [];
    while (i < lines.length) {
      const current = lines[i].trim();
      if (!current) break;
      if (paragraph.length && aiIsBlockStart(current, lines[i + 1])) break;
      paragraph.push(current);
      i += 1;
    }
    out.push(`<p>${paragraph.map(aiInlineMarkdown).join("<br>")}</p>`);
  }

  return out
    .join("")
    .replace(/\u0000MD(\d+)\u0000/g, (_all, idx) => tokens[Number(idx)] ?? "");
}

/* ---------------- 视图：AI 运行分析 ---------------- */
let aiMessages = [];
let aiBusy = false;
let aiHealthState = null;
let aiHealthLoading = false;

const AI_EMPTY_HINT = "选择分析范围后提问，AI 会基于当前平台快照给出只读分析与建议。";

function renderAiView() {
  renderAiAppOptions();
  loadAiHealth();
  renderAiMessages();
}

function renderAiAppOptions() {
  const sel = $("#aiApp");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML =
    '<option value="">全平台</option>' +
    apps
      .map((a) => {
        const extra = a.status === "running" ? "" : ` · ${STATUS_TEXT[a.status] || a.status}`;
        return `<option value="${a.id}">${esc(a.name + extra)}</option>`;
      })
      .join("");
  sel.value = apps.some((a) => String(a.id) === prev) ? prev : "";
}

function aiReady() {
  return !!(aiHealthState && aiHealthState.enabled && aiHealthState.configured);
}

function updateAiControls() {
  const ready = aiReady();
  const input = $("#aiInput");
  const send = $("#aiSend");
  if (!input || !send) return;
  input.disabled = !ready || aiBusy;
  send.disabled = !ready || aiBusy;
  const scope = $("#aiApp");
  const clear = $("#aiClear");
  if (scope) scope.disabled = aiBusy;
  if (clear) clear.disabled = aiBusy;
  $$("#aiSuggest .ai-chip").forEach((btn) => (btn.disabled = !ready || aiBusy));
  if (ready) {
    input.placeholder = "例如：哪些应用需要优先处理？为什么？";
  } else {
    input.placeholder = (aiHealthState && aiHealthState.hint) || "AI 未启用，请在后端 .env 中配置 AI_* 后重启";
  }
}

async function loadAiHealth(force = false) {
  if (aiHealthLoading || (aiHealthState && !force)) {
    updateAiControls();
    return;
  }
  aiHealthLoading = true;
  try {
    aiHealthState = await api("/ai/health");
  } catch (e) {
    aiHealthState = { enabled: false, configured: false, hint: "AI 配置检查失败：" + e.message };
  } finally {
    aiHealthLoading = false;
  }
  const hint = $("#aiHealth");
  if (hint) {
    const h = aiHealthState || {};
    if (!h.enabled) hint.textContent = h.hint || "AI 未开启";
    else if (!h.configured) hint.textContent = h.hint || "AI 参数不完整";
    else hint.textContent = `已就绪 · ${h.model || "默认模型"} · 每分钟最多 ${h.rate_limit_per_min || "不限"} 次`;
    hint.classList.toggle("warn", !(h.enabled && h.configured));
  }
  updateAiControls();
  renderAiMessages();
}

function aiResultMeta(result) {
  const bits = [];
  if (result.model) bits.push(result.model);
  if (result.elapsed_ms != null) bits.push(`${(result.elapsed_ms / 1000).toFixed(1)}s`);
  const summary = result.context_summary || {};
  if (summary.apps_total != null) bits.push(`${summary.apps_total} 个应用`);
  if (summary.open_alerts != null) bits.push(`${summary.open_alerts} 条未消除告警`);
  if (summary.target_app && summary.target_app.name) bits.push(`范围：${summary.target_app.name}`);
  return bits.join(" · ");
}

function renderAiMessages() {
  const box = $("#aiChat");
  if (!box) return;
  if (!aiMessages.length && !aiBusy) {
    const hint = aiReady()
      ? AI_EMPTY_HINT
      : (aiHealthState && aiHealthState.hint) || "AI 未启用，请在后端 .env 中配置 AI_* 后重启";
    box.innerHTML = `<div class="ai-empty"><span class="ai-empty-icon">AI</span><p>${esc(hint)}</p></div>`;
    return;
  }
  const rows = aiMessages.map((m) => {
    const role = m.role === "user" ? "user" : "assistant";
    const error = m.error ? " error" : "";
    const meta = m.meta ? `<div class="ai-meta">${esc(m.meta)}</div>` : "";
    const markdown = role === "assistant" && !m.error;
    const body = markdown ? renderAiMarkdown(m.content) : esc(m.content);
    const cls = markdown ? "ai-text md" : "ai-text plain";
    return `<div class="ai-msg ${role}${error}">
      <div class="ai-role">${role === "user" ? "你" : "AI"}</div>
      <div class="ai-bubble"><div class="${cls}">${body}</div>${meta}</div>
    </div>`;
  });
  if (aiBusy) {
    rows.push(`<div class="ai-msg assistant">
      <div class="ai-role">AI</div>
      <div class="ai-bubble ai-thinking"><span></span><span></span><span></span></div>
    </div>`);
  }
  box.innerHTML = rows.join("");
  box.scrollTop = box.scrollHeight;
}

async function callAi(kind, question) {
  if (aiBusy) return;
  if (!aiReady()) {
    toast((aiHealthState && aiHealthState.hint) || "AI 未启用", "err");
    return;
  }
  const sel = $("#aiApp");
  const appId = sel && sel.value ? parseInt(sel.value, 10) : null;
  const history = aiMessages
    .filter((m) => !m.error)
    .slice(-8)
    .map((m) => ({ role: m.role, content: m.content }));

  aiMessages.push({ role: "user", content: question });
  aiBusy = true;
  $("#aiInput").value = "";
  updateAiControls();
  renderAiMessages();

  try {
    let result;
    if (kind === "analyze") {
      result = await api("/ai/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app_id: appId }),
      });
    } else {
      result = await api("/ai/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: question, app_id: appId, history }),
      });
    }
    aiMessages.push({
      role: "assistant",
      content: result.answer || "AI 未返回内容",
      meta: aiResultMeta(result),
    });
  } catch (e) {
    aiMessages.push({ role: "assistant", error: true, content: "请求失败：" + e.message });
  } finally {
    aiBusy = false;
    updateAiControls();
    renderAiMessages();
  }
}

function aiAnalyze() {
  const sel = $("#aiApp");
  const scoped = sel && sel.value;
  callAi("analyze", scoped ? "请分析当前应用的运行状态" : "请总结当前平台运行状态");
}

function aiAsk(question) {
  callAi("chat", question);
}

/* ---------------- 事件绑定 ---------------- */
function bindEvents() {
  $("#refreshBtn").onclick = () => {
    loadApps();
    loadMode();
    loadPoolResources();
    if (activeView === "nodes") loadHostMetrics();
    if (activeView === "alerts") loadAlerts();
    if (activeView === "audit") loadAuditLogs();
    if (activeView === "files") renderFilePage(true);
    if (activeView === "ai") { loadAiHealth(true); renderAiAppOptions(); }
    if (activeView === "users" && isAdmin()) loadUsers();
    toast("已刷新");
  };
  $("#newAppBtn").onclick = () => { resetWizard(); $("#wizardMask").classList.remove("hidden"); };
  $("#logoutBtn").onclick = doLogout;

  // 左侧导航切换（包含用户管理视图）
  $("#sideNav").addEventListener("click", (e) => {
    const item = e.target.closest(".nav-item[data-view]");
    if (!item) return;
    switchWorkspaceView(item.dataset.view);
  });

  // 网关路由视图事件
  $("#routeSearch").addEventListener("input", (e) => {
    routeQuery = e.target.value;
    renderRoutesView();
  });
  $("#routesBody").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-ract]");
    if (!btn) return;
    const id = parseInt(btn.closest("tr").dataset.id, 10);
    const act = btn.dataset.ract;
    if (act === "copy") copyText(btn.dataset.url, "访问链接已复制");
    else if (act === "nginx") openNginxPreview(id);
    else if (act === "detail") {
      const a = apps.find((x) => x.id === id);
      if (a) openDrawer(a);
    }
  });
  $("#nginxPreviewClose").onclick = $("#nginxPreviewOk").onclick = () =>
    $("#nginxPreviewMask").classList.add("hidden");
  $("#nginxPreviewMask").addEventListener("click", (e) => {
    if (e.target.id === "nginxPreviewMask") $("#nginxPreviewMask").classList.add("hidden");
  });
  $("#nginxPreviewCopy").onclick = () =>
    copyText($("#nginxPreviewCode").textContent, "Nginx 配置已复制");

  // 模板与指南事件
  $("#tplGrid").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-tpl]");
    if (!btn) return;
    resetWizard();
    $("#wzName").value = btn.dataset.demo || "";
    $("#wzPort").value = btn.dataset.port || "8000";
    setWizardSource("demo");
    await loadDemoOptions();
    const want = `${btn.dataset.tpl}-demo.zip`;
    const opt = [...$("#wzDemo").options].find((o) => o.value === want);
    if (opt) $("#wzDemo").value = want;
    $("#wizardMask").classList.remove("hidden");
    toast(`已预填 ${btn.dataset.tpl.toUpperCase()} 模板，可用内置示例直接创建，或切换为上传自己的 zip 包`);
  });
  $("#copyManifestBtn").onclick = () =>
    copyText($("#manifestCode").textContent, "paas.json 示例已复制");

  // 管理员：用户管理内嵌页面事件
  $("#addUserBtn").onclick = showUserForm;
  $("#ufCancel").onclick = hideUserForm;
  $("#ufSubmit").onclick = submitUserForm;
  $("#usersBody").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-uact]");
    if (!btn) return;
    const id = parseInt(btn.closest("tr").dataset.id, 10);
    onUserAction(id, btn.dataset.uact);
  });

  $("#ownerSelect").onchange = (e) => { ownerFilter = e.target.value; renderList(); };
  $("#wizardClose").onclick = $("#wizardCancel").onclick = () => $("#wizardMask").classList.add("hidden");
  $("#wizardSubmit").onclick = runWizard;

  Object.keys(WZ_FIELDS).forEach((key) => {
    $(WZ_FIELDS[key].btn).onclick = () => confirmWzField(key);$(WZ_FIELDS[key].input).addEventListener("input", () => {
      if (wizardConfirm[key]) setWzFieldState(key, false, "", "");
    });
  });

  Object.keys(QUOTA_FIELDS).forEach((key) => {
    $(QUOTA_FIELDS[key].btn).onclick = () => confirmQuotaField(key);$(QUOTA_FIELDS[key].input).addEventListener("input", () => {
      quotaEditing = true;
      if (quotaConfirm[key]) setQuotaField(key, false, "", "");
    });
  });

  $("#redeployClose").onclick = $("#redeployCancel").onclick = () => $("#redeployMask").classList.add("hidden");
  $("#redeployGo").onclick = runRedeploy; $$("input[name='rdMode']").forEach((r) => (r.onchange = updateRdGate));
  Object.keys(RD_FIELDS).forEach((key) => {
    $(RD_FIELDS[key].btn).onclick = () => confirmRdField(key);$(RD_FIELDS[key].input).addEventListener("input", () => {
      if (rdConfirm[key]) setRdField(key, false, "", "");
    });
  });
  $("#redeployMask").addEventListener("click", (e) => {
    if (e.target.id === "redeployMask") $("#redeployMask").classList.add("hidden");
  });
  updateRdGate();

  $("#drawerClose").onclick = closeDrawer;
  $("#drawerMask").onclick = closeDrawer;

  // 项目文件管理：顶部项目上下文栏 + 选择浮层 + 文本编辑器弹层
  $("#fmSwitchBtn").onclick = (e) => {
    e.stopPropagation();
    toggleFmPicker();
  };
  $("#fmPicker").addEventListener("click", (e) => {
    const row = e.target.closest("[data-fm-pick]");
    if (!row) return;
    e.stopPropagation();
    closeFmPicker();
    const id = parseInt(row.dataset.fmPick, 10);
    if (id === fmCurrentAppId) return;
    filesTerm.stop(); // 目标容器变了，先断开旧会话（避免连到上一个项目的容器）
    fmCurrentAppId = id;
    renderFilePage(true);
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#fmCtx")) closeFmPicker();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#fmPicker").classList.contains("hidden")) closeFmPicker();
  });
  $("#editorClose").onclick = $("#editorCancel").onclick = closeEditor;
  $("#editorSaveBtn").onclick = saveEditor;
  $("#editorMask").addEventListener("click", (e) => {
    if (e.target.id === "editorMask") closeEditor();
  });

  $$(".seg-item").forEach((s) => (s.onclick = () => { wizardSrc = s.dataset.src; $$
(".seg-item").forEach((x) => x.classList.toggle("active", x === s));
    setWizardSource(s.dataset.src);
  }));

  $("#appList").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const card = e.target.closest(".app-card");
    onCardAction(parseInt(card.dataset.id, 10), btn.dataset.act);
  });

  $("#drawerTabs").addEventListener("click", (e) => {
    const t = e.target.closest(".tab");
    if (t) switchTab(t.dataset.tab);
  });

  $("#drawerActions").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-dact]");
    if (b) onCardAction(current.id, b.dataset.dact);
  });

  // 详情抽屉：切换服务 / 单服务重启
  $("#drawerService").onchange = (e) => onServiceChange(e.target.value);
  $("#overviewInfo").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-svc-restart]");
    if (b) restartService(b.dataset.svcRestart);
  });

  // 告警中心事件
  $("#alertStatusFilter").onchange = $("#alertLevelFilter").onchange = loadAlerts;
  $("#alertsBody").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-aact]");
    if (!b) return;
    onAlertAction(parseInt(b.closest("tr").dataset.id, 10), b.dataset.aact);
  });

  // 审计日志事件
  $("#auditSearch").addEventListener("input", (e) => {
    auditQuery = e.target.value.trim();
    renderAuditLogs();
  });
  $("#auditActionFilter").onchange = loadAuditLogs;
  $("#auditBody").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-audit-id]");
    if (!btn) return;
    openAuditDetail(parseInt(btn.dataset.auditId, 10));
  });
  $("#auditDetailClose").onclick = $("#auditDetailOk").onclick = closeAuditDetail;
  $("#auditDetailMask").addEventListener("click", (e) => {
    if (e.target.id === "auditDetailMask") closeAuditDetail();
  });

  // AI 运行分析
  $("#aiForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#aiInput").value.trim();
    if (text) aiAsk(text);
  });
  $("#aiInput").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
    e.preventDefault();
    const text = $("#aiInput").value.trim();
    if (text) aiAsk(text);
  });
  $("#aiSuggest").addEventListener("click", (e) => {
    const btn = e.target.closest("button.ai-chip");
    if (!btn || btn.disabled) return;
    if (btn.dataset.aiAction === "analyze") aiAnalyze();
    else if (btn.dataset.aiPrompt) aiAsk(btn.dataset.aiPrompt);
  });
  $("#aiClear").onclick = () => {
    aiMessages = [];
    renderAiMessages();
  };
  $("#aiApp").onchange = () => {
    if (!aiMessages.length) return;
    aiMessages = [];
    renderAiMessages();
    toast("已切换分析范围");
  };

  // 应用配置（环境变量 / 启动命令）
  $("#cfgSaveBtn").onclick = saveAppConfig;
  ["#cfgEnv", "#cfgCmd"].forEach((sel) => {
    $(sel).addEventListener("input", () => { cfgEditing = true; });
  });

  $("#wsToggle").onclick = () => { ws ? stopWs() : startWs(); };
  $("#wsClear").onclick = () => { $("#liveLogs").textContent = ""; };
  $("#quotaSaveBtn").onclick = saveQuota;
  $("#diagRun").onclick = runDiagnose;
  $("#whTriggerBtn").onclick = triggerWebhook;
  $("#whResetBtn").onclick = resetWebhook;
  $("#whRefreshBtn").onclick = loadWebhook;
  $("#whCopyUrl").onclick = () => copyText($("#whUrl").value, "Webhook 地址已复制");
  $("#whCopySecret").onclick = () => copyText($("#whSecret").value, "签名密钥已复制");
  // 终端实例：抽屉与文件管理页各一个，各面板的按钮/输入框由实例自行绑定
  drawerTerm.bind();
  filesTerm.bind();

  $("#wizardMask").addEventListener("click", (e) => {
    if (e.target.id === "wizardMask") $("#wizardMask").classList.add("hidden");
  });
}

/* ---------------- 初始化 ---------------- */
(function init() {
  bindEvents();
  loadAuth();
  loadMode();
  loadApps();
  loadPoolResources();
  updateAlertBadge();
  listTimer = setInterval(() => {
    loadApps();
    if (current) refreshCurrent();
  }, 5000);
})();