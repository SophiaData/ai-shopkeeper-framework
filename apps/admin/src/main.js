import "./styles.css";
import "./operations.css";
import { OperationsWorkbench, OPERATIONS_ROUTES } from "./operations-workbench.js";
import { api, login, logout, me } from "./api.js";
import { OrderWorkbench } from "./order-workbench.js";

const orderWorkbench = new OrderWorkbench(api);

const ICONS = {
  overview:
    '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
  workflows:
    '<svg viewBox="0 0 24 24"><path d="M4 6h10"/><path d="M4 12h16"/><path d="M4 18h8"/><circle cx="18" cy="6" r="2"/><circle cx="10" cy="18" r="2"/></svg>',
  runs:
    '<svg viewBox="0 0 24 24"><path d="M8 6h12"/><path d="M8 12h12"/><path d="M8 18h12"/><circle cx="4.2" cy="6" r="1.3"/><circle cx="4.2" cy="12" r="1.3"/><circle cx="4.2" cy="18" r="1.3"/></svg>',
  agents:
    '<svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="2.4"/><circle cx="16" cy="8" r="2.4"/><path d="M4.2 18.2c.7-2.2 2.2-3.3 3.8-3.3s3.1 1.1 3.8 3.3"/><path d="M12.2 18.2c.7-2.2 2.2-3.3 3.8-3.3s3.1 1.1 3.8 3.3"/></svg>',
  scout:
    '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><path d="M20 20l-3.5-3.5"/></svg>',
  products:
    '<svg viewBox="0 0 24 24"><path d="M4 8l8-4 8 4-8 4-8-4z"/><path d="M4 12l8 4 8-4"/><path d="M4 16l8 4 8-4"/></svg>',
  approvals:
    '<svg viewBox="0 0 24 24"><path d="M8 6h11"/><path d="M8 12h11"/><path d="M8 18h11"/><path d="M4 6h.01"/><path d="M4 12h.01"/><path d="M4 18h.01"/></svg>',
  orders:
    '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h8"/><path d="M8 13h5"/></svg>',
  reports:
    '<svg viewBox="0 0 24 24"><path d="M5 19V9"/><path d="M10 19V5"/><path d="M15 19v-7"/><path d="M20 19V8"/></svg>',
  profit:
    '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 7v10"/><path d="M9.4 9.3c.7-1 1.7-1.5 2.6-1.5 1.4 0 2.4.8 2.4 2 0 2.4-4 1.6-4 3.8 0 1.1.9 2 2.4 2 .9 0 1.9-.4 2.6-1.3"/></svg>',
  settings:
    '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4"/></svg>',
  cs: '<svg viewBox="0 0 24 24"><path d="M5 6.5A2.5 2.5 0 0 1 7.5 4h9A2.5 2.5 0 0 1 19 6.5v7A2.5 2.5 0 0 1 16.5 16H9l-4 3.2V6.5z"/></svg>',
  outreach:
    '<svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="2.2"/><path d="M4.2 16.8c.6-2 2-3 3.8-3s3.2 1 3.8 3"/><path d="M14 6.5h6.2v5.2H16L14 14.2V6.5z"/></svg>',
  moon: '<svg viewBox="0 0 24 24"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 7 7 0 1 0 20 14.5z"/></svg>',
  sun: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M17.7 6.3l1.4-1.4M4.9 19.1l1.4-1.4"/></svg>',
  user: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.5"/><path d="M5 19c1.5-3 4-4.5 7-4.5s5.5 1.5 7 4.5"/></svg>',
  check:
    '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9"/></svg>',
  logo: '<svg viewBox="0 0 24 24"><path d="M5 16l7-12 7 12H5z"/><path d="M9 16l3 5 3-5"/></svg>',
};

const NAV_GROUPS = [
  { label: "日常运营", items: Object.entries(OPERATIONS_ROUTES).map(([id,label]) => ({id,label})) },
  {
    label: "AI 与系统工具",
    items: [
      { id: "overview", label: "系统诊断" },
      { id: "workflows", label: "AI 工作流" },
      { id: "runs", label: "编排运行" },
      { id: "agents", label: "AI 代理" },
      { id: "scout", label: "选品" },
      { id: "spotcheck", label: "人工抽检" },
      { id: "products", label: "商品" },
      { id: "profit", label: "利润" },
    ],
  },
  {
    label: "治理",
    items: [
      { id: "approvals", label: "审批" },
      { id: "cs", label: "客服" },
      { id: "outreach", label: "达人建联" },
      { id: "orders", label: "订单" },
      { id: "reports", label: "报表" },
      { id: "settings", label: "设置" },
    ],
  },
];

const NAV = NAV_GROUPS.flatMap((g) => g.items);

const state = {
  user: null,
  status: null,
  shop: null,
  route: "operations",
  approvals: [],
  selectedApproval: null,
  publishPreview: null,
  runs: [],
  selectedRun: null,
  orchRuns: [],
  orchRun: null,
  agentStats: null,
  agentRun: null,
  agentFrom: "",
  agentTo: "",
  error: "",
  theme: localStorage.getItem("admin-theme") || "light",
  lang: localStorage.getItem("admin-lang") || "zh",
  profitShop: "",
  shops: [],
  activeShop: "",
  shopGeneration: 0,
  selectedOrderId: "",
  orderPlatform: "",
  profitFrom: "",
  profitTo: "",
  profitShops: [],
  profit: null,
  adSpendRows: [],
  csInbox: [],
  csDrafts: [],
  csNote: "",
  csError: "",
  outreachDrafts: [],
  outreachNote: "",
  outreachError: "",
};

const FIXTURE_URL = "https://detail.1688.com/offer/600123456789.html";

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function applyTheme() {
  document.documentElement.setAttribute("data-theme", state.theme === "dark" ? "dark" : "light");
}

function routeFromLocation() {
  if (location.pathname === "/dashboard/profit") return "profit";
  const path = location.pathname.replace(/^\/admin\/?/, "");
  const page = (path.split("/")[0] || "operations").toLowerCase();
  if (page === "login") return "login";
  return NAV.some((n) => n.id === page) ? page : "operations";
}

function runIdFromLocation() {
  try {
    return new URLSearchParams(location.search).get("run") || "";
  } catch {
    return "";
  }
}

function approvalIdFromLocation() {
  try {
    return new URLSearchParams(location.search).get("id") || "";
  } catch {
    return "";
  }
}

function navigate(page, query) {
  const qs =
    query && Object.keys(query).length ? `?${new URLSearchParams(query).toString()}` : "";
  const next = page === "login" ? "/admin/login" : `/admin/${page}${qs}`;
  if (`${location.pathname}${location.search}` !== next) history.pushState({}, "", next);
  state.route = page;
  render();
}

function pageTitle() {
  const hit = NAV.find((n) => n.id === state.route);
  return hit ? hit.label : "总览";
}

function emptyState(title, text) {
  return `<div class="empty"><strong>${esc(title)}</strong>${esc(text)}</div>`;
}

function renderLogin() {
  applyTheme();
  const root = document.getElementById("app");
  root.innerHTML = `
    <div class="login-page">
      <form class="login-card" id="login-form">
        <div class="brand">
          <div class="brand-logo">${ICONS.logo}</div>
          <div class="brand-text">
            <div class="brand-name">AI 店长</div>
            <p class="brand-sub">小团队运营助手</p>
          </div>
        </div>
        <h1>登录</h1>
        <p class="lead">小团队运营助手 · 辅助选品、素材整理、投放计划与数据复盘。交易与履约在平台完成。</p>
        ${state.error ? `<p class="error">${esc(state.error)}</p>` : ""}
        <div class="field"><label>用户名</label><input name="username" autocomplete="username" required /></div>
        <div class="field"><label>密码</label><input name="password" type="password" autocomplete="current-password" required /></div>
        <button class="btn" type="submit" style="width:100%">登录</button>
        <p class="muted" style="margin:14px 0 0;font-size:11px">开发自测页仍在 <a href="/workflow">/workflow</a>。</p>
      </form>
    </div>
  `;
  document.getElementById("login-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    state.error = "";
    try {
      const res = await login(String(fd.get("username") || ""), String(fd.get("password") || ""));
      state.user = res.user;
      window.__CSRF__ = res.csrf_token || "";
      navigate("operations");
      await refreshShellData();
      render();
    } catch (err) {
      state.error = err.message || "登录失败";
      renderLogin();
    }
  };
}

// P2-11 多店铺：把店铺选择从「利润页局部」提升为**全局**顶栏胶囊。
// 选中的店铺存进 state.activeShop，各页请求都带上 shop_id —— 避免出现
// 「页面上看的是 A 店、请求却打到默认店」这种静默错配。
function renderShopSwitcher() {
  const shops = state.shops || [];
  if (shops.length <= 1) {
    const only = shops[0] || {};
    return `<span class="capsule" title="当前店铺">${esc(only.name || state.shop?.name || "默认店铺")}</span>`;
  }
  const active = state.activeShop || "";
  return `<span class="capsule shop-switch">
    <select id="shop-switch" title="切换店铺">${shops
      .map(
        (s) =>
          `<option value="${esc(s.shop_id)}" ${s.shop_id === active ? "selected" : ""}>${esc(
            s.name || s.shop_id
          )}</option>`
      )
      .join("")}</select>
  </span>`;
}

function shell(bodyHtml) {
  applyTheme();
  const user = state.user || {};
  const shop = state.shop || { name: "泰国站 · 家居收纳" };
  const systemOk = state.overview ? state.overview.system_ok !== false : true;
  const navHtml = NAV_GROUPS.map(
    (g) => `
      <div class="nav-group">
        <div class="nav-label">${esc(g.label)}</div>
        ${g.items
          .map(
            (n) =>
              `<a href="/admin/${n.id}" data-nav="${n.id}" class="${state.route === n.id ? "active" : ""}">${
                ICONS[n.id] || ""
              }<span>${esc(n.label)}</span></a>`
          )
          .join("")}
      </div>`
  ).join("");
  return `
    <div class="app-shell">
      <aside class="sidebar" id="sidebar">
        <div class="brand">
          <div class="brand-logo">${ICONS.logo}</div>
          <div class="brand-text">
            <div class="brand-name">AI 店长</div>
            <p class="brand-sub">小团队运营助手</p>
          </div>
        </div>
        <nav class="nav">${navHtml}</nav>
        <div class="sidebar-foot">店铺 ${esc(shop.name || shop.id || "-")}<br/>发货 / 退款请到卖家中心</div>
      </aside>
      <div class="nav-scrim" id="nav-scrim"></div>
      <div class="main">
        <header class="topbar">
          <div class="topbar-left">
            <button class="capsule nav-toggle" type="button" id="btn-menu" aria-label="打开导航" aria-controls="sidebar" aria-expanded="false">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
            </button>
            <div class="top-title">${esc(pageTitle())}</div>
          </div>
          <div class="top-right">
            ${renderShopSwitcher()}
            <button class="capsule" type="button" id="btn-theme" title="主题">${
              state.theme === "dark" ? ICONS.sun : ICONS.moon
            }</button>
            <button class="capsule" type="button" id="btn-lang">${esc(state.lang === "en" ? "EN" : "中文")}</button>
            <span class="capsule"><span class="dot" style="${
              systemOk ? "" : "background:var(--bad)"
            }"></span>${systemOk ? "系统正常" : "需关注"}</span>
            <button class="capsule user" type="button" id="btn-logout">${ICONS.user}<span>${esc(
              user.username || "-"
            )}</span></button>
          </div>
        </header>
        <main class="content">${bodyHtml}</main>
      </div>
    </div>
  `;
}

function bindShell() {
  const shell = document.querySelector(".app-shell");
  const menuButton = document.getElementById("btn-menu");
  const scrim = document.getElementById("nav-scrim");
  const setNav = (open) => {
    shell?.classList.toggle("nav-open", open);
    menuButton?.setAttribute("aria-expanded", open ? "true" : "false");
  };
  menuButton?.addEventListener("click", () => setNav(!shell?.classList.contains("nav-open")));
  scrim?.addEventListener("click", () => setNav(false));
  document.onkeydown = (event) => { if (event.key === "Escape") setNav(false); };
  const routeKicker = {
    overview: "OPERATIONS", workflows: "ORCHESTRATION", runs: "RUN QUEUE", agents: "AI AGENTS",
    scout: "SOURCING", spotcheck: "QUALITY", products: "CATALOG", profit: "PROFIT",
    approvals: "GOVERNANCE", cs: "SUPPORT", outreach: "CREATOR OPS", orders: "FULFILLMENT",
    reports: "REPORTS", settings: "SETTINGS", records: "MODEL CALLS",
  };
  document.querySelectorAll(".page-head").forEach((head) => {
    const first = head.querySelector(":scope > div:first-child");
    if (!first || first.querySelector(".eyebrow")) return;
    const eyebrow = document.createElement("div");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = routeKicker[state.route] || "WORKSPACE";
    first.prepend(eyebrow);
  });
  document.querySelectorAll("[data-nav]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.preventDefault();
      navigate(el.getAttribute("data-nav"));
    });
  });
  const shopSwitch = document.getElementById("shop-switch");
  if (shopSwitch) {
    shopSwitch.onchange = () => {
      // 切店要重载：各页数据都按 shop_id 取，只换标签不换数据会显示错配。
      state.activeShop = shopSwitch.value;
      state.shopGeneration += 1;
      operationsWorkbench.clear();
      // 先清掉上一店的订单与 SLA，避免新店请求失败时旧列表仍可点开处理。
      state.syncedOrders = [];
      state.sla = null;
      state.selectedOrderId = "";
      orderWorkbench.clear();
      if (state.route === "orders") history.replaceState({}, "", "/admin/orders");
      try {
        window.localStorage.setItem("shopkeeper.activeShop", state.activeShop);
      } catch (err) {
        /* 无痕模式下 localStorage 可能不可用，不影响功能 */
      }
      render();
    };
  }

  const themeBtn = document.getElementById("btn-theme");
  if (themeBtn) {
    themeBtn.onclick = () => {
      state.theme = state.theme === "dark" ? "light" : "dark";
      localStorage.setItem("admin-theme", state.theme);
      applyTheme();
      render();
    };
  }
  const langBtn = document.getElementById("btn-lang");
  if (langBtn) {
    langBtn.onclick = () => {
      state.lang = state.lang === "en" ? "zh" : "en";
      localStorage.setItem("admin-lang", state.lang);
      render();
    };
  }
  const logoutBtn = document.getElementById("btn-logout");
  if (logoutBtn) {
    logoutBtn.onclick = async () => {
      try {
        await logout();
      } catch {
        /* ignore */
      }
      state.user = null;
      navigate("login");
    };
  }
}

async function refreshShellData() {
  const [overview, status] = await Promise.all([
    api("/api/admin/overview"),
    api("/api/admin/status"),
  ]);
  state.overview = overview;
  state.status = status;
  state.shop = overview.shop;
  await loadShops();
}

// P2-11：店铺列表是全局的（顶栏胶囊要用）。取不到就退回单店，不让整页挂掉。
async function loadShops() {
  try {
    const data = await api("/api/workflow/shops");
    state.shops = data.shops || [];
  } catch (err) {
    state.shops = [{ shop_id: "default", name: state.shop?.name || "默认店铺" }];
    return;
  }
  if (!state.activeShop) {
    try {
      state.activeShop = window.localStorage.getItem("shopkeeper.activeShop") || "";
    } catch (err) {
      state.activeShop = "";
    }
    const known = (state.shops || []).some((s) => s.shop_id === state.activeShop);
    if (!known) state.activeShop = (state.shops[0] || {}).shop_id || "";
  }
}

function healthToneClass(c) {
  if (c && c.tone === "bad") return "bad";
  if (c && c.tone === "warn") return "warn";
  if (c && c.tone === "idle") return "idle";
  if (c && c.ok === false) return "bad";
  return "";
}

function pageOverview() {
  const o = state.overview || {};
  const banner = o.banner || "系统正常 · 全部组件运行中";
  const bannerOk = o.system_ok !== false;
  const components = o.components || [];
  const facts = o.facts || [];
  const switches = o.switches || [];
  const canWrite = state.user && state.user.role !== "viewer";
  const metrics = [
    ["待审批", o.pending_approvals ?? 0, "八态审批收件箱"],
    ["运行中工作流", o.running_workflows ?? 0, "编排器未终态 run"],
    ["临期订单", o.orders_due_soon ?? 0, "本地 SLA 提醒"],
    ["超时订单", o.orders_overdue ?? 0, "请去卖家中心打单"],
  ];
  const alerts = o.alerts || [];
  const factPills = facts.length
    ? `<div class="banner-facts">${facts
        .map(
          (f) =>
            `<span class="pill ${f.ok === false ? "bad" : ""}" title="${esc(f.hint || "")}">${esc(
              f.label
            )} ${esc(f.value)}</span>`
        )
        .join("")}</div>`
    : "";
  return `
    <div class="banner ${bannerOk ? "" : "warn"}">${ICONS.check}<div class="banner-body"><span>${esc(
      banner
    )}</span>${factPills}</div></div>
    <div class="page-head">
      <div>
        <h1>总览</h1>
        <p>组件健康只读自 /healthz、工作流 status 与编排 flows。无数据标缺数据，不编造。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" id="btn-demo-orch" ${canWrite ? "" : "disabled"} title="${
          canWrite ? "强制 mock，写入本服务工作台库" : "只读账号不能触发演示"
        }">一键 mock 演示</button>
        <button class="btn secondary" type="button" id="btn-refresh-overview">刷新</button>
      </div>
    </div>
    <div class="grid-4" style="margin-bottom:12px">
      ${components
        .map(
          (c) => `
        <section class="card health-card">
          <div class="health-top">
            <span class="health-dot ${healthToneClass(c)}"></span>
            <span class="health-name">${esc(c.name)}</span>
            <span class="tag-mono">${esc(c.tag)}</span>
          </div>
          <div class="health-desc">${esc(c.desc || "")}</div>
        </section>`
        )
        .join("")}
    </div>
    <div class="grid-4">
      ${metrics
        .map(
          ([title, value, hint]) => `
        <section class="card">
          <h3>${esc(title)}</h3>
          <div class="metric">${esc(value)}</div>
          <div class="hint">${esc(hint)}</div>
        </section>`
        )
        .join("")}
    </div>
    <div class="split" style="margin-top:12px">
      <section class="panel">
        <div class="panel-head"><h2>快捷开关</h2><span class="muted">只读 · 危险项不可在线打开</span></div>
        <div class="panel-body">
          ${switches
            .map(
              (s) => `
            <div class="switch-row">
              <div class="switch-meta">
                <strong>${esc(s.name)}</strong>
                <span>${esc(s.hint || "")}</span>
              </div>
              <div class="toggle-wrap">
                <span class="tag">${s.on ? "已开启" : "已关闭"}</span>
                <div class="toggle ${s.on ? "on" : ""}" title="只读"></div>
              </div>
            </div>`
            )
            .join("") || emptyState("无开关", "环境开关会显示在这里。")}
        </div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>最近告警 / 任务失败</h2></div>
        <div class="panel-body">
          ${
            alerts.length
              ? `<table><thead><tr><th>类型</th><th>标题</th><th>详情</th><th>时间</th></tr></thead><tbody>
              ${alerts
                .map(
                  (a) => `<tr>
                  <td>${esc(a.kind)}</td><td>${esc(a.title)}</td>
                  <td>${esc(a.detail)}</td><td>${esc(a.at)}</td></tr>`
                )
                .join("")}
              </tbody></table>`
              : emptyState("暂无告警", "任务失败或审批执行失败会出现在这里。")
          }
        </div>
      </section>
    </div>
  `;
}

async function loadApprovals(selectId) {
  const data = await api("/api/admin/approvals");
  state.approvals = data.approvals || [];
  if (selectId) {
    state.selectedApproval = await api(`/api/admin/approvals/${encodeURIComponent(selectId)}`);
    state.publishPreview = await loadPublishPreview(selectId);
  } else if (state.approvals[0]) {
    state.selectedApproval = await api(`/api/admin/approvals/${encodeURIComponent(state.approvals[0].id)}`);
    state.publishPreview = await loadPublishPreview(state.approvals[0].id);
  } else {
    state.selectedApproval = null;
  }
}

async function loadPublishPreview(approvalId) {
  // P0-05：上架前预览。只读端点；取不到不应让整个审批页崩掉。
  try {
    return await api(`/api/admin/approvals/${encodeURIComponent(approvalId)}/publish-preview`);
  } catch (err) {
    return { error: String(err && err.message ? err.message : err) };
  }
}

// P0-04：真写上架要求 category_id / warehouse_id / 主图，而生成草稿时只有 name/specs。
// 此前没有任何入口能填 —— 卖家只能手改数据库，工作流断在「已批准但发不出去」。
function renderPublishInputs(draft) {
  const inp = (draft && draft.payload && draft.payload.input) || {};
  const listed = Boolean(draft && draft.platform_product_id);
  return `
    <h3 style="margin-top:14px">上架必需字段</h3>
    <p class="muted" style="margin:0 0 8px">
      真写上架需要类目、仓库与主图。补齐后草稿会回到「待审」，需重新通过才能上架。
    </p>
    <div class="field"><label>类目 ID（category_id）</label>
      <input id="pi-category" value="${esc(inp.category_id || "")}" placeholder="例如 600001" /></div>
    <div class="field"><label>仓库 ID（warehouse_id）</label>
      <input id="pi-warehouse" value="${esc(inp.warehouse_id || "")}" placeholder="例如 1234567890" /></div>
    <div class="field"><label>价格（THB）</label>
      <input id="pi-price" value="${esc(inp.price_amount || "")}" placeholder="例如 99" /></div>
    <div class="field"><label>本地主图路径（工作目录内）</label>
      <input id="pi-image" value="${esc(inp.image_path || "")}" placeholder="例如 artifacts/demo/images/x_EN.png" /></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
      <button class="btn" type="button" id="btn-save-inputs">保存上架字段</button>
      ${listed ? '<span class="muted">该草稿已有平台商品 ID，改动不影响已上架记录</span>' : ""}
    </div>
    <div id="pi-msg" class="muted" style="margin-top:6px"></div>
  `;
}

function renderPublishPreview(pv) {
  if (!pv) return "";
  if (pv.error) {
    return `<h3 style="margin-top:14px">上架前预览</h3><p class="muted">取不到预览：${esc(pv.error)}</p>`;
  }
  const warn = (pv.warnings || []).length
    ? `<ul class="compliance">${pv.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`
    : '<p class="muted">无阻塞项。</p>';
  const copy = pv.copy || {};
  const armed = pv.write_armed
    ? '<span class="tag approved">已武装</span>'
    : '<span class="tag pending">未武装</span>';
  return `
    <h3 style="margin-top:14px">上架前预览</h3>
    <p class="muted">写通道 ${armed} · 载荷哈希 ${esc(String(pv.payload_hash || "").slice(0, 12))}…${
      pv.preview_consistent ? "" : " · <strong>草稿在审批后被改过</strong>"
    }</p>
    ${warn}
    <h3 style="margin-top:12px">将发给平台的文案</h3>
    <div class="diff">
      <div><div class="muted" style="margin-bottom:6px">泰语标题</div><pre>${esc(copy.title_th || "—")}</pre></div>
      <div><div class="muted" style="margin-bottom:6px">英语标题</div><pre>${esc(copy.title_en || "—")}</pre></div>
    </div>
    <div class="diff" style="margin-top:8px">
      <div><div class="muted" style="margin-bottom:6px">泰语正文</div><pre>${esc(copy.body_th || "—")}</pre></div>
      <div><div class="muted" style="margin-bottom:6px">英语正文</div><pre>${esc(copy.body_en || "—")}</pre></div>
    </div>
    <h3 style="margin-top:12px">图片</h3>
    <div class="images">${
      (pv.images || []).length
        ? pv.images.map((src) => `<img src="${esc(src)}" alt="主图" />`).join("")
        : '<span class="muted">无主图</span>'
    }</div>
  `;
}

function pageApprovals() {
  const list = state.approvals || [];
  const detail = state.selectedApproval;
  const canApprove = state.user && state.user.role === "owner";
  const listHtml = list.length
    ? `<table><thead><tr><th>动作</th><th>状态</th><th>实体</th><th>创建</th></tr></thead><tbody>
      ${list
        .map((a) => {
          const active = detail && detail.id === a.id ? ' style="background:var(--bg)"' : "";
          return `<tr class="clickable" data-approval="${esc(a.id)}"${active}>
            <td>${esc(a.action_type)}</td>
            <td><span class="tag ${esc(a.status)}">${esc(a.status)}</span></td>
            <td>${esc(a.entity_type)} / ${esc(a.entity_id)}</td>
            <td>${esc(a.created_at)}</td>
          </tr>`;
        })
        .join("")}
      </tbody></table>`
    : emptyState("暂无审批单", "生成 listing 草稿后会出现 create_listing 待审单。");

  let detailHtml = emptyState("选择审批单", "左侧点一行查看详情。");
  if (detail) {
    const d = detail.detail || {};
    const images = (d.images || [])
      .map((src) => `<img src="${esc(src)}" alt="商品图" />`)
      .join("");
    detailHtml = `
      <div class="panel-head">
        <h2>${esc(detail.action_type)} · ${esc(detail.status)}</h2>
        <span class="muted">审批人 ${esc(detail.decided_by || "—")}</span>
      </div>
      <div class="panel-body">
        <p class="muted">实体 ${esc(detail.entity_type)} / ${esc(detail.entity_id)} · 载荷哈希 ${esc(
          String(detail.payload_hash || "").slice(0, 12)
        )}…</p>
        <h3>图片</h3>
        <div class="images">${images || '<span class="muted">无图片</span>'}</div>
        <h3 style="margin-top:14px">改动前后</h3>
        <div class="diff">
          <div><div class="muted" style="margin-bottom:6px">改前</div><pre>${esc(
            JSON.stringify(d.before || {}, null, 2)
          )}</pre></div>
          <div><div class="muted" style="margin-bottom:6px">改后</div><pre>${esc(
            JSON.stringify(d.after || {}, null, 2)
          )}</pre></div>
        </div>
        <h3 style="margin-top:14px">合规提示</h3>
        <ul class="compliance">${(d.compliance || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
        ${renderPublishInputs(detail)}
        ${renderPublishPreview(state.publishPreview)}
        ${
          detail.status === "pending"
            ? `<div style="margin-top:16px">
                <div class="field"><label>决定说明（打回必填）</label>
                <textarea id="decision-note" placeholder="通过可写简短说明；打回必须写原因"></textarea></div>
                <div style="display:flex;gap:8px;flex-wrap:wrap">
                  <button class="btn" type="button" id="btn-approve" ${canApprove ? "" : "disabled"}>通过</button>
                  <button class="btn danger" type="button" id="btn-reject" ${canApprove ? "" : "disabled"}>打回</button>
                  ${canApprove ? "" : '<span class="muted">仅 owner 可审批</span>'}
                </div>
              </div>`
            : `<p class="muted" style="margin-top:12px">决定说明：${esc(detail.decision_note || "—")}</p>`
        }
      </div>
    `;
  }

  return `
    <div class="page-head">
      <div><h1>审批</h1><p>八态审批收件箱。通过/打回写入审批人账号。</p></div>
    </div>
    <div class="split">
      <section class="panel"><div class="panel-head"><h2>列表</h2></div><div class="panel-body">${listHtml}</div></section>
      <section class="panel" id="approval-detail">${detailHtml}</section>
    </div>
  `;
}

function auditCell(it) {
  // P0-07：平台审核状态（v9 回写）。未轮询过时给明确文案，不假装已通过。
  const status = String(it.platform_audit_status || "").toUpperCase();
  if (!status) {
    return it.platform_product_id ? '<span class="muted">未查询</span>' : '<span class="muted">—</span>';
  }
  // 复用既有 .tag 语义类（styles.css）：approved→绿 / failed→红 / 其余→黄。
  // 不新增 CSS 类，避免与设计规范 §0-11 的既有样式体系分叉。
  const cls = status === "APPROVED" ? "approved" : status === "FAILED" ? "failed" : "pending";
  const reason = it.platform_audit_reason ? ` title="${esc(it.platform_audit_reason)}"` : "";
  return `<span class="tag ${cls}"${reason}>${esc(status)}</span>`;
}

function pageProducts() {
  const items = state.catalog || [];
  return `
    <div class="page-head">
      <div><h1>商品</h1><p>本地商品库（接现有 catalog API）。</p></div>
      <div style="display:flex;gap:8px">
        <a class="btn" href="/admin/candidates" data-nav="candidates">+ 录入候选商品</a>
        <button class="btn secondary" type="button" id="btn-import-demo">导入 Demo 包</button>
      </div>
    </div>
    <section class="panel"><div class="panel-body">
      ${
        items.length
          ? `<table><thead><tr><th>标题</th><th>审批</th><th>平台商品 ID</th><th>平台审核</th><th>更新</th></tr></thead><tbody>
          ${items
            .map(
              (it) => `<tr>
              <td>${esc(it.title)}</td>
              <td><span class="tag ${esc(it.approval_status)}">${esc(it.approval_status)}</span></td>
              <td>${esc(it.platform_product_id || "—")}</td>
              <td>${auditCell(it)}</td>
              <td>${esc(it.updated_at || it.created_at || "")}</td>
            </tr>`
            )
            .join("")}
          </tbody></table>`
          : emptyState("商品库为空", "请先在「日常运营 ➔ 候选商品」保存商品事实并生成 Listing 草稿，再前往审批页查看内容并人工审批。")
      }
    </div></section>
  `;
}

function pageOrders() {
  const synced = state.syncedOrders || [];
  const sla = state.sla || {};
  const statuses = [...new Set(synced.map((o) => String(o.status || "").trim()).filter(Boolean))];
  const platforms = [...new Set(synced.map((o) => String(o.platform || "tiktok")))];
  const kpi = (label, value, hint, tone = "") => `
    <article class="kpi-card ${tone}">
      <span class="kpi-label">${esc(label)}</span>
      <strong class="kpi-value">${esc(value)}</strong>
      <span class="kpi-hint">${esc(hint)}</span>
    </article>`;
  const rows = synced
    .map((o) => {
      const id = String(o.order_id || o.id || "");
      const status = String(o.status || "未知状态");
      const syncedAt = String(o._synced_at || "—");
      const platform = String(o.platform || "tiktok");
      const readOnly = platform !== "tiktok";
      const selected = state.selectedOrderId === id ? "is-selected" : "";
      const disabled = readOnly ? ' disabled title="Shopee 当前只读导入；代发/物流写尚未接入。"' : "";
      return `<button type="button" class="order-row ${selected}" data-order-work="${esc(id)}" data-order-status="${esc(status)}" data-order-platform="${esc(platform)}" data-order-search="${esc(`${id} ${status} ${platform} ${syncedAt}`.toLowerCase())}"${disabled}>
        <span class="order-row-avatar">${esc(id.slice(-2).toUpperCase() || "?")}</span>
        <span class="order-row-main"><strong>${esc(id)}</strong><span class="order-row-meta">${esc(platform)} · ${esc(status)} · ${esc(syncedAt)}</span></span>
        <span class="order-row-arrow" aria-hidden="true">${readOnly ? "只读" : "→"}</span>
      </button>`;
    })
    .join("");
  return `
    <div class="orders-page">
      <div class="page-head orders-head">
        <div>
          <div class="eyebrow">FULFILLMENT</div>
          <h1>订单履约</h1>
          <p>同步订单、代发审批与物流回填。所有外部写都需要人工确认。</p>
        </div>
        <div class="page-actions">
          <span class="safety-chip"><span class="dot"></span>真实写保护中</span>
          <button class="btn secondary" type="button" id="btn-sync-orders" ${state.user && state.user.role === "viewer" ? 'disabled title="只读账号不能执行同步写操作"' : ""}>只读同步</button>
        </div>
      </div>
      <div class="orders-safety">代发与物流回填须先过对应审批；本后台不自动下单、不付款、不发货、不退款。发货与退款请在 TikTok Shop 卖家中心由人操作。</div>
      <div class="orders-kpis">
        ${kpi("临期", sla.due_soon ?? 0, "接近履约时限", Number(sla.due_soon || 0) > 0 ? "is-warn" : "")}
        ${kpi("超时", sla.overdue ?? 0, "需优先处理", Number(sla.overdue || 0) > 0 ? "is-bad" : "")}
        ${kpi("窗口内", sla.ok ?? 0, "仍在时限内")}
        ${kpi("已同步", synced.length, state.orderPlatform ? `当前：${state.orderPlatform}` : "当前店铺 · 全部平台")}
      </div>
      <div class="orders-workspace">
        <section class="panel order-queue" aria-label="已同步订单">
          <div class="queue-head">
            <div><div class="eyebrow">QUEUE</div><h2>已同步订单</h2><p class="muted">${synced.length} 个订单</p></div>
            <div class="queue-tools">
              <input id="order-search" type="search" placeholder="搜索订单号或状态" aria-label="搜索订单" />
              <select id="order-platform-filter" aria-label="按平台筛选"><option value="">全部平台</option>${platforms.map((platform) => `<option value="${esc(platform)}"${state.orderPlatform === platform ? " selected" : ""}>${esc(platform)}</option>`).join("")}</select>
              <select id="order-status-filter" aria-label="按状态筛选"><option value="">全部状态</option>${statuses.map((status) => `<option value="${esc(status)}">${esc(status)}</option>`).join("")}</select>
              <label class="btn secondary" for="shopee-csv" title="Shopee 只读 CSV 导入；不调用平台 API">导入 Shopee CSV</label>
              <input id="shopee-csv" type="file" accept=".csv,text/csv" hidden />
            </div>
          </div>
          <div class="order-list" id="order-list">
            ${rows || emptyState("暂无订单", "点击「只读同步」从平台适配器拉取（mock/offline 可能为空）。")}
          </div>
        </section>
        <div id="order-workbench" class="order-detail"></div>
      </div>
    </div>
  `;
}

// P0-09：M1 人工抽检。四判据逐条打分，自动算通过率并导出 md。
// 关键口径：**未打分与「判不通过」是两回事** —— 前者不计入通过率，
// 否则「还没抽检」会被算成 0% 或 100%。这里的 pending 一律不参与分母。
const SPOT_VERDICTS = [
  { v: "pass", label: "通过" },
  { v: "fail", label: "不通过" },
];

function spotScores(row, criteria) {
  const out = {};
  criteria.forEach((c) => {
    const el = document.querySelector(`input[name="sc-${row}-${c}"]:checked`);
    out[c] = el ? el.value : "pending";
  });
  return out;
}

function renderSpotCheck(state) {
  const items = state.spotCheck || [];
  const criteria = state.spotCheckCriteria || [];
  if (!items.length) {
    return emptyState(
      "暂无可抽检样本",
      "先用 demo 包或生成 listing 草稿，样本会出现在这里。"
    );
  }
  const cohort = state.spotCheckCohort || [];
  const cohortSet = new Set(cohort);
  const matched = items.filter((it) => cohortSet.has(String(it.offer_id || ""))).length;
  const cohortNote =
    cohort.length && matched !== items.length
      ? `<div class="tag failed" style="margin-bottom:8px">当前样本 ${items.length} 个，其中 ${matched} 个属于 M1 cohort（共 ${cohort.length} 个 offerId）—— 映射不上就不是 M1 抽样，本页结论不得当作 M1 验收。</div>`
      : "";
  const head = criteria.map((c) => `<th>${esc(c.label)}</th>`).join("");
  const body = items
    .map((it, idx) => {
      const cells = criteria
        .map(
          (c) => `<td class="sc-cell">${SPOT_VERDICTS.map(
            (v) =>
              `<label class="sc-opt"><input type="radio" name="sc-${idx}-${esc(c.id)}" value="${v.v}" />${v.label}</label>`
          ).join("")}</td>`
        )
        .join("");
      return `<tr data-offer="${esc(it.offer_id)}" data-title="${esc(it.title)}" data-idx="${idx}">
        <td>${idx + 1}</td><td>${esc(it.offer_id)}</td><td>${esc(it.title)}</td>${cells}</tr>`;
    })
    .join("");
  return `
    ${cohortNote}
    <div class="muted" style="margin-bottom:8px">
      门槛：<strong>十包中至少 ${state.spotCheckThresholdSamples ?? 8} 包四项全部通过</strong>
      （即样本级通过率 ≥ ${((state.spotCheckThreshold ?? 0.8) * 100).toFixed(0)}%，DESIGN §9 Done 条件 2）。
      某一包挂任一项即整包不通过；逐格比率只作诊断。
    </div>
    <table class="sc-table"><thead><tr><th>#</th><th>offer_id</th><th>输入标题</th>${head}</tr></thead>
    <tbody>${body}</tbody></table>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
      <button class="btn" type="button" id="btn-spot-score">计算通过率</button>
      <button class="btn secondary" type="button" id="btn-spot-export">导出 md</button>
    </div>
    <div id="spot-result" style="margin-top:12px"></div>
  `;
}

async function loadSpotCheckSamples() {
  const data = await api("/api/admin/spot-check");
  state.spotCheck = data.items || [];
  state.spotCheckCriteria = data.criteria || [];
  state.spotCheckThreshold = data.threshold ?? 0.8;
  state.spotCheckThresholdSamples = data.threshold_samples ?? 8;
  state.spotCheckCohort = data.cohort || [];
  state.spotCheckReport = "";
}

function collectSpotCheckRows() {
  const rows = [];
  document.querySelectorAll("#spot-body tr[data-idx]").forEach((tr) => {
    const idx = tr.getAttribute("data-idx");
    const scores = {};
    (state.spotCheckCriteria || []).forEach((c) => {
      const el = document.querySelector(`input[name="sc-${idx}-${c.id}"]:checked`);
      scores[c.id] = el ? el.value : "pending";
    });
    rows.push({
      offer_id: tr.getAttribute("data-offer") || "",
      title: tr.getAttribute("data-title") || "",
      scores,
    });
  });
  return rows;
}

async function scoreSpotCheck() {
  const rows = collectSpotCheckRows();
  const box = document.getElementById("spot-result");
  let resp;
  try {
    resp = await api("/api/admin/spot-check/report", {
      method: "POST",
      body: JSON.stringify({ rows }),
    });
  } catch (err) {
    // 422 时**保留**表格里已选的判定，只把定位信息显示出来 ——
    // 不能让 owner 因为一次校验失败重填四十格。
    let errs = [];
    try {
      const parsed = JSON.parse(err && err.message ? err.message : "");
      if (parsed && Array.isArray(parsed.errors)) errs = parsed.errors;
    } catch (_) {
      errs = [];
    }
    const detailHtml = errs.length
      ? errs.map((e) => `· ${esc(e.message || JSON.stringify(e))}`).join("<br>")
      : esc(String((err && err.message) || err));
    if (box) {
      box.innerHTML = `
        <div><span class="tag failed">报告被拒绝：未生成通过率</span></div>
        <div class="muted" style="margin-top:6px">已选判定仍保留在表格里，修正后可直接重算。</div>
        <div style="margin-top:6px">${detailHtml}</div>`;
    }
    return;
  }
  state.spotCheckReport = resp.report_md || "";
  const sc = resp.scores || {};
  const rate = sc.pass_rate === null || sc.pass_rate === undefined
    ? sc.status === "no_data"
      ? "无数据（尚未打分）"
      : "未完成（样本未齐或仍有未打分）"
    : `${(sc.pass_rate * 100).toFixed(1)}%`;
  const verdict = sc.meets_threshold === null || sc.meets_threshold === undefined
    ? ""
    : sc.meets_threshold
    ? '<span class="tag approved">达标</span>'
    : '<span class="tag failed">未达标</span>';
  if (box) {
    box.innerHTML = `
      <div><strong>通过率（样本级）：${esc(rate)}</strong> ${verdict}</div>
      <div class="muted">整包四项全过 ${sc.whole_sample_passed ?? 0}/${sc.cohort_size ?? 10}；提交样本 ${sc.samples ?? 0} 个（命中 cohort ${sc.cohort_matched ?? 0} 个）</div>
      <div class="muted">逐格诊断：已打分 ${sc.scored ?? 0} 格（通过 ${sc.passed ?? 0} / 不通过 ${sc.failed ?? 0}），未打分 ${sc.pending ?? 0} 格 —— 不是 M1 通过率。</div>`;
  }
}

function pageSpotCheck() {
  return `
    <div class="page-head">
      <div><h1>人工抽检</h1><p>四判据逐条人工判定；自动计算通过率并可导出报告。</p></div>
      <button class="btn secondary" type="button" id="btn-spot-reload">刷新样本</button>
    </div>
    <section class="panel"><div class="panel-head"><h2>样本</h2></div>
    <div class="panel-body" id="spot-body">${renderSpotCheck(state)}</div></section>
  `;
}

function pageScout() {
  const drafts = (state.drafts || []).filter((d) => d.kind === "listing" || d.kind === "video_script");
  return `
    <div class="page-head">
      <div><h1>选品</h1><p>草稿生成与选品摘要（接现有 drafts / jobs API）。</p></div>
      <div style="display:flex;gap:8px">
        <button class="btn secondary" type="button" id="btn-gen-listing">生成 listing 草稿</button>
        <button class="btn secondary" type="button" id="btn-run-research">跑选品摘要</button>
      </div>
    </div>
    <section class="panel"><div class="panel-head"><h2>最近草稿</h2></div><div class="panel-body">
      ${
        drafts.length
          ? `<table><thead><tr><th>标题</th><th>类型</th><th>状态</th><th>创建</th></tr></thead><tbody>
          ${drafts
            .slice(0, 30)
            .map(
              (d) => `<tr>
              <td>${esc(d.title)}</td><td>${esc(d.kind)}</td>
              <td><span class="tag ${esc(d.status)}">${esc(d.status)}</span></td>
              <td>${esc(d.created_at)}</td></tr>`
            )
            .join("")}
          </tbody></table>`
          : emptyState("暂无选品草稿", "可生成 mock listing，或跑本地选品摘要报表。")
      }
    </div></section>
  `;
}

const MISSING_LABEL = "缺数据";

function thb(minor) {
  if (minor === null || minor === undefined || minor === "") return "—";
  if (minor === MISSING_LABEL) return MISSING_LABEL;
  const n = Number(minor);
  if (!Number.isFinite(n)) return esc(minor);
  return (n / 100).toFixed(2);
}

function ratioText(value) {
  if (value === null || value === undefined || value === "") return "—";
  return esc(value);
}

function isMissing(value) {
  return value === null || value === undefined || value === "" || value === MISSING_LABEL;
}

function moneyOrMissing(minor) {
  if (isMissing(minor)) return MISSING_LABEL;
  const n = Number(minor);
  if (!Number.isFinite(n)) return MISSING_LABEL;
  return (n / 100).toFixed(2);
}

function sumOrMissing(...values) {
  if (values.some((v) => isMissing(v))) return MISSING_LABEL;
  let total = 0;
  for (const value of values) {
    const n = Number(value);
    if (!Number.isFinite(n)) return MISSING_LABEL;
    total += n;
  }
  return total;
}

function moneyCellHtml(value) {
  if (isMissing(value) || !Number.isFinite(Number(value))) {
    return `<span class="missing">${MISSING_LABEL}</span>`;
  }
  return esc(moneyOrMissing(value));
}

function ratioCellHtml(value) {
  if (isMissing(value)) return `<span class="missing">${MISSING_LABEL}</span>`;
  return esc(value);
}

function marginCellHtml(value) {
  if (isMissing(value) || !Number.isFinite(Number(value))) {
    return `<span class="missing">${MISSING_LABEL}</span>`;
  }
  return `${esc(value)}%`;
}

function statusCellHtml(row) {
  if (row && row.data_status === "ok") return "完整";
  return `<span class="missing">${MISSING_LABEL}</span>`;
}

function pageReports() {
  const reports = state.reports || [];
  const profit = state.profit || {};
  const totals = profit.totals || {};
  const alerts = profit.alerts || [];
  const rows = state.adSpendRows || [];
  const canWrite = state.user && state.user.role !== "viewer";
  const shop = state.profitShop || "default";
  return `
    <div class="page-head">
      <div>
        <h1>报表</h1>
        <p>广告费记账与扣广告后利润。完整订单级利润控制台在「运营 / 利润」。ROAS 用销售额除以记账广告费，不是广告平台归因。低于保本只提醒，不改预算。</p>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <a class="btn secondary" href="/admin/profit" data-nav="profit">完整利润控制台</a>
        <button class="btn secondary" type="button" id="btn-job-research">选品摘要</button>
        <button class="btn secondary" type="button" id="btn-job-daily">日报</button>
      </div>
    </div>
    <div id="ad-spend-cards" class="grid-4" style="margin-bottom:12px">
      <article class="card"><h3>广告费 (THB)</h3><div class="metric">${esc(thb(totals.ad_spend ?? profit.ad_spend ?? 0))}</div><p class="hint">店铺 ${esc(shop)} · 记账</p></article>
      <article class="card"><h3>ROAS</h3><div class="metric">${ratioText(totals.ROAS ?? profit.ROAS)}</div><p class="hint">销售额 / 广告费</p></article>
      <article class="card"><h3>ACOS</h3><div class="metric">${ratioText(totals.ACOS ?? profit.ACOS)}</div><p class="hint">广告费 / 销售额</p></article>
      <article class="card"><h3>扣广告后利润 (THB)</h3><div class="metric">${esc(thb(totals.profit_after_ads ?? profit.profit_after_ads))}</div><p class="hint">已扣记账广告费</p></article>
    </div>
    <div id="ad-roas-alert">
      ${
        alerts.length
          ? alerts
              .map(
                (a) =>
                  `<div class="banner warn">${esc(a.message || "")} 保本 ROAS ${esc(
                    a.break_even_roas ?? "—"
                  )} · 最高 CPA ${esc(a.max_cpa_minor == null ? "—" : thb(a.max_cpa_minor))} THB。只提醒，不自动操作。</div>`
              )
              .join("")
          : `<div class="banner">没有低于保本的提醒。本页不修改广告预算。</div>`
      }
    </div>
    <section class="panel" style="margin-bottom:12px">
      <div class="panel-head"><h2>广告费明细</h2><span class="muted">${rows.length} 行 · 只读拉取为 mock</span></div>
      <div class="panel-body" id="ad-spend-table">
        ${
          rows.length
            ? `<table><thead><tr><th>日期</th><th>店铺</th><th>SKU</th><th>渠道</th><th>花费 THB</th><th>展示</th><th>点击</th><th>转化</th><th>来源</th></tr></thead><tbody>
            ${rows
              .map(
                (r) => `<tr>
                <td>${esc(r.spend_date)}</td>
                <td>${esc(r.shop_id)}</td>
                <td>${esc(r.sku || r.product_id || "—")}</td>
                <td>${esc(r.channel)}</td>
                <td>${esc(r.amount_thb)}</td>
                <td>${esc(r.impressions ?? "—")}</td>
                <td>${esc(r.clicks ?? "—")}</td>
                <td>${esc(r.conversions ?? "—")}</td>
                <td>${esc(r.source)}</td>
              </tr>`
              )
              .join("")}
            </tbody></table>`
            : emptyState("还没有广告费", "operator 可手工记账，或从 mock 拉取。不会调用 TikTok Ads。")
        }
        <form id="ad-spend-form" class="grid-3" style="margin-top:16px">
          <div class="field"><label>日期</label><input name="spend_date" type="date" required ${canWrite ? "" : "disabled"} /></div>
          <div class="field"><label>花费 (THB)</label><input name="amount_thb" inputmode="decimal" placeholder="12.50" required ${canWrite ? "" : "disabled"} /></div>
          <div class="field"><label>渠道</label><input name="channel" value="tiktok_ads" ${canWrite ? "" : "disabled"} /></div>
          <div class="field"><label>SKU</label><input name="sku" placeholder="可空，表示整店" ${canWrite ? "" : "disabled"} /></div>
          <div class="field"><label>展示 / 点击 / 转化</label><input name="metrics" placeholder="1000,25,1 可空" ${canWrite ? "" : "disabled"} /></div>
          <div class="field" style="display:flex;align-items:flex-end;gap:8px">
            <button class="btn" type="submit" ${canWrite ? "" : "disabled"}>记账</button>
            <button class="btn secondary" type="button" id="btn-ads-pull" ${canWrite ? "" : "disabled"}>从 mock 拉取</button>
          </div>
        </form>
        <p class="muted">${canWrite ? "记账只写本地表。mock 拉取不外呼、不改预算。" : "当前是只读账号，不能记账。"}</p>
        <p id="ad-spend-form-error" class="error"></p>
      </div>
    </section>
    <section class="panel"><div class="panel-head"><h2>本地报表</h2></div><div class="panel-body">
      ${
        reports.length
          ? reports
              .map(
                (r) => `<article style="margin-bottom:16px">
                <h3 style="margin:0 0 6px">${esc(r.kind)} · ${esc(r.created_at)}</h3>
                <pre class="mono">${esc(r.body_md || "")}</pre>
              </article>`
              )
              .join("")
          : emptyState("暂无报表", "跑一次选品摘要或日报后会出现在这里。")
      }
    </div></section>
  `;
}

function profitAggTable(rows, keyLabel) {
  if (!rows.length) return emptyState("暂无汇总", "选定范围没有可汇总的订单。");
  const head =
    `<th>${esc(keyLabel)}</th><th>订单数</th><th>完整</th><th>缺数据</th><th>收入(THB)</th><th>佣金</th><th>支付费</th><th>广告占位</th><th>广告费</th><th>ROAS</th><th>ACOS</th><th>扣广告后利润</th><th>采购成本</th><th>运费</th><th>毛利</th><th>毛利率</th><th>状态</th>`;
  const body = rows
    .map(
      (row) => `<tr>
        <td>${esc(row.key || row.shop_id || "")}</td>
        <td>${esc(row.orders ?? 0)}</td>
        <td>${esc(row.complete_orders ?? 0)}</td>
        <td>${esc(row.incomplete_orders ?? 0)}</td>
        <td>${moneyCellHtml(row.revenue)}</td>
        <td>${moneyCellHtml(row.platform_commission)}</td>
        <td>${moneyCellHtml(row.payment_fee)}</td>
        <td>${moneyCellHtml(row.ads)}</td>
        <td>${moneyCellHtml(row.ad_spend)}</td>
        <td>${ratioCellHtml(row.ROAS)}</td>
        <td>${ratioCellHtml(row.ACOS)}</td>
        <td>${moneyCellHtml(row.profit_after_ads)}</td>
        <td>${moneyCellHtml(row.purchase_cost)}</td>
        <td>${moneyCellHtml(row.shipping)}</td>
        <td>${moneyCellHtml(row.gross_profit)}</td>
        <td>${marginCellHtml(row.gross_margin_pct)}</td>
        <td>${statusCellHtml(row)}</td>
      </tr>`
    )
    .join("");
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function profitOrderTable(rows) {
  if (!rows.length) return emptyState("暂无订单", "选定店铺与日期范围内没有已同步订单。");
  const head =
    "<th>订单</th><th>店铺</th><th>日期</th><th>收入(THB)</th><th>佣金</th><th>支付费</th><th>广告占位</th><th>广告费</th><th>ROAS</th><th>ACOS</th><th>扣广告后利润</th><th>采购成本</th><th>运费</th><th>毛利</th><th>毛利率</th><th>状态</th>";
  const body = rows
    .map(
      (row) => `<tr>
        <td>${esc(row.order_id)}</td>
        <td>${esc(row.shop_id)}</td>
        <td>${esc(row.date || "")}</td>
        <td>${moneyCellHtml(row.revenue)}</td>
        <td>${moneyCellHtml(row.platform_commission)}</td>
        <td>${moneyCellHtml(row.payment_fee)}</td>
        <td>${moneyCellHtml(row.ads)}</td>
        <td>${moneyCellHtml(row.ad_spend)}</td>
        <td>${ratioCellHtml(row.ROAS)}</td>
        <td>${ratioCellHtml(row.ACOS)}</td>
        <td>${moneyCellHtml(row.profit_after_ads)}</td>
        <td>${moneyCellHtml(row.purchase_cost)}</td>
        <td>${moneyCellHtml(row.shipping)}</td>
        <td>${moneyCellHtml(row.gross_profit)}</td>
        <td>${marginCellHtml(row.gross_margin_pct)}</td>
        <td>${statusCellHtml(row)}</td>
      </tr>`
    )
    .join("");
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function pageProfit() {
  const profit = state.profit || {};
  const totals = profit.totals || {};
  const alerts = profit.alerts || [];
  const shops = state.profitShops || [];
  const shopOptions = [`<option value="">全部店铺</option>`]
    .concat(
      shops.map(
        (s) =>
          `<option value="${esc(s.shop_id)}"${
            state.profitShop === s.shop_id ? " selected" : ""
          }>${esc(s.shop_id)}</option>`
      )
    )
    .join("");
  const bookedRows = Number(profit.ad_spend_row_count || 0);
  const rates = profit.rates || {};
  const placeholderPath = rates.placeholder !== false || bookedRows === 0;
  const revenue = totals.revenue ?? profit.revenue;
  const cogs = totals.purchase_cost;
  const platformFees = sumOrMissing(totals.platform_commission, totals.payment_fee);
  const adSpend = totals.ad_spend ?? profit.ad_spend;
  const roas = totals.ROAS ?? profit.ROAS;
  const acos = totals.ACOS ?? profit.ACOS;
  const afterAds = totals.profit_after_ads ?? profit.profit_after_ads;
  const margin = totals.gross_margin_pct;
  const hasAlert = alerts.length > 0;
  const banner = hasAlert
    ? "低于保本：只提醒（alert_only），不自动改价、不建审批、不调预算"
    : "只读利润控制台 · 数字来自已同步订单与广告费记账，缺数据不编造";
  const cards = [
    ["收入 (THB)", moneyOrMissing(revenue), "已同步订单销售额"],
    ["采购成本 COGS (THB)", moneyOrMissing(cogs), "代发采购；缺则标缺数据"],
    ["平台费 (THB)", moneyOrMissing(platformFees), "佣金 + 支付费"],
    ["广告费 (THB)", moneyOrMissing(adSpend), bookedRows ? "记账 ad_spend" : "无记账不编造"],
    ["ROAS", isMissing(roas) ? MISSING_LABEL : String(roas), "销售额 / 记账广告费"],
    ["ACOS", isMissing(acos) ? MISSING_LABEL : String(acos), "广告费 / 销售额"],
    ["扣广告后利润 (THB)", moneyOrMissing(afterAds), "profit_after_ads"],
    ["毛利率", isMissing(margin) ? MISSING_LABEL : `${margin}%`, "gross_margin_pct"],
  ];
  const rateBits = [
    `佣金 ${rates.commission_pct ?? "?"}%`,
    `支付 ${rates.payment_fee_pct ?? "?"}%`,
    `广告 ${rates.ads_pct ?? "?"}%`,
    `FX ${rates.fx_rate ?? "?"}（${rates.fx_source || "env"}）`,
  ];
  const footnote = placeholderPath
    ? `占位费率（PRICING_*，非平台结算实收）：${rateBits.join(" · ")}。广告记账为空或走占位路径时，扣广告后利润用占位 ads_pct，不编造广告费。${
        rates.note || profit.note || ""
      } 低于保本只提醒（alert_only）。本页无付款/退款/提现按钮，也不改价、不建审批、不调预算。`
    : `费率：${rateBits.join(" · ")}。${rates.note || profit.note || ""} 低于保本只提醒（alert_only）。本页无付款/退款/提现按钮。`;
  const alertHtml = hasAlert
    ? alerts
        .map(
          (a) =>
            `<div class="banner warn">${esc(a.message || "")} 保本 ROAS ${esc(
              a.break_even_roas ?? "—"
            )} · 最高 CPA ${esc(
              a.max_cpa_minor == null ? "—" : moneyOrMissing(a.max_cpa_minor)
            )} THB。只提醒（alert_only），不自动改价、不建审批、不调预算。</div>`
        )
        .join("")
    : "";
  return `
    <div class="banner ${hasAlert ? "warn" : ""}">${ICONS.check}<span>${esc(banner)}</span></div>
    <div class="page-head">
      <div>
        <h1>利润</h1>
        <p>正式控制台利润页。复用 <code>GET /api/workflow/profit</code>。viewer 只读。不是 /workflow 自测面板。</p>
      </div>
      <form class="filters" id="profit-console-form">
        <div class="field"><label for="profit-shop">店铺</label>
          <select id="profit-shop" name="shop_id">${shopOptions}</select>
        </div>
        <div class="field"><label for="profit-from">从</label>
          <input id="profit-from" name="from" type="date" value="${esc(state.profitFrom)}" />
        </div>
        <div class="field"><label for="profit-to">到</label>
          <input id="profit-to" name="to" type="date" value="${esc(state.profitTo)}" />
        </div>
        <button class="btn secondary" type="submit" id="btn-profit-query">查询</button>
      </form>
    </div>
    <div id="profit-summary-cards" class="grid-4" style="margin-bottom:12px">
      ${cards
        .map(
          ([title, value, hint]) => `
        <article class="card">
          <h3>${esc(title)}</h3>
          <div class="metric">${value === MISSING_LABEL ? `<span class="missing">${MISSING_LABEL}</span>` : esc(value)}</div>
          <p class="hint">${esc(hint)}</p>
        </article>`
        )
        .join("")}
    </div>
    <div id="profit-alerts">${alertHtml}</div>
    <p class="muted" id="profit-summary">订单 ${esc(profit.order_count ?? 0)} · 缺数据 ${esc(
      profit.incomplete_order_count ?? 0
    )} · 单位 THB（API 为 satang/100） · 只读</p>
    <section class="panel">
      <div class="panel-head"><h2>合计</h2><span class="muted">与 API totals 同字段</span></div>
      <div class="panel-body" id="profit-totals">${profitAggTable(
        [Object.assign({}, totals, { key: "合计" })],
        "范围"
      )}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>按日</h2></div>
      <div class="panel-body" id="profit-by-day">${profitAggTable(profit.by_day || [], "日期")}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>按周</h2></div>
      <div class="panel-body" id="profit-by-week">${profitAggTable(profit.by_week || [], "ISO 周")}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>订单明细</h2><span class="muted">${esc(profit.order_count ?? 0)} 行</span></div>
      <div class="panel-body" id="profit-orders">${profitOrderTable(profit.orders || [])}</div>
    </section>
    <footer class="profit-foot" id="profit-rates-note">${esc(footnote)}</footer>
  `;
}

function pageWorkflows() {
  const runs = state.runs || [];
  const detail = state.selectedRun;
  const canApprove = state.user && state.user.role === "owner";
  const canWrite = state.user && state.user.role !== "viewer";
  const listHtml = runs.length
    ? `<table><thead><tr><th>状态</th><th>当前步</th><th>流程</th><th>创建</th></tr></thead><tbody>
      ${runs
        .map((r) => {
          const active = detail && detail.id === r.id ? ' style="background:var(--bg)"' : "";
          return `<tr class="clickable" data-run="${esc(r.id)}"${active}>
            <td><span class="tag ${esc(r.status)}">${esc(r.status)}</span></td>
            <td>${esc(r.current_step || "—")}</td>
            <td>${esc(r.flow_id)}</td>
            <td>${esc(r.created_at)}</td>
          </tr>`;
        })
        .join("")}
      </tbody></table>`
    : emptyState("暂无运行", "点「一键 mock 演示」把 Scout→定价→Stylist→QA→审批→上架→订单/代发/物流→客服写入本库。");

  let detailHtml = emptyState("选择运行", "左侧点一行查看时间线。");
  if (detail) {
    const timeline = detail.timeline || detail.steps || [];
    const awaiting = detail.status === "awaiting_approval";
    detailHtml = `
      <div class="panel-head">
        <h2>${esc(detail.status)} · ${esc(detail.current_step || "—")}</h2>
        <span class="muted">${esc(detail.id)}</span>
      </div>
      <div class="panel-body">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
          <button class="btn secondary" type="button" id="btn-run-retry" ${canWrite ? "" : "disabled"}>重试当前步</button>
          <button class="btn danger" type="button" id="btn-run-cancel" ${canWrite ? "" : "disabled"}>取消</button>
          ${
            awaiting
              ? `<button class="btn" type="button" id="btn-run-approve" ${canApprove ? "" : "disabled"}>通过审批</button>
                 <button class="btn danger" type="button" id="btn-run-reject" ${canApprove ? "" : "disabled"}>打回</button>
                 ${canApprove ? "" : '<span class="muted">仅 owner 可审批</span>'}`
              : ""
          }
        </div>
        ${
          awaiting
            ? `<div class="field"><label>审批说明（打回必填）</label>
               <textarea id="run-decision-note" placeholder="通过可写简短说明；打回必须写原因"></textarea></div>`
            : ""
        }
        <h3>时间线</h3>
        ${
          timeline.length
            ? timeline
                .map((s) => {
                  let rationale = s.rationale || "";
                  if (!rationale && Array.isArray(s.ai_trace)) {
                    const hit = s.ai_trace.find((t) => t && t.rationale);
                    rationale = (hit && hit.rationale) || "";
                  } else if (!rationale && s.ai_trace && s.ai_trace.rationale) {
                    rationale = s.ai_trace.rationale;
                  }
                  return `<article class="card" style="margin-bottom:10px">
                    <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
                      <strong>${esc(s.label || s.step_id)}</strong>
                      <span class="tag ${esc(s.status)}">${esc(s.status)}</span>
                    </div>
                    <p class="muted" style="margin:6px 0">尝试 ${esc(s.attempt)} · 成本 $${esc(
                      s.cost_usd ?? "0"
                    )} · ${esc(s.started_at || s.created_at || "")}</p>
                    <p style="margin:0 0 8px"><strong>AI 理由：</strong>${esc(rationale || "—")}</p>
                    <div class="diff">
                      <div><div class="muted" style="margin-bottom:4px">输入</div><pre>${esc(
                        JSON.stringify(s.input || {}, null, 2)
                      )}</pre></div>
                      <div><div class="muted" style="margin-bottom:4px">输出</div><pre>${esc(
                        JSON.stringify(s.output || {}, null, 2)
                      )}</pre></div>
                    </div>
                  </article>`;
                })
                .join("")
            : emptyState("无步骤", "启动后会出现 Scout / Stylist / 审批等步骤。")
        }
      </div>
    `;
  }

  return `
    <div class="page-head">
      <div>
        <h1>AI 工作流</h1>
        <p>编排器 run 列表、时间线与审批挂起。不打开真实发货/退款/TikTok 写入。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" id="btn-demo-orch" ${canWrite ? "" : "disabled"} title="${
          canWrite ? "强制 WORKFLOW_PLATFORM=mock，不外呼，TTS_WRITE 保持关闭" : "只读账号不能触发演示"
        }">一键 mock 演示</button>
        <button class="btn secondary" type="button" id="btn-start-run" ${canWrite ? "" : "disabled"}>启动流水线</button>
      </div>
    </div>
    <div class="split">
      <section class="panel"><div class="panel-head"><h2>运行列表</h2></div><div class="panel-body">${listHtml}</div></section>
      <section class="panel" id="run-detail">${detailHtml}</section>
    </div>
  `;
}

function healthDotClass(health) {
  if (health === "bad") return "bad";
  if (health === "warn") return "warn";
  if (health === "idle") return "idle";
  return "";
}

function llmLine(llm) {
  if (!llm || llm.status === "missing") return "LLM 预算 · 缺数据";
  const tokens = Number(llm.prompt_tokens || 0) + Number(llm.completion_tokens || 0);
  const cost = llm.cost_usd == null ? "—" : Number(llm.cost_usd).toFixed(4);
  return `LLM ${tokens} tokens · $${cost}`;
}

function pageAgents() {
  const stats = state.agentStats || { agents: [], runs: [], budget: {}, task_logs: {} };
  const agents = stats.agents || [];
  const runs = stats.runs || [];
  const detail = state.agentRun;
  const anyBad = agents.some((a) => a.health === "bad");
  const anyWarn = agents.some((a) => a.health === "warn");
  const bannerOk = !anyBad && !anyWarn;
  const banner = anyBad
    ? "有代理失败 · 数字来自已落库的编排记录"
    : anyWarn
      ? "有代理挂起、待审批或 QA 打回"
      : "只读统计 · 数字来自编排 run、步骤、审批表与 LLM 用量";
  const budget = stats.budget || {};
  const budgetText =
    budget.limit_status === "missing"
      ? "日预算上限 · 缺数据"
      : `日预算上限 $${Number(budget.daily_limit_usd).toFixed(2)}`;
  const spent = Number(budget.spent_in_range_usd || 0).toFixed(4);

  const cards = agents.length
    ? agents
        .map((agent) => {
          const c = agent.counts || {};
          const arts = agent.recent_artifacts || [];
          const artHtml = arts.length
            ? `<ul class="artifact-list">${arts
                .map(
                  (art) =>
                    `<li><button type="button" class="linkish" data-agent-run="${esc(
                      art.run_id
                    )}" title="${esc(art.href || "")}">${esc(art.label)}</button></li>`
                )
                .join("")}</ul>`
            : `<p class="hint">最近产物 · 暂无</p>`;
          return `<section class="card agent-card" data-agent="${esc(agent.id)}">
            <div class="health-top">
              <span class="health-dot ${healthDotClass(agent.health)}"></span>
              <span class="health-name">${esc(agent.name)}</span>
              <span class="tag-mono">${esc(agent.role || agent.id)}</span>
              <span class="tag-mono agent-kind">${esc(agent.kind || "")}</span>
            </div>
            <div class="health-desc">${esc(agent.description || "")}</div>
            <div class="agent-metrics">
              <div><strong>${esc(c.runs ?? 0)}</strong><span>运行</span></div>
              <div><strong>${esc(c.succeeded ?? 0)}</strong><span>成功</span></div>
              <div><strong>${esc(c.qa_rejected ?? 0)}</strong><span title="QA 打回">打回</span></div>
              <div><strong>${esc(c.awaiting_approval ?? 0)}</strong><span title="等待人工审批">待审</span></div>
              <div><strong>${esc(c.failed ?? 0)}</strong><span>失败</span></div>
            </div>
            <p class="hint">${esc(llmLine(agent.llm))}${
              c.suspended ? ` · 挂起 ${esc(c.suspended)}` : ""
            }</p>
            ${artHtml}
          </section>`;
        })
        .join("")
    : emptyState("暂无代理定义", "流水线步骤未加载。");

  const listHtml = runs.length
    ? `<table><thead><tr><th>Run</th><th>当前阶段</th><th>挂起原因</th><th>更新</th></tr></thead><tbody>
      ${runs
        .map((run) => {
          const active = detail && detail.run && detail.run.id === run.id ? ' style="background:var(--bg)"' : "";
          return `<tr class="clickable" data-agent-run="${esc(run.id)}"${active}>
            <td class="mono" title="${esc(run.id)}">${esc(String(run.id || "").slice(0, 8))}</td>
            <td>${esc(run.current_step_label || run.current_step || "—")}</td>
            <td><span class="tag ${esc(run.suspend_code || run.status)}">${esc(run.suspend_reason || "—")}</span></td>
            <td>${esc(run.updated_at || run.created_at || "")}</td>
          </tr>`;
        })
        .join("")}
      </tbody></table>`
    : emptyState(
        "暂无编排 run",
        "在「AI 工作流」启动一条流水线，或让 demo-orch --db 指向本服务的工作台库之后，这里出现真实计数。"
      );

  let detailHtml = emptyState("选择运行", "左侧点一行查看步骤时间线。本页只读。");
  if (detail && detail.run) {
    const steps = detail.steps || [];
    detailHtml = `
      <div class="panel-head">
        <h2>${esc(detail.run.suspend_reason || detail.run.status)} · ${esc(
          detail.run.current_step_label || "—"
        )}</h2>
        <span class="muted mono">${esc(detail.run.id)}</span>
      </div>
      <div class="panel-body">
        <p class="muted" style="margin-top:0">状态 ${esc(detail.run.status)} · 店 ${esc(
          detail.run.shop_id || "—"
        )} · 只读时间线，不含发货 / 退款 / 付款。</p>
        ${
          steps.length
            ? `<ol class="timeline">${steps
                .map((step) => {
                  const dot =
                    step.status === "failed"
                      ? "bad"
                      : step.status === "awaiting_approval" || step.status === "suspended"
                        ? "warn"
                        : step.status === "succeeded"
                          ? ""
                          : "idle";
                  const when = step.finished_at || step.started_at || "";
                  const err = step.error && step.error.message ? step.error.message : "";
                  return `<li>
                    <span class="health-dot ${dot}" style="margin-top:6px"></span>
                    <div>
                      <strong>${esc(step.label || step.step_id)}</strong>
                      <span class="tag ${esc(step.status)}">${esc(step.status)}</span>
                      <div class="muted">${esc(when)}${
                        step.rewrite_round ? ` · 重写 ${esc(step.rewrite_round)}` : ""
                      }</div>
                      ${err ? `<div class="error">${esc(err)}</div>` : ""}
                    </div>
                  </li>`;
                })
                .join("")}</ol>`
            : emptyState("无步骤", "这条 run 还没有步骤记录。")
        }
      </div>`;
  }

  return `
    <div class="banner ${bannerOk ? "" : "warn"}">${ICONS.check}<span>${esc(banner)}</span></div>
    <div class="page-head">
      <div>
        <h1>AI 代理</h1>
        <p>选品 → 定价 → 文案图片 → 质检 → 审批 → 上架 → 订单 → 代发 → 物流 → 客服。只读，不打开真写。</p>
      </div>
      <form class="filters" id="agent-stats-form">
        <div class="field"><label for="agent-from">从</label><input id="agent-from" name="from" type="date" value="${esc(
          state.agentFrom
        )}" /></div>
        <div class="field"><label for="agent-to">到</label><input id="agent-to" name="to" type="date" value="${esc(
          state.agentTo
        )}" /></div>
        <button class="btn secondary" type="submit" id="btn-agent-stats">查询</button>
      </form>
    </div>
    <p class="muted" style="margin:-6px 0 12px">${esc(budgetText)} · 区间内已记账 $${esc(
      spent
    )} · 任务轨迹 ${esc((stats.task_logs && stats.task_logs.events) || 0)} 条${
      stats.task_logs && stats.task_logs.business_tasks && stats.task_logs.business_tasks.label
        ? ` · 业务库任务日志 ${esc(stats.task_logs.business_tasks.label)}`
        : ""
    }</p>
    <div class="grid-agents">${cards}</div>
    <div class="split">
      <section class="panel"><div class="panel-head"><h2>编排 run</h2></div><div class="panel-body">${listHtml}</div></section>
      <section class="panel" id="agent-run-detail">${detailHtml}</section>
    </div>
  `;
}

const CS_CATEGORY_OPTIONS = [
  ["logistics_query", "物流查询 · 白名单"],
  ["tracking_status", "物流状态 · 白名单"],
  ["sla_reminder", "时效提醒 · 白名单"],
  ["refund", "退款 · 必审"],
  ["compensation", "赔偿 · 必审"],
  ["price_match", "差价 · 必审"],
  ["complaint", "投诉 · 必审"],
];

function csCategoryLabel(category) {
  const hit = CS_CATEGORY_OPTIONS.find((row) => row[0] === category);
  return hit ? hit[1].split(" · ")[0] : category || "—";
}

function pageCs() {
  const canWrite = state.user && state.user.role !== "viewer";
  const inbox = state.csInbox || [];
  const drafts = state.csDrafts || [];
  const pending = drafts.filter((d) => d.status === "pending_approval" && d.approval_id);
  const inboxHtml = inbox.length
    ? `<div class="table-wrap"><table><thead><tr><th>会话</th><th>类目</th><th>正文</th><th>时间</th><th></th></tr></thead><tbody>
      ${inbox
        .map(
          (m) => `<tr>
          <td>${esc(m.conversation_id)}</td>
          <td><span class="health-dot"></span> ${esc(csCategoryLabel(m.category))}</td>
          <td>${esc(m.body)}</td>
          <td>${esc(m.created_at || "")}</td>
          <td class="cs-actions"><button class="btn secondary pill" type="button" data-cs-draft-msg="${esc(m.id)}" ${
            canWrite ? "" : "disabled"
          }>生成草稿</button></td>
        </tr>`
        )
        .join("")}
      </tbody></table></div>`
    : emptyState("入站为空", "记一条本地 mock 消息，或点刷新。不会去拉 TikTok 客服。");
  const draftRows = (rows) =>
    rows.length
      ? `<div class="table-wrap"><table><thead><tr><th>类目</th><th>状态</th><th>话术</th><th>动作</th></tr></thead><tbody>
        ${rows
          .map((d) => {
            const sent = d.sent_stub || d.status === "sent_stub";
            const gate = d.whitelist
              ? `<span class="tag approved">白名单免审</span>`
              : `<span class="tag pending">必审</span>`;
            const actions = [];
            if (sent) {
              actions.push(`<span class="tag approved">sent_stub</span>`, `<span class="tag approved">dry_run</span>`);
            } else if (d.whitelist) {
              actions.push(
                `<button class="btn pill ok" type="button" data-cs-send="${esc(d.id)}" ${
                  canWrite ? "" : "disabled"
                }>stub 发送</button>`
              );
            }
            if (d.approval_id && !sent) {
              actions.push(
                `<a class="btn secondary pill" href="/admin/approvals?id=${encodeURIComponent(
                  d.approval_id
                )}" data-cs-approval="${esc(d.approval_id)}">查看审批</a>`
              );
            }
            const action = actions.length ? actions.join("") : "—";
            return `<tr>
              <td>${esc(csCategoryLabel(d.category))} ${gate}</td>
              <td><span class="tag ${esc(d.status)}">${esc(d.status)}</span></td>
              <td>${esc(d.body_th || d.body_en || "")}</td>
              <td class="cs-actions">${action}</td>
            </tr>`;
          })
          .join("")}
        </tbody></table></div>`
      : "";
  return `
    <div class="banner">${ICONS.check}<span>客服白名单在本地 stub。不外呼 TikTok 客服。</span></div>
    <div class="page-head">
      <div>
        <h1>客服</h1>
        <p>入站 inbox、话术草稿、白名单 stub 发送。非白名单建 auto_reply 审批。</p>
      </div>
      <button class="btn secondary pill" type="button" id="btn-refresh-cs">刷新</button>
    </div>
    ${state.csNote ? `<div class="banner">${esc(state.csNote)}</div>` : ""}
    ${state.csError ? `<p class="error" id="cs-form-error">${esc(state.csError)}</p>` : `<p class="error" id="cs-form-error"></p>`}
    <div class="split cs-layout">
      <section class="panel">
        <div class="panel-head"><h2>入站 inbox</h2><span class="muted">${inbox.length} 条 · 本地 mock</span></div>
        <div class="panel-body">${inboxHtml}</div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>草稿</h2><span class="muted">待审 ${pending.length}</span></div>
        <div class="panel-body">
          <h3 style="margin:0 0 8px">待审草稿</h3>
          ${
            pending.length
              ? draftRows(pending)
              : emptyState("没有待审草稿", "非白名单类目生成草稿后会出现在这里，并可跳到审批详情。")
          }
          <h3 style="margin:16px 0 8px">全部草稿</h3>
          ${drafts.length ? draftRows(drafts) : emptyState("暂无客服草稿", "从入站点「生成草稿」。")}
        </div>
      </section>
    </div>
    <section class="panel">
      <div class="panel-head"><h2>记入本地入站</h2><span class="muted">不外呼平台</span></div>
      <div class="panel-body">
        <form id="cs-inbox-form" class="cs-form">
          <div class="field"><label>会话 ID</label><input name="conversation_id" required placeholder="conv-1" ${
            canWrite ? "" : "disabled"
          } /></div>
          <div class="field"><label>类目</label>
            <select name="category" ${canWrite ? "" : "disabled"}>
              ${CS_CATEGORY_OPTIONS.map(
                ([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`
              ).join("")}
            </select>
          </div>
          <div class="field"><label>语言</label>
            <select name="lang" ${canWrite ? "" : "disabled"}>
              <option value="th">th</option>
              <option value="en">en</option>
              <option value="zh">zh</option>
            </select>
          </div>
          <div class="field"><label>订单号（可空）</label><input name="order_id" ${canWrite ? "" : "disabled"} /></div>
          <div class="field cs-form-wide"><label>买家消息</label><textarea name="body" required placeholder="พัสดุถึงไหนแล้วคะ" ${
            canWrite ? "" : "disabled"
          }></textarea></div>
          <div class="field cs-form-wide">
            <button class="btn pill" type="submit" ${canWrite ? "" : "disabled"}>记入本地入站</button>
            <p class="muted">${canWrite ? "只写本地 inbox，不外呼平台。" : "当前是只读账号，不能写入站或发送。"}</p>
          </div>
        </form>
      </div>
    </section>
    <footer class="page-foot" id="cs-constraints">不外呼 TikTok 客服；发货/退款仍 403；不打开 TTS_WRITE_*；涉钱类（refund/compensation/price_match/complaint）必审。</footer>
  `;
}

function outreachStatusLabel(status) {
  if (status === "pending") return "待你确认";
  if (status === "approved") return "已标记通过";
  if (status === "rejected") return "已标记拒绝";
  return status || "—";
}

function pageOutreach() {
  const canWrite = state.user && state.user.role !== "viewer";
  const drafts = state.outreachDrafts || [];
  const pending = drafts.filter((d) => d.status === "pending");
  const cards = drafts.length
    ? `<div class="outreach-grid">${drafts
        .map((d) => {
          const payload = d.payload || {};
          const message = payload.message || {};
          const terms = Array.isArray(message.terms) ? message.terms : [];
          const followups = Array.isArray(message.followups) ? message.followups : [];
          const termHtml = terms.length
            ? `<ul class="outreach-terms">${terms
                .map(
                  (term) =>
                    `<li><span>${esc(term.label || term.key || "")}</span> ${esc(term.value || "")}</li>`
                )
                .join("")}</ul>`
            : "";
          const followHtml = followups.length
            ? followups
                .map(
                  (item) =>
                    `<p class="outreach-copy"><strong>第 ${esc(item.day)} 天</strong> ${esc(
                      item.body_th || ""
                    )}<br><span class="muted">${esc(item.body_zh || "")}</span></p>`
                )
                .join("")
            : "";
          const actions =
            d.status === "pending"
              ? `<div class="outreach-actions">
                  <button class="btn pill ok" type="button" data-outreach-approve="${esc(d.id)}" ${
                    canWrite ? "" : "disabled"
                  }>标记通过</button>
                  <button class="btn secondary pill" type="button" data-outreach-reject="${esc(d.id)}" ${
                    canWrite ? "" : "disabled"
                  }>标记拒绝</button>
                </div>`
              : `<p class="muted">只改了状态。没有发送。</p>`;
          const runLink = payload.run_id
            ? `<a class="btn secondary pill" href="/admin/workflows?run=${encodeURIComponent(
                payload.run_id
              )}">工作流</a>`
            : "";
          return `<article class="outreach-card panel">
            <div class="outreach-meta">
              <span class="tag">${esc(payload.creator_type || d.kind || "达人")}</span>
              <span class="tag ${esc(d.status)}">${esc(outreachStatusLabel(d.status))}</span>
              ${runLink}
            </div>
            <h3>${esc(d.title || payload.creator_name || "达人")}</h3>
            <p class="outreach-copy">${esc(message.dm_th || d.body_th || "")}</p>
            <p class="muted outreach-copy">${esc(message.dm_zh || "")}</p>
            <p class="muted outreach-copy">${esc(message.dm_en || d.body_en || "")}</p>
            <p class="outreach-copy"><strong>样品</strong> ${esc(message.sample_th || "")}<br><span class="muted">${esc(
              message.sample_zh || ""
            )}</span></p>
            ${termHtml}
            ${followHtml}
            <p class="muted">${esc(message.closing_note || "")}</p>
            ${actions}
          </article>`;
        })
        .join("")}</div>`
    : emptyState(
        "还没有达人建联草稿",
        "跑一条 AI 工作流或一键 mock 演示后，通过 QA 的商品会在这里出现。草稿不会自动发送。"
      );
  return `
    <div class="banner">${ICONS.check}<span>达人建联只产草稿。不会自动发送，也不会外呼 TikTok。</span></div>
    <div class="page-head">
      <div>
        <h1>达人建联</h1>
        <p>泰语邀约草稿待你确认。通过或拒绝只改状态，不发送私信、不寄样、不付款。</p>
      </div>
      <button class="btn secondary pill" type="button" id="btn-refresh-outreach">刷新</button>
    </div>
    ${state.outreachNote ? `<div class="banner">${esc(state.outreachNote)}</div>` : ""}
    ${
      state.outreachError
        ? `<p class="error" id="outreach-form-error">${esc(state.outreachError)}</p>`
        : `<p class="error" id="outreach-form-error"></p>`
    }
    <section class="panel">
      <div class="panel-head">
        <h2>草稿</h2>
        <span class="muted">待你确认 ${pending.length} / ${drafts.length}</span>
      </div>
      <div class="panel-body">
        <div class="field">
          <label for="outreach-note">拒绝原因</label>
          <input id="outreach-note" placeholder="标记拒绝时必填" ${canWrite ? "" : "disabled"} />
        </div>
        <p class="muted">${canWrite ? "通过不需要原因。两种决定都不发送。" : "当前是只读账号，不能改状态。"}</p>
      </div>
    </section>
    ${cards}
    <footer class="page-foot" id="outreach-constraints">不自动发送；不外呼 TikTok；不打开 TTS_WRITE_*；批准只改状态，不发送私信、不寄样、不付款。</footer>
  `;
}

// P1-07：每日上架上限默认只有 1，而此前这个数字界面上完全看不到 ——
// owner 只能靠「上架被拒」才知道今天用完了。这里把已用/上限摆出来。
// P2-12：只读展示各开关，敏感值由后端打码。**刻意不提供任何写入口** ——
// 真写必须在 .env 手动开启，UI 一键打开会让误触代价不可逆。
function renderSettingsGroups(view) {
  if (!view || !view.groups) return "";
  const groups = view.groups
    .map(
      (g) => `
      <h3 style="margin-top:16px">${esc(g.label)}</h3>
      <table>
        ${(g.items || [])
          .map(
            (it) => `<tr>
            <td>${it.secret ? "🔒 " : ""}${esc(it.name)}</td>
            <td>${it.value === "" || it.value === undefined ? '<span class="muted">未设置</span>' : esc(it.value)}</td>
            <td class="muted">${esc(it.detail || "")}</td>
          </tr>`
          )
          .join("")}
      </table>`
    )
    .join("");
  return `${groups}<p class="muted" style="margin-top:12px">${esc(view.note || "")}</p>`;
}

function renderPiiLast(view) {
  const p = view && view.pii_retention;
  if (!p || !p.ran) {
    return `<span class="muted">${esc((p && p.summary) || "尚未执行")}</span>`;
  }
  return `${esc(p.ran_at || "")}<div class="muted">${esc(p.summary || "")}</div>`;
}

function renderQuotaCell(q) {
  if (!q) return '<span class="muted">加载中…</span>';
  if (!q.available) return `<span class="muted">${esc(q.reason || "不适用")}</span>`;
  const used = q.used === null || q.used === undefined ? "?" : q.used;
  const limit = q.limit === null || q.limit === undefined ? "?" : q.limit;
  const tone = q.exhausted ? "failed" : "approved";
  const label = q.exhausted ? "今日已用完" : `剩 ${q.remaining}`;
  return `${used} / ${limit} <span class="tag ${tone}">${esc(label)}</span>`;
}

function pageSettings() {
  const s = state.status || {};
  return `
    <div class="page-head"><div><h1>设置</h1><p>环境与能力开关（只读展示）。</p></div></div>
    <section class="panel"><div class="panel-body">
      <table>
        <tr><th>项</th><th>值</th></tr>
        <tr><td>TikTok 模式</td><td>${esc(s.tiktok_mode)}</td></tr>
        <tr><td>平台适配器</td><td>${esc(s.platform)}</td></tr>
        <tr><td>LLM</td><td>${esc(s.llm_provider)} ${s.llm_degraded ? "(降级)" : ""}</td></tr>
        <tr><td>时区</td><td>${esc(s.timezone)}</td></tr>
        <tr><td>上架写通道</td><td>${s.publish_enabled ? "可（仍须审批）" : "关"}</td></tr>
        <tr><td>TTS 真写武装</td><td>${s.tts_write_armed ? "是" : "否"}</td></tr>
        <tr><td>今日写配额</td><td>${renderQuotaCell(state.ttsQuota)}</td></tr>
        <tr><td>发货</td><td>禁用（去卖家中心）</td></tr>
        <tr><td>退款</td><td>禁用（去卖家中心）</td></tr>
        <tr><td>上次 PII 清理</td><td>${renderPiiLast(state.settings)}</td></tr>
        <tr><td>当前用户</td><td>${esc(state.user?.username)} / ${esc(state.user?.role)}</td></tr>
      </table>
      ${renderSettingsGroups(state.settings)}
      <p class="muted" style="margin-top:12px">${esc(s.note || "")}</p>
      <p class="muted">开发自测面板：<a href="/workflow">/workflow</a></p>
    </div></section>
  `;
}

function shortTime(iso) {
  if (!iso) return "缺数据";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso).slice(0, 16);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function runTagClass(displayStatus, rawStatus) {
  if (displayStatus === "ok") return "succeeded";
  if (displayStatus === "waiting_approval") return "awaiting_approval";
  if (displayStatus === "failed") return "failed";
  if (displayStatus === "suspended") return "suspended";
  return rawStatus || "";
}

function runDot(displayStatus) {
  if (displayStatus === "failed") return "bad";
  if (displayStatus === "waiting_approval" || displayStatus === "suspended") return "warn";
  if (displayStatus === "ok") return "";
  return "idle";
}

function summaryBlock(title, items) {
  if (!items || !items.length) return "";
  return `<div class="run-summary"><div class="muted">${esc(title)}</div>${items
    .map((item) => `<div><span class="k">${esc(item.label)}</span>${esc(item.value)}</div>`)
    .join("")}</div>`;
}

function stepLlmLine(llm) {
  if (!llm || llm.status !== "recorded") return `<span class="missing">LLM · 缺数据</span>`;
  const tokens = Number(llm.prompt_tokens || 0) + Number(llm.completion_tokens || 0);
  const cost = llm.cost_usd == null ? "—" : Number(llm.cost_usd).toFixed(4);
  const model = llm.model ? ` · ${esc(llm.model)}` : "";
  return `LLM ${esc(tokens)} tokens · $${esc(cost)}${model}`;
}

function errorDataBlock(err) {
  if (!err) return "";
  return `<pre class="error-data" data-error-code="${esc(err.code || "")}">${esc(
    JSON.stringify(err, null, 2)
  )}</pre>`;
}

function approvalQueueLink(approval, className) {
  if (!approval || !approval.id) return "";
  const label = approval.pending ? "去审批队列" : "查看审批";
  const href = approval.href || `/admin/approvals?id=${encodeURIComponent(approval.id)}`;
  return `<a class="${className}" href="${esc(href)}" data-orch-approval="${esc(approval.id)}">${label}</a>`;
}

function pageRuns() {
  const runs = state.orchRuns || [];
  const detail = state.orchRun;
  const run = detail && detail.run;
  const counts = { ok: 0, waiting: 0, failed: 0 };
  runs.forEach((item) => {
    if (item.display_status === "ok") counts.ok += 1;
    else if (item.display_status === "waiting_approval") counts.waiting += 1;
    else if (item.display_status === "failed") counts.failed += 1;
  });
  const bannerWarn = run && (run.display_status === "failed" || run.display_status === "waiting_approval");
  const banner = !run
    ? "只读时间线 · 数字来自已落库的编排记录 · 不打开 TikTok 真写"
    : run.display_status === "waiting_approval"
      ? "等待人工审批 · 从这里进入审批队列，本页不代批"
      : run.display_status === "failed"
        ? "有步骤失败 · 错误以数据返回，未外呼平台"
        : "这条运行的时间线只读 · 不打开 TikTok 真写";
  const cards = [
    ["最近运行", runs.length, "本页列表", runs.length ? "" : "idle"],
    ["成功", counts.ok, "终态成功", counts.ok ? "" : "idle"],
    ["等待人工审批", counts.waiting, "链到审批队列", counts.waiting ? "warn" : "idle"],
    ["失败", counts.failed, "错误以数据返回", counts.failed ? "bad" : "idle"],
  ];
  const listHtml = runs.length
    ? `<table><thead><tr><th>状态</th><th>商品</th><th>开始</th><th>耗时</th><th>停在</th></tr></thead><tbody>
      ${runs
        .map((item) => {
          const active = run && run.id === item.id ? ' style="background:var(--bg)"' : "";
          const product = (item.product && item.product.title_label) || "缺数据";
          const queue = item.pending_approval
            ? `<div>${approvalQueueLink(item.pending_approval, "queue-link")}</div>`
            : "";
          return `<tr class="clickable" data-orch-run="${esc(item.id)}"${active}>
            <td><span class="tag ${esc(runTagClass(item.display_status, item.status))}">${esc(
              item.display_label || item.status
            )}</span></td>
            <td title="${esc((item.product && item.product.url) || "")}">${esc(product)}</td>
            <td>${esc(shortTime(item.started_at))}</td>
            <td>${esc(item.duration_label || "缺数据")}</td>
            <td>${esc(item.stopped_step_label || "—")}${queue}</td>
          </tr>`;
        })
        .join("")}
      </tbody></table>`
    : emptyState("暂无编排运行", "在总览点「一键 mock 演示」，跑完后这里出现完整时间线。");

  let detailHtml = emptyState("选择运行", "左侧点一行查看每个 agent 步骤。本页只读。");
  if (run) {
    const steps = detail.timeline || [];
    detailHtml = `
      <div class="panel-head">
        <h2>${esc(run.display_label || run.status)} · ${esc(run.stopped_step_label || "—")}</h2>
        <span class="muted mono">${esc(run.id)}</span>
      </div>
      <div class="panel-body">
        <p class="muted" style="margin-top:0">商品 ${esc(
          (run.product && run.product.title_label) || "缺数据"
        )} · 开始 ${esc(shortTime(run.started_at))} · 耗时 ${esc(run.duration_label || "缺数据")}</p>
        ${run.pending_approval ? `<p>${approvalQueueLink(run.pending_approval, "btn secondary pill")}</p>` : ""}
        ${errorDataBlock(run.error)}
        <h3>时间线</h3>
        ${
          steps.length
            ? `<ol class="timeline" id="orch-run-timeline">${steps
                .map((step) => {
                  return `<li data-step="${esc(step.step_id)}" data-display-status="${esc(step.display_status)}">
                    <span class="health-dot ${runDot(step.display_status)}" style="margin-top:6px"></span>
                    <div>
                      <div class="step-head">
                        <strong>${esc(step.label || step.step_id)}</strong>
                        <span class="tag ${esc(runTagClass(step.display_status, step.status))}">${esc(
                          step.display_label || step.status
                        )}</span>
                      </div>
                      <div class="muted">${esc(shortTime(step.started_at || step.finished_at))} · ${esc(
                        step.duration_label || "缺数据"
                      )}${step.attempt ? ` · 尝试 ${esc(step.attempt)}` : ""}</div>
                      ${summaryBlock("输入", step.input_summary)}
                      ${summaryBlock("输出", step.output_summary)}
                      <p class="hint" style="margin:6px 0 0">${stepLlmLine(step.llm)}</p>
                      ${errorDataBlock(step.error)}
                      ${
                        step.approval
                          ? `<p style="margin:8px 0 0">${approvalQueueLink(step.approval, "btn secondary pill")}</p>`
                          : ""
                      }
                    </div>
                  </li>`;
                })
                .join("")}</ol>`
            : emptyState("无步骤", "这条 run 还没有步骤记录。")
        }
      </div>`;
  }

  return `
    <div class="banner ${bannerWarn ? "warn" : ""}" id="orch-run-banner">${ICONS.check}<span>${esc(banner)}</span></div>
    <div class="page-head">
      <div>
        <h1>编排运行</h1>
        <p>选品 → 定价 → 文案图片 → 质检 → 短视频脚本 → 达人建联 → 审批 → 上架 → 订单 → 代发 → 物流 → 客服。只读，不代批、不外呼。</p>
      </div>
    </div>
    <div class="grid-4 run-kpis" id="orch-run-health" style="margin-bottom:12px">
      ${cards
        .map(
          ([title, value, hint, tone]) => `
        <section class="card health-card">
          <div class="health-top">
            <span class="health-dot ${tone}"></span>
            <span class="health-name">${esc(title)}</span>
          </div>
          <div class="metric">${esc(value)}</div>
          <div class="health-desc">${esc(hint)}</div>
        </section>`
        )
        .join("")}
    </div>
    <div class="split">
      <section class="panel" id="orch-run-list"><div class="panel-head"><h2>最近运行</h2></div><div class="panel-body">${listHtml}</div></section>
      <section class="panel" id="orch-run-detail">${detailHtml}</section>
    </div>
    <footer class="page-foot">本页只读。不外呼 TikTok，不打开发货、退款或付款，真写开关保持关闭。待审单进入现有审批队列。</footer>
  `;
}

async function loadProfitPage() {
  await refreshShellData();
  try {
    const shops = await api("/api/workflow/shops");
    state.profitShops = shops.shops || [];
  } catch {
    state.profitShops = [{ shop_id: "default" }];
  }
  const params = new URLSearchParams();
  if (state.profitShop) params.set("shop_id", state.profitShop);
  if (state.profitFrom) params.set("from", state.profitFrom);
  if (state.profitTo) params.set("to", state.profitTo);
  const qs = params.toString();
  state.profit = await api("/api/workflow/profit" + (qs ? `?${qs}` : ""));
}

async function loadAgentStats() {
  const params = new URLSearchParams();
  if (state.agentFrom) params.set("from", state.agentFrom);
  if (state.agentTo) params.set("to", state.agentTo);
  const qs = params.toString();
  state.agentStats = await api("/api/admin/agents/stats" + (qs ? `?${qs}` : ""));
  const runs = (state.agentStats && state.agentStats.runs) || [];
  const current = state.agentRun && state.agentRun.run && state.agentRun.run.id;
  const pick = current && runs.some((run) => run.id === current) ? current : runs[0] && runs[0].id;
  if (pick) {
    state.agentRun = await api(`/api/admin/agents/runs/${encodeURIComponent(pick)}`);
  } else {
    state.agentRun = null;
  }
}

async function loadOutreach() {
  const data = await api("/api/workflow/outreach/drafts");
  state.outreachDrafts = data.drafts || [];
}

async function loadCs() {
  const [inbox, drafts] = await Promise.all([
    api("/api/workflow/cs/inbox"),
    api("/api/workflow/cs/drafts"),
  ]);
  state.csInbox = inbox.messages || [];
  state.csDrafts = drafts.drafts || [];
}

async function loadOrchRuns(selectId) {
  const data = await api("/api/admin/runs?limit=50");
  state.orchRuns = data.runs || [];
  const wanted = selectId || "";
  const pick = wanted || (state.orchRuns[0] && state.orchRuns[0].id);
  if (!pick) {
    state.orchRun = null;
    return;
  }
  try {
    state.orchRun = await api(`/api/admin/runs/${encodeURIComponent(pick)}`);
  } catch (err) {
    if (err.status === 404 && state.orchRuns[0] && state.orchRuns[0].id !== pick) {
      state.orchRun = await api(`/api/admin/runs/${encodeURIComponent(state.orchRuns[0].id)}`);
      return;
    }
    throw err;
  }
}

async function loadRuns(selectId) {
  const data = await api("/api/orchestrator/runs?limit=50");
  state.runs = data.runs || [];
  if (selectId) {
    state.selectedRun = await api(`/api/orchestrator/runs/${encodeURIComponent(selectId)}`);
  } else if (state.selectedRun && state.selectedRun.id) {
    state.selectedRun = await api(`/api/orchestrator/runs/${encodeURIComponent(state.selectedRun.id)}`);
  } else if (state.runs[0]) {
    state.selectedRun = await api(`/api/orchestrator/runs/${encodeURIComponent(state.runs[0].id)}`);
  } else {
    state.selectedRun = null;
  }
}

async function ensurePageData() {
  if (OPERATIONS_ROUTES[state.route]) { if (!state.shops.length) await loadShops(); await operationsWorkbench.load(); return; }
  if (state.route === "overview") {
    await refreshShellData();
  } else if (state.route === "workflows") {
    await refreshShellData();
    await loadRuns(runIdFromLocation() || (state.selectedRun && state.selectedRun.id));
  } else if (state.route === "runs") {
    await loadOrchRuns(runIdFromLocation() || (state.orchRun && state.orchRun.run && state.orchRun.run.id));
  } else if (state.route === "agents") {
    await loadAgentStats();
  } else if (state.route === "approvals") {
    await refreshShellData();
    await loadApprovals(approvalIdFromLocation() || (state.selectedApproval && state.selectedApproval.id));
  } else if (state.route === "cs") {
    await refreshShellData();
    await loadCs();
  } else if (state.route === "outreach") {
    await refreshShellData();
    await loadOutreach();
  } else if (state.route === "products") {
    await refreshShellData();
    const cat = await api("/api/workflow/catalog");
    state.catalog = cat.items || cat.catalog || [];
  } else if (state.route === "orders") {
    await refreshShellData();
    const shopAtRequest = state.activeShop || "default";
    const generation = state.shopGeneration;
    const shopQuery = `?shop_id=${encodeURIComponent(shopAtRequest)}`;
    try {
      const synced = await api(`/api/workflow/orders/synced${shopQuery}`);
      // 切店竞态：晚到的上一店响应不得覆盖当前店列表。
      if (generation !== state.shopGeneration || (state.activeShop || "default") !== shopAtRequest) return;
      state.syncedOrders = synced.orders || [];
      state.sla = synced.sla || (await api(`/api/workflow/orders/sla${shopQuery}`));
      if (generation !== state.shopGeneration || (state.activeShop || "default") !== shopAtRequest) return;
    } catch (err) {
      // 旧店请求失败不能清空已经切到的新店列表。
      if (generation !== state.shopGeneration || (state.activeShop || "default") !== shopAtRequest) return;
      // 当前店读取失败不能把上一店的行留在页面上当成当前店订单。
      state.syncedOrders = [];
      state.sla = null;
      throw err;
    }
  } else if (state.route === "scout") {
    await refreshShellData();
    const drafts = await api("/api/workflow/drafts");
    state.drafts = drafts.drafts || [];
  } else if (state.route === "spotcheck") {
    await refreshShellData();
    await loadSpotCheckSamples();
  } else if (state.route === "reports") {
    await refreshShellData();
    const reports = await api("/api/workflow/reports");
    state.reports = reports.reports || [];
    const shop = state.profitShop || "default";
    const profit = await api(`/api/workflow/profit?shop_id=${encodeURIComponent(shop)}`);
    state.profit = profit;
    const spend = await api(`/api/workflow/ad-spend?shop_id=${encodeURIComponent(shop)}`);
    state.adSpendRows = spend.rows || [];
  } else if (state.route === "profit") {
    await loadProfitPage();
  } else if (state.route === "settings") {
    await refreshShellData();
    // 配额是可观测项，取不到不该让整页打不开 —— 失败时按「不适用」呈现。
    try {
      state.ttsQuota = await api("/api/admin/tts-quota");
    } catch (err) {
      state.ttsQuota = { available: false, reason: "读取配额失败。" };
    }
    try {
      state.settings = await api("/api/admin/settings");
    } catch (err) {
      state.settings = null;
    }
  }
}

function bindPageActions() {
  const orderHost = document.getElementById("order-workbench");
  if (orderHost) {
    orderWorkbench.attach(orderHost, state.user?.role);
    const openOrder = (id) => {
      history.replaceState({}, "", `/admin/orders?order=${encodeURIComponent(id)}`);
      state.selectedOrderId = id;
      document.querySelectorAll("[data-order-work]").forEach((button) => {
        button.classList.toggle("is-selected", button.dataset.orderWork === id);
      });
      orderWorkbench.shopId = state.activeShop || "default";
      orderWorkbench.open(id);
    };
    document.querySelectorAll("[data-order-work]").forEach((button) => {
      button.onclick = () => openOrder(button.dataset.orderWork);
    });
    const orderSearch = document.getElementById("order-search");
    const orderStatusFilter = document.getElementById("order-status-filter");
    const orderPlatformFilter = document.getElementById("order-platform-filter");
    const applyOrderFilter = () => {
      const query = String(orderSearch?.value || "").trim().toLowerCase();
      const status = String(orderStatusFilter?.value || "");
      const platform = String(orderPlatformFilter?.value || "");
      document.querySelectorAll("[data-order-work]").forEach((row) => {
        const matchesQuery = !query || String(row.dataset.orderSearch || "").includes(query);
        const matchesStatus = !status || row.dataset.orderStatus === status;
        const matchesPlatform = !platform || row.dataset.orderPlatform === platform;
        row.hidden = !(matchesQuery && matchesStatus && matchesPlatform);
      });
    };
    orderSearch?.addEventListener("input", applyOrderFilter);
    orderStatusFilter?.addEventListener("change", applyOrderFilter);
    orderPlatformFilter?.addEventListener("change", () => {
      state.orderPlatform = orderPlatformFilter.value;
      applyOrderFilter();
    });
    const shopeeCsv = document.getElementById("shopee-csv");
    shopeeCsv?.addEventListener("change", async () => {
      const file = shopeeCsv.files && shopeeCsv.files[0];
      if (!file) return;
      // 文件读取是异步的；必须绑定选择时的店铺，不能在 await 之后读可变状态。
      const shopAtSelect = state.activeShop || "default";
      const generationAtSelect = state.shopGeneration;
      try {
        const csvText = await file.text();
        if (generationAtSelect !== state.shopGeneration
            || (state.activeShop || "default") !== shopAtSelect) {
          shopeeCsv.value = "";
          state.pageError = "店铺已切换，已取消本次 CSV 导入，请重新选择文件。";
          render();
          return;
        }
        await api("/api/workflow/orders/import/shopee", {
          method: "POST",
          body: JSON.stringify({ csv_text: csvText, shop_id: shopAtSelect }),
        });
        shopeeCsv.value = "";
        await render();
      } catch (err) {
        state.pageError = err.message || "Shopee CSV 导入失败";
        render();
      }
    });
    const selected = new URLSearchParams(location.search).get("order");
    const selectedOrder = (state.syncedOrders || []).find(
      (order) => String(order.order_id || order.id) === selected,
    );
    if (selectedOrder && String(selectedOrder.platform || "tiktok") === "tiktok") openOrder(selected);
    else { state.selectedOrderId = ""; orderWorkbench.clear(); }
  }
  const refresh = document.getElementById("btn-refresh-overview");
  if (refresh) refresh.onclick = () => render();

  document.querySelectorAll("[data-approval]").forEach((row) => {
    row.addEventListener("click", () => {
      const id = row.getAttribute("data-approval");
      if (!id) return;
      navigate("approvals", { id });
    });
  });

  document.querySelectorAll("[data-agent-run]").forEach((row) => {
    row.addEventListener("click", async () => {
      const id = row.getAttribute("data-agent-run");
      if (!id) return;
      state.agentRun = await api(`/api/admin/agents/runs/${encodeURIComponent(id)}`);
      render();
    });
  });

  const agentForm = document.getElementById("agent-stats-form");
  if (agentForm) {
    agentForm.addEventListener("submit", (ev) => {
      ev.preventDefault();
      state.agentFrom = document.getElementById("agent-from")?.value || "";
      state.agentTo = document.getElementById("agent-to")?.value || "";
      state.agentRun = null;
      render();
    });
  }

  const profitForm = document.getElementById("profit-console-form");
  if (profitForm) {
    profitForm.addEventListener("submit", (ev) => {
      ev.preventDefault();
      state.profitShop = document.getElementById("profit-shop")?.value || "";
      state.profitFrom = document.getElementById("profit-from")?.value || "";
      state.profitTo = document.getElementById("profit-to")?.value || "";
      render();
    });
  }

  document.querySelectorAll("[data-orch-run]").forEach((row) => {
    row.addEventListener("click", async (ev) => {
      if (ev.target && ev.target.closest && ev.target.closest("[data-orch-approval]")) return;
      const id = row.getAttribute("data-orch-run");
      if (!id) return;
      const href = `/admin/runs?run=${encodeURIComponent(id)}`;
      if (`${location.pathname}${location.search}` !== href) history.pushState({}, "", href);
      state.orchRun = await api(`/api/admin/runs/${encodeURIComponent(id)}`);
      render();
    });
  });

  document.querySelectorAll("[data-orch-approval]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = el.getAttribute("data-orch-approval");
      if (!id) return;
      navigate("approvals", { id });
    });
  });

  document.querySelectorAll("[data-run]").forEach((row) => {
    row.addEventListener("click", async () => {
      const id = row.getAttribute("data-run");
      const href = `/admin/workflows?run=${encodeURIComponent(id)}`;
      if (`${location.pathname}${location.search}` !== href) history.pushState({}, "", href);
      state.selectedRun = await api(`/api/orchestrator/runs/${encodeURIComponent(id)}`);
      render();
    });
  });

  const startRun = document.getElementById("btn-start-run");
  if (startRun) {
    startRun.onclick = async () => {
      const created = await api("/api/orchestrator/runs", {
        method: "POST",
        body: JSON.stringify({ url: FIXTURE_URL }),
      });
      state.selectedRun = created;
      await loadRuns(created.id);
      render();
    };
  }
  const demoOrch = document.getElementById("btn-demo-orch");
  if (demoOrch) {
    demoOrch.onclick = async () => {
      if (state.user && state.user.role === "viewer") return;
      demoOrch.disabled = true;
      demoOrch.textContent = "演示运行中…";
      try {
        const created = await api("/api/admin/demo-orch", {
          method: "POST",
          body: "{}",
        });
        const id = created.run_id;
        const href = created.href || `/admin/runs?run=${encodeURIComponent(id)}`;
        if (`${location.pathname}${location.search}` !== href) history.pushState({}, "", href);
        render();
      } catch (err) {
        state.pageError = err.message || "演示失败";
        render();
      }
    };
  }
  const runApprove = document.getElementById("btn-run-approve");
  if (runApprove) {
    runApprove.onclick = async () => {
      const note = document.getElementById("run-decision-note")?.value || "";
      const id = state.selectedRun.id;
      await api(`/api/orchestrator/runs/${encodeURIComponent(id)}/approve`, {
        method: "POST",
        body: JSON.stringify({ note }),
      });
      await loadRuns(id);
      render();
    };
  }
  const runReject = document.getElementById("btn-run-reject");
  if (runReject) {
    runReject.onclick = async () => {
      const note = (document.getElementById("run-decision-note")?.value || "").trim();
      if (!note) {
        alert("打回必须填写原因");
        return;
      }
      const id = state.selectedRun.id;
      await api(`/api/orchestrator/runs/${encodeURIComponent(id)}/reject`, {
        method: "POST",
        body: JSON.stringify({ note }),
      });
      await loadRuns(id);
      render();
    };
  }
  const runCancel = document.getElementById("btn-run-cancel");
  if (runCancel) {
    runCancel.onclick = async () => {
      const id = state.selectedRun.id;
      await api(`/api/orchestrator/runs/${encodeURIComponent(id)}/cancel`, {
        method: "POST",
        body: JSON.stringify({ note: "admin cancel" }),
      });
      await loadRuns(id);
      render();
    };
  }
  const runRetry = document.getElementById("btn-run-retry");
  if (runRetry) {
    runRetry.onclick = async () => {
      const id = state.selectedRun.id;
      const stepId = state.selectedRun.current_step;
      if (!stepId) {
        alert("没有可重试的当前步骤");
        return;
      }
      await api(
        `/api/orchestrator/runs/${encodeURIComponent(id)}/steps/${encodeURIComponent(stepId)}/retry`,
        { method: "POST", body: "{}" }
      );
      await loadRuns(id);
      render();
    };
  }

  const btnSaveInputs = document.getElementById("btn-save-inputs");
  if (btnSaveInputs) {
    btnSaveInputs.addEventListener("click", async () => {
      const detail = state.draftDetail || {};
      const val = (id) => (document.getElementById(id) || {}).value || "";
      const msg = document.getElementById("pi-msg");
      try {
        const resp = await api(`/api/workflow/drafts/${detail.id}/inputs`, {
          method: "POST",
          body: JSON.stringify({
            category_id: val("pi-category"),
            warehouse_id: val("pi-warehouse"),
            price_amount: val("pi-price"),
            image_path: val("pi-image"),
          }),
        });
        if (msg) msg.textContent = `已保存：${(resp.saved || []).join("、") || "无"}（草稿已回到待审）`;
        await render();
      } catch (err) {
        if (msg) msg.textContent = `保存失败：${(err && err.message) || err}`;
      }
    });
  }

  const approve = document.getElementById("btn-approve");
  if (approve) {
    approve.onclick = async () => {
      const note = document.getElementById("decision-note")?.value || "";
      const id = state.selectedApproval.id;
      await api(`/api/workflow/approvals/${encodeURIComponent(id)}/approve`, {
        method: "POST",
        body: JSON.stringify({
          note,
          expected_hash: state.selectedApproval?.payload_hash || "",
        }),
      });
      await loadApprovals(id);
      render();
    };
  }
  const reject = document.getElementById("btn-reject");
  if (reject) {
    reject.onclick = async () => {
      const note = (document.getElementById("decision-note")?.value || "").trim();
      if (!note) {
        alert("打回必须填写原因");
        return;
      }
      const id = state.selectedApproval.id;
      await api(`/api/workflow/approvals/${encodeURIComponent(id)}/reject`, {
        method: "POST",
        body: JSON.stringify({ note }),
      });
      await loadApprovals(id);
      render();
    };
  }

  const importDemo = document.getElementById("btn-import-demo");
  if (importDemo) {
    importDemo.onclick = async () => {
      await api("/api/workflow/catalog/import-demo", { method: "POST", body: "{}" });
      render();
    };
  }
  const syncOrders = document.getElementById("btn-sync-orders");
  if (syncOrders) {
    syncOrders.onclick = async () => {
      try {
        await api("/api/workflow/orders/sync", { method: "POST", body: JSON.stringify({ shop_id: state.activeShop || "default" }) });
      } catch (err) {
        state.pageError = err.message || "订单同步失败";
      }
      render();
    };
  }
  const genListing = document.getElementById("btn-gen-listing");
  if (genListing) {
    genListing.onclick = async () => {
      await api("/api/workflow/drafts/generate", {
        method: "POST",
        body: JSON.stringify({
          kind: "listing",
          items: [{ name: "抽屉分隔盒", specs: "外径 20cm，材质 PP" }],
        }),
      });
      render();
    };
  }
  const spotScore = document.getElementById("btn-spot-score");
  if (spotScore) {
    spotScore.onclick = async () => {
      await scoreSpotCheck();
    };
  }

  const spotExport = document.getElementById("btn-spot-export");
  if (spotExport) {
    spotExport.onclick = () => {
      if (!state.spotCheckReport) {
        window.alert("请先点「计算通过率」，再导出。");
        return;
      }
      const blob = new Blob([state.spotCheckReport], { type: "text/markdown;charset=utf-8" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "m1-spot-check.md";
      link.click();
      URL.revokeObjectURL(link.href);
    };
  }

  const spotReload = document.getElementById("btn-spot-reload");
  if (spotReload) {
    spotReload.onclick = async () => {
      await loadSpotCheckSamples();
      render();
    };
  }

  const runResearch = document.getElementById("btn-run-research");
  if (runResearch) {
    runResearch.onclick = async () => {
      await api("/api/workflow/jobs/research", { method: "POST" });
      render();
    };
  }
  const jobResearch = document.getElementById("btn-job-research");
  if (jobResearch) {
    jobResearch.onclick = async () => {
      await api("/api/workflow/jobs/research", { method: "POST" });
      render();
    };
  }
  const jobDaily = document.getElementById("btn-job-daily");
  if (jobDaily) {
    jobDaily.onclick = async () => {
      await api("/api/workflow/jobs/daily", { method: "POST" });
      render();
    };
  }
  const adForm = document.getElementById("ad-spend-form");
  if (adForm) {
    adForm.onsubmit = async (ev) => {
      ev.preventDefault();
      const fd = new FormData(adForm);
      const metrics = String(fd.get("metrics") || "")
        .split(",")
        .map((part) => part.trim());
      const body = {
        shop_id: state.profitShop || "default",
        spend_date: String(fd.get("spend_date") || ""),
        amount_thb: String(fd.get("amount_thb") || ""),
        channel: String(fd.get("channel") || "tiktok_ads"),
        sku: String(fd.get("sku") || "") || null,
        impressions: metrics[0] ? Number(metrics[0]) : null,
        clicks: metrics[1] ? Number(metrics[1]) : null,
        conversions: metrics[2] ? Number(metrics[2]) : null,
      };
      const errEl = document.getElementById("ad-spend-form-error");
      try {
        await api("/api/workflow/ad-spend", { method: "POST", body: JSON.stringify(body) });
        if (errEl) errEl.textContent = "";
        render();
      } catch (err) {
        if (errEl) errEl.textContent = err.message || "记账失败";
      }
    };
  }
  const pullBtn = document.getElementById("btn-ads-pull");
  if (pullBtn) {
    pullBtn.onclick = async () => {
      const end = new Date();
      const start = new Date(end.getTime() - 6 * 86400000);
      const iso = (d) => d.toISOString().slice(0, 10);
      const errEl = document.getElementById("ad-spend-form-error");
      try {
        await api("/api/workflow/ad-spend/pull", {
          method: "POST",
          body: JSON.stringify({
            shop_id: state.profitShop || "default",
            date_from: iso(start),
            date_to: iso(end),
          }),
        });
        if (errEl) errEl.textContent = "";
        render();
      } catch (err) {
        if (errEl) errEl.textContent = err.message || "拉取失败";
      }
    };
  }

  const refreshCs = document.getElementById("btn-refresh-cs");
  if (refreshCs) refreshCs.onclick = () => render();

  const csForm = document.getElementById("cs-inbox-form");
  if (csForm) {
    csForm.onsubmit = async (ev) => {
      ev.preventDefault();
      const fd = new FormData(csForm);
      const errEl = document.getElementById("cs-form-error");
      try {
        await api("/api/workflow/cs/inbox", {
          method: "POST",
          body: JSON.stringify({
            conversation_id: String(fd.get("conversation_id") || "").trim(),
            category: String(fd.get("category") || "").trim(),
            body: String(fd.get("body") || "").trim(),
            lang: String(fd.get("lang") || "th").trim() || "th",
            order_id: String(fd.get("order_id") || "").trim() || null,
          }),
        });
        state.csNote = "已写入本地 mock inbox。未外呼 TikTok 客服。";
        state.csError = "";
        if (errEl) errEl.textContent = "";
        render();
      } catch (err) {
        state.csError = err.message || "写入失败";
        if (errEl) errEl.textContent = state.csError;
      }
    };
  }

  document.querySelectorAll("[data-cs-draft-msg]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-cs-draft-msg");
      try {
        const out = await api("/api/workflow/cs/draft", {
          method: "POST",
          body: JSON.stringify({ message_id: id }),
        });
        state.csNote = out.note || (out.whitelist ? "白名单免审。" : "已建 auto_reply 审批。");
        state.csError = "";
        render();
      } catch (err) {
        state.csError = err.message || "生成草稿失败";
        render();
      }
    });
  });

  document.querySelectorAll("[data-cs-send]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-cs-send");
      try {
        const out = await api(`/api/workflow/cs/drafts/${encodeURIComponent(id)}/send`, {
          method: "POST",
          body: "{}",
        });
        state.csNote = out.sent_stub
          ? "已本地 stub 发送（sent_stub / dry_run）。未外呼 TikTok 客服。"
          : out.note || "已处理。";
        state.csError = "";
        render();
      } catch (err) {
        state.csError = err.message || "发送失败";
        render();
      }
    });
  });

  const refreshOutreach = document.getElementById("btn-refresh-outreach");
  if (refreshOutreach) refreshOutreach.onclick = () => render();

  document.querySelectorAll("[data-outreach-approve]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-outreach-approve");
      try {
        const out = await api(`/api/workflow/outreach/drafts/${encodeURIComponent(id)}/approve`, {
          method: "POST",
          body: JSON.stringify({ note: document.getElementById("outreach-note")?.value || "" }),
        });
        state.outreachNote = out.sent
          ? ""
          : "已标记通过。没有发送私信，没有寄样，也没有付款。";
        state.outreachError = "";
        render();
      } catch (err) {
        state.outreachError = err.message || "标记失败";
        render();
      }
    });
  });

  document.querySelectorAll("[data-outreach-reject]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const note = (document.getElementById("outreach-note")?.value || "").trim();
      if (!note) {
        state.outreachError = "标记拒绝必须填写原因。";
        render();
        return;
      }
      const id = btn.getAttribute("data-outreach-reject");
      try {
        await api(`/api/workflow/outreach/drafts/${encodeURIComponent(id)}/reject`, {
          method: "POST",
          body: JSON.stringify({ note }),
        });
        state.outreachNote = "已标记拒绝。没有发送。";
        state.outreachError = "";
        render();
      } catch (err) {
        state.outreachError = err.message || "标记失败";
        render();
      }
    });
  });

  document.querySelectorAll("[data-cs-approval]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.preventDefault();
      const id = el.getAttribute("data-cs-approval");
      if (!id) return;
      navigate("approvals", { id });
    });
  });
}

const operationsWorkbench = new OperationsWorkbench(api, () => ({shop: state.activeShop || "default", generation: state.shopGeneration, route: state.route, user: state.user}));
let renderGeneration = 0;

async function render(reload = true) {
  const generation = ++renderGeneration;
  const root = document.getElementById("app");
  state.route = routeFromLocation();
  if (state.route !== "orders" || !state.user) orderWorkbench.clear();
  if (!OPERATIONS_ROUTES[state.route] || !state.user) operationsWorkbench.clear();
  if (state.route === "login" || !state.user) {
    if (!state.user && state.route !== "login") {
      // try session
      try {
        const res = await me();
        state.user = res.user;
        window.__CSRF__ = res.csrf_token || "";
      } catch {
        state.route = "login";
        history.replaceState({}, "", "/admin/login");
        renderLogin();
        return;
      }
    }
    if (state.route === "login") {
      renderLogin();
      return;
    }
  }

  if (OPERATIONS_ROUTES[state.route] && reload) {
    root.innerHTML = shell(`<p role="status" class="ops-empty">正在加载当前店铺运营数据…</p>`);
    bindShell();
  }
  try {
    if (reload) await ensurePageData();
  } catch (err) {
    if (err.status === 401) {
      state.user = null;
      state.route = "login";
      history.replaceState({}, "", "/admin/login");
      renderLogin();
      return;
    }
    state.pageError = err.message;
  }

  if (generation !== renderGeneration) return;
  const pages = {
    ...Object.fromEntries(Object.keys(OPERATIONS_ROUTES).map(k => [k, () => operationsWorkbench.render()])),
    overview: pageOverview,
    workflows: pageWorkflows,
    runs: pageRuns,
    agents: pageAgents,
    approvals: pageApprovals,
    products: pageProducts,
    orders: pageOrders,
    scout: pageScout,
    spotcheck: pageSpotCheck,
    reports: pageReports,
    profit: pageProfit,
    cs: pageCs,
    outreach: pageOutreach,
    settings: pageSettings,
  };
  const body = (pages[state.route] || pageOverview)();
  root.innerHTML = shell(body + (state.pageError ? `<p class="error">${esc(state.pageError)}</p>` : ""));
  state.pageError = "";
  bindShell();
  bindPageActions();
  if (OPERATIONS_ROUTES[state.route]) operationsWorkbench.bind(root, render);
}

window.addEventListener("popstate", () => render());
render();
