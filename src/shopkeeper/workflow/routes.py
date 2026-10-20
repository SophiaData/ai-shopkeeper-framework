"""卖家工作台路由。上架须人工通过后才调用写适配器；发货与退款固定关闭。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from shopkeeper import __version__
from shopkeeper.site_profile import SiteProfile, active_site_profile, profile_currency
from shopkeeper.workflow.admin_routes import mount_admin
from shopkeeper.workflow.auth_middleware import build_auth_middleware
from shopkeeper.workflow.auth_routes import mount_auth
from shopkeeper.workflow.drafts import DraftError
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.service import WorkflowService

# healthz 允许出现的键；禁止密钥/令牌类字段（单测钉死）。
HEALTHZ_KEYS = frozenset({"ok", "version", "publish_enabled"})

# raw string：否则 JS 里的 "\n" 会被 Python 解成真实换行，浏览器 SyntaxError，页面卡在「正在读取状态…」。
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>__WORKBENCH_TITLE__</title>
  <style>
    :root { color-scheme: light; font-family: "Segoe UI", "PingFang SC", sans-serif; }
    body { margin: 0; background: #f4f1ea; color: #1c1915; }
    header, main { padding: 20px 24px; }
    header { background: #1f3d36; color: #f7f3ea; }
    h1 { margin: 0 0 8px; font-size: 22px; }
    h2 { font-size: 18px; margin: 0 0 12px; }
    section { background: white; border-radius: 12px; padding: 16px; margin: 0 0 16px; }
    .muted { color: #5c564c; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #efe6d6; margin-right: 8px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #eee; vertical-align: top; }
    button { margin-right: 8px; padding: 6px 10px; border-radius: 8px; border: 1px solid #1f3d36; background: #1f3d36; color: white; }
    button.secondary { background: white; color: #1f3d36; }
    textarea, input, select { width: 100%; box-sizing: border-box; margin: 6px 0 12px; padding: 8px; }
    pre { white-space: pre-wrap; background: #f7f4ee; padding: 12px; border-radius: 8px; }
    .bad { color: #8a2b2b; }
    .warn { color: #8a5a12; }
    .filters { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; margin: 0 0 12px; }
    .filters label { display: block; min-width: 140px; margin: 0; font-size: 13px; color: #5c564c; }
    .filters input, .filters select { margin: 4px 0 0; width: 160px; }
    .filters button { margin: 0; }
    footer.profit-foot { margin-top: 12px; padding-top: 12px; border-top: 1px solid #eee; font-size: 13px; color: #5c564c; }
  </style>
</head>
<body>
  <header>
    <h1>__WORKBENCH_HEADING__</h1>
    <p>本机首次使用可直接访问。创建管理员后请先到 /admin 登录；浏览器使用登录会话，不接收机器调用口令。</p>
    <p>人工审核：上架须先通过草稿再请求上架。发货与退款不会自动执行。</p>
    <p id="status">正在读取状态…</p>
  </header>
  <main>
    <section>
      <h2>订单 / 商品 / 库存</h2>
      <p id="platform-reason" class="muted"></p>
      <button id="sync-orders" class="secondary" type="button">只读同步订单</button>
      <button id="refresh-sla" class="secondary" type="button">刷新发货 SLA</button>
      <p class="muted">同步只写入本机缓存，不发货、不改平台订单。SLA 是本地提醒，飞书后置；超时单请去卖家中心打单。</p>
      <h3>发货 SLA 提醒</h3>
      <p id="sla-summary" class="muted"></p>
      <div id="sla-reminders"></div>
      <h3>快照订单</h3>
      <div id="orders"></div>
      <h3>已同步订单</h3>
      <div id="synced-orders"></div>
      <h3>商品</h3>
      <div id="products"></div>
      <h3>库存</h3>
      <div id="inventory"></div>
    </section>
    <section>
      <h2>任务流日志</h2>
      <p class="muted">看 mock agent 的 tool_calls。轮询 <code>/tasks/&#123;id&#125;/logs</code>，或 SSE <code>/tasks/&#123;id&#125;/logs/stream</code>。不触发上架或发货。</p>
      <label>1688 链接（夹具，不真抓）</label>
      <input id="demo-url" value="https://detail.1688.com/offer/600123456789.html" />
      <button id="run-scout" type="button">跑一次选品（mock）</button>
      <button id="refresh-tasks" class="secondary" type="button">刷新任务列表</button>
      <div id="task-list"></div>
      <pre id="task-logs" class="muted">还没有日志。</pre>
    </section>
    <section>
      <h2>生成草稿</h2>
      <p class="muted">质检通过后点「通过草稿」。listing 还要再点「请求上架」才会调用写适配器（mock 或干跑）。</p>
      <label>类型</label>
      <select id="kind">
        <option value="listing">上架文案</option>
        <option value="video_script">短视频脚本</option>
        <option value="customer_reply">客服回复</option>
      </select>
      <label>名称</label>
      <input id="name" placeholder="例如：抽屉分隔盒" />
      <label>你已经核对过的事实，或客户原话</label>
      <textarea id="specs" rows="4" placeholder="尺寸、材质。没测过的防水和承重不要写。"></textarea>
      <button id="generate" type="button">生成草稿</button>
    </section>
    <section>
      <h2>审批收件箱</h2>
      <p class="muted">待审 → 通过/拒绝。已通过的 listing 可请求上架。拒绝与过期不会写平台。</p>
      <div id="drafts"></div>
    </section>
    <section id="cs-replies">
      <h2>客服话术（白名单 / 待审）</h2>
      <p class="muted">物流查询等白名单可免审 stub 发送；退款/赔偿/差价/投诉必审。不外呼 TikTok 客服 API；发货/退款仍关闭。</p>
      <button id="refresh-cs" class="secondary" type="button">刷新客服草稿</button>
      <div id="cs-drafts"></div>
    </section>
    <section id="catalog">
      <h2>商品库</h2>
      <p class="muted">本地 listing / 商品一览（审批状态、平台商品 ID）。可从 Demo 交付包导入；闸门上架成功也会写入。可编辑本地 title/价格/币种/规格；保存后审批回 pending，须重新通过才能上架。不改已发布平台商品。</p>
      <button id="refresh-catalog" class="secondary" type="button">刷新商品库</button>
      <button id="import-demo" class="secondary" type="button">从 Demo 交付包导入</button>
      <p id="catalog-note" class="muted"></p>
      <div id="catalog-items"></div>
      <div id="catalog-edit" hidden>
        <h3>编辑商品（本地）</h3>
        <p class="muted">只改本机商品库。保存后 approval_status 回 pending；重新审批后才能 publish。</p>
        <input type="hidden" id="edit-id" />
        <label>标题</label>
        <input id="edit-title" />
        <label>价格</label>
        <input id="edit-price" />
        <label>币种</label>
        <input id="edit-currency" placeholder="__PROFILE_CURRENCY__" />
        <label>规格</label>
        <textarea id="edit-specs" rows="3"></textarea>
        <button id="edit-save" type="button">保存</button>
        <button id="edit-cancel" class="secondary" type="button">取消</button>
      </div>
    </section>
    <section>
      <h2>报表</h2>
      <button id="run-research" class="secondary" type="button">现在跑选品摘要</button>
      <button id="run-daily" class="secondary" type="button">现在跑日报</button>
      <div id="reports"></div>
    </section>
    <section id="profit">
      <h2>利润</h2>
      <p class="muted">只读订单级利润与日/周汇总（<code>GET /api/workflow/profit</code>）。已扣记账广告费，并显示 ROAS / ACOS / 扣广告后利润。低于保本只提醒。缺成本或费率标「缺数据」，不编造。本页不做付款、退款或提现，也不改广告预算。</p>
      <div class="filters">
        <label>店铺
          <select id="profit-shop">
            <option value="">全部店铺</option>
          </select>
        </label>
        <label>从
          <input id="profit-from" type="date" />
        </label>
        <label>到
          <input id="profit-to" type="date" />
        </label>
        <button id="refresh-profit" class="secondary" type="button">查询利润</button>
      </div>
      <p id="profit-summary" class="muted"></p>
      <div id="profit-alerts"></div>
      <h3>合计</h3>
      <div id="profit-totals"></div>
      <h3>按日</h3>
      <div id="profit-by-day"></div>
      <h3>按周</h3>
      <div id="profit-by-week"></div>
      <h3>订单明细</h3>
      <div id="profit-orders"></div>
      <footer class="profit-foot" id="profit-rates-note">费率说明将在查询后显示。</footer>
    </section>
  </main>
  <script>
    function apiHeaders(extra) {
      const headers = Object.assign({}, extra || {});
      const csrf = document.cookie.split('; ').find(item => item.startsWith('sk_csrf='));
      if (csrf) headers["X-CSRF-Token"] = decodeURIComponent(csrf.slice('sk_csrf='.length));
      return headers;
    }
    async function getJSON(url) {
      const response = await fetch(url, { headers: apiHeaders() });
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }
    function cell(value) {
      if (Array.isArray(value)) return escapeHtml(value.join("、"));
      return escapeHtml(value ?? "");
    }
    function table(rows, columns) {
      if (!rows.length) return "<p class='muted'>没有数据。</p>";
      const head = columns.map(([key, label]) => "<th>" + label + "</th>").join("");
      const body = rows.map(row => "<tr>" + columns.map(([key]) => "<td>" + cell(row[key]) + "</td>").join("") + "</tr>").join("");
      return "<table><thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table>";
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[ch]));
    }
    function escapeAttr(value) {
      return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
    }
    const MISSING_LABEL = "缺数据";
    function moneyCell(value) {
      if (value === MISSING_LABEL || value === null || value === undefined || value === "") {
        return "<span class='warn'>" + MISSING_LABEL + "</span>";
      }
      if (typeof value === "number" && Number.isFinite(value)) {
        return escapeHtml((value / 100).toFixed(2));
      }
      const n = Number(value);
      if (Number.isFinite(n) && String(value).trim() !== "") {
        return escapeHtml((n / 100).toFixed(2));
      }
      return "<span class='warn'>" + escapeHtml(value) + "</span>";
    }
    function marginCell(value) {
      if (value === MISSING_LABEL || value === null || value === undefined || value === "") {
        return "<span class='warn'>" + MISSING_LABEL + "</span>";
      }
      return escapeHtml(value) + "%";
    }
    function ratioCell(value) {
      if (value === null || value === undefined || value === "") return "—";
      return escapeHtml(value);
    }
    function statusCell(row) {
      if (row.data_status === "ok") return "完整";
      return "<span class='warn'>" + MISSING_LABEL + "</span>";
    }
    function profitAggTable(rows, keyLabel) {
      if (!rows.length) return "<p class='muted'>没有数据。</p>";
      const head = "<th>" + keyLabel + "</th><th>订单数</th><th>完整</th><th>缺数据</th><th>收入(THB)</th><th>佣金</th><th>支付费</th><th>广告占位</th><th>广告费</th><th>ROAS</th><th>ACOS</th><th>扣广告后利润</th><th>采购成本</th><th>运费</th><th>毛利</th><th>毛利率</th><th>状态</th>";
      const body = rows.map(row =>
        "<tr><td>" + escapeHtml(row.key || row.shop_id || "") +
        "</td><td>" + escapeHtml(row.orders) +
        "</td><td>" + escapeHtml(row.complete_orders) +
        "</td><td>" + escapeHtml(row.incomplete_orders) +
        "</td><td>" + moneyCell(row.revenue) +
        "</td><td>" + moneyCell(row.platform_commission) +
        "</td><td>" + moneyCell(row.payment_fee) +
        "</td><td>" + moneyCell(row.ads) +
        "</td><td>" + moneyCell(row.ad_spend) +
        "</td><td>" + ratioCell(row.ROAS) +
        "</td><td>" + ratioCell(row.ACOS) +
        "</td><td>" + moneyCell(row.profit_after_ads) +
        "</td><td>" + moneyCell(row.purchase_cost) +
        "</td><td>" + moneyCell(row.shipping) +
        "</td><td>" + moneyCell(row.gross_profit) +
        "</td><td>" + marginCell(row.gross_margin_pct) +
        "</td><td>" + statusCell(row) + "</td></tr>"
      ).join("");
      return "<table><thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table>";
    }
    function profitOrderTable(rows) {
      if (!rows.length) return "<p class='muted'>没有订单。</p>";
      const head = "<th>订单</th><th>店铺</th><th>日期</th><th>收入(THB)</th><th>佣金</th><th>支付费</th><th>广告占位</th><th>广告费</th><th>ROAS</th><th>ACOS</th><th>扣广告后利润</th><th>采购成本</th><th>运费</th><th>毛利</th><th>毛利率</th><th>状态</th>";
      const body = rows.map(row =>
        "<tr><td>" + escapeHtml(row.order_id) +
        "</td><td>" + escapeHtml(row.shop_id) +
        "</td><td>" + escapeHtml(row.date || "") +
        "</td><td>" + moneyCell(row.revenue) +
        "</td><td>" + moneyCell(row.platform_commission) +
        "</td><td>" + moneyCell(row.payment_fee) +
        "</td><td>" + moneyCell(row.ads) +
        "</td><td>" + moneyCell(row.ad_spend) +
        "</td><td>" + ratioCell(row.ROAS) +
        "</td><td>" + ratioCell(row.ACOS) +
        "</td><td>" + moneyCell(row.profit_after_ads) +
        "</td><td>" + moneyCell(row.purchase_cost) +
        "</td><td>" + moneyCell(row.shipping) +
        "</td><td>" + moneyCell(row.gross_profit) +
        "</td><td>" + marginCell(row.gross_margin_pct) +
        "</td><td>" + statusCell(row) + "</td></tr>"
      ).join("");
      return "<table><thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table>";
    }
    function renderProfitReport(report) {
      const totals = report.totals || {};
      document.getElementById("profit-summary").textContent =
        "订单 " + (report.order_count || 0) +
        " · 缺数据 " + (report.incomplete_order_count || 0) +
        " · 单位 THB（API 为 satang/100） · 只读";
      document.getElementById("profit-totals").innerHTML = profitAggTable(
        [Object.assign({}, totals, { key: "合计" })],
        "范围"
      );
      document.getElementById("profit-by-day").innerHTML = profitAggTable(report.by_day || [], "日期");
      document.getElementById("profit-by-week").innerHTML = profitAggTable(report.by_week || [], "ISO 周");
      document.getElementById("profit-orders").innerHTML = profitOrderTable(report.orders || []);
      const alerts = report.alerts || [];
      document.getElementById("profit-alerts").innerHTML = alerts.length
        ? alerts.map(item => "<p class='warn'>" + escapeHtml(item.message || "") + "（只提醒，不自动操作）</p>").join("")
        : "";
      const rates = report.rates || {};
      const rateBits = [
        "佣金 " + (rates.commission_pct ?? "?") + "%",
        "支付 " + (rates.payment_fee_pct ?? "?") + "%",
        "广告 " + (rates.ads_pct ?? "?") + "%",
        "FX " + (rates.fx_rate ?? "?") + "（" + (rates.fx_source || "env") + "）"
      ];
      document.getElementById("profit-rates-note").textContent =
        "占位费率（PRICING_*，非平台结算实收）：" + rateBits.join(" · ") +
        "。" + (rates.note || report.note || "") +
        " 本页无付款/退款/提现按钮。";
    }
    async function loadProfitShops() {
      const select = document.getElementById("profit-shop");
      const current = select.value;
      try {
        const body = await getJSON("/api/workflow/shops");
        const shops = body.shops || [];
        select.innerHTML = "<option value=\"\">全部店铺</option>" + shops.map(s =>
          "<option value=\"" + escapeAttr(s.shop_id) + "\">" + escapeHtml(s.shop_id) + "</option>"
        ).join("");
        if (current) select.value = current;
      } catch (err) {
        select.innerHTML = "<option value=\"\">全部店铺</option><option value=\"default\">default</option>";
      }
    }
    async function refreshProfit() {
      const shop = document.getElementById("profit-shop").value;
      const from = document.getElementById("profit-from").value;
      const to = document.getElementById("profit-to").value;
      const params = new URLSearchParams();
      if (shop) params.set("shop_id", shop);
      if (from) params.set("from", from);
      if (to) params.set("to", to);
      const qs = params.toString();
      const url = "/api/workflow/profit" + (qs ? "?" + qs : "");
      try {
        const report = await getJSON(url);
        renderProfitReport(report);
      } catch (err) {
        document.getElementById("profit-summary").textContent = "查询失败：" + (err && err.message ? err.message : err);
        document.getElementById("profit-totals").innerHTML = "";
        document.getElementById("profit-by-day").innerHTML = "";
        document.getElementById("profit-by-week").innerHTML = "";
        document.getElementById("profit-orders").innerHTML = "";
        document.getElementById("profit-alerts").innerHTML = "";
        document.getElementById("profit-rates-note").textContent = "占位费率说明在查询成功后显示。本页无付款/退款/提现按钮。";
      }
    }
    let catalogItems = [];
    function renderCatalog(items) {
      if (!items.length) return "<p class='muted'>没有数据。</p>";
      const head = "<th>标题</th><th>价格</th><th>币种</th><th>规格</th><th>审批状态</th><th>平台商品 ID</th><th>来源</th><th></th>";
      const body = items.map(row =>
        "<tr><td>" + escapeHtml(row.title) +
        "</td><td>" + escapeHtml(row.price_amount || "") +
        "</td><td>" + escapeHtml(row.currency || "") +
        "</td><td>" + escapeHtml(row.specs || "") +
        "</td><td>" + escapeHtml(row.approval_status) +
        "</td><td>" + escapeHtml(row.platform_product_id || "") +
        "</td><td>" + escapeHtml(row.source) +
        "</td><td><button class='secondary' type='button' data-catalog-edit='" + escapeAttr(row.id) + "'>编辑</button></td></tr>"
      ).join("");
      return "<table><thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table>";
    }
    async function refresh() {
      const status = await getJSON("/api/workflow/status");
      const publishLabel = status.publish_enabled ? "闸门上架可用（" + status.platform + "）" : "写通道未启用";
      document.getElementById("status").textContent =
        "TikTok：" + status.tiktok_mode + " · 文案：" + status.llm_provider +
        (status.llm_degraded ? "（已退回模板）" : "") +
        " · 时区 " + status.timezone + " · " + publishLabel + " · 发货/退款已关闭";
      const snap = await getJSON("/api/workflow/snapshot");
      document.getElementById("platform-reason").textContent = snap.reason || snap.orders_error || snap.products_error || "";
      document.getElementById("orders").innerHTML = table(snap.orders || [], [["order_id", "订单"], ["status", "状态"], ["item_names", "商品"], ["total_amount", "金额"], ["currency", "币种"]]);
      document.getElementById("products").innerHTML = table(snap.products || [], [["product_id", "商品"], ["title", "标题"], ["status", "状态"]]);
      document.getElementById("inventory").innerHTML = table(snap.inventory || [], [["product_id", "商品"], ["sku_id", "SKU"], ["available_stock", "可售库存"]]);
      const synced = await getJSON("/api/workflow/orders/synced");
      document.getElementById("synced-orders").innerHTML = table(synced.orders || [], [["order_id", "订单"], ["status", "状态"], ["_source", "来源"], ["_synced_at", "同步于"]]);
      const sla = synced.sla || await getJSON("/api/workflow/orders/sla");
      document.getElementById("sla-summary").textContent =
        "超时 " + (sla.overdue || 0) + " · 临期 " + (sla.due_soon || 0) + " · 窗口内 " + (sla.ok || 0) +
        " · " + (sla.note || "");
      const reminders = sla.reminders || [];
      document.getElementById("sla-reminders").innerHTML = reminders.length
        ? "<table><thead><tr><th>级别</th><th>订单</th><th>状态</th><th>截止</th><th>剩余小时</th><th>说明</th></tr></thead><tbody>" +
          reminders.map(r => {
            const cls = r.level === "overdue" ? "bad" : "warn";
            const label = r.level === "overdue" ? "超时" : "临期";
            return "<tr class='" + cls + "'><td>" + label + "</td><td>" + escapeHtml(r.order_id) +
              "</td><td>" + escapeHtml(r.status) + "</td><td>" + escapeHtml(r.ship_deadline || "") +
              "</td><td>" + escapeHtml(r.hours_left) + "</td><td>" + escapeHtml(r.message) + "</td></tr>";
          }).join("") + "</tbody></table>"
        : "<p class='muted'>没有临期或超时订单。</p>";
      const drafts = await getJSON("/api/workflow/drafts");
      document.getElementById("drafts").innerHTML = (drafts.drafts || []).map(draft => {
        const issues = (draft.qa.issues || []).map(escapeHtml).join("<br>");
        let actions = "";
        if (draft.status === "pending") {
          actions = "<button type='button' data-act='approve' data-id='" + draft.id + "'>通过草稿</button><button class='secondary' type='button' data-act='reject' data-id='" + draft.id + "'>退回</button>";
        } else if (draft.status === "needs_revision") {
          actions = "<button class='secondary' type='button' data-act='reject' data-id='" + draft.id + "'>退回</button>";
        } else if (draft.status === "approved" && draft.kind === "listing") {
          actions = "<button type='button' data-act='publish' data-id='" + draft.id + "'>请求上架</button>";
        }
        const pid = draft.platform_product_id ? "<p class='muted'>平台商品 ID：" + escapeHtml(draft.platform_product_id) + "</p>" : "";
        return "<article><p><span class='pill'>" + escapeHtml(draft.kind) + "</span><span class='pill'>" + escapeHtml(draft.status) + "</span><span class='pill'>" + escapeHtml(draft.source) + "</span></p><h3>" + escapeHtml(draft.title) + "</h3><p>" + escapeHtml(draft.body_th) + "</p><p class='muted'>" + escapeHtml(draft.body_en) + "</p><p class='bad'>" + issues + "</p>" + pid + actions + "</article>";
      }).join("") || "<p class='muted'>队列是空的。</p>";
      const reports = await getJSON("/api/workflow/reports");
      document.getElementById("reports").innerHTML = (reports.reports || []).map(report =>
        "<article><h3>" + escapeHtml(report.kind) + " · " + escapeHtml(report.created_at) + "</h3><pre>" + escapeHtml(report.body_md) + "</pre></article>"
      ).join("") || "<p class='muted'>还没有报表。定时任务在曼谷时间 08:30 和 21:00 写入，也可以现在手动跑。</p>";
      const catalog = await getJSON("/api/workflow/catalog");
      catalogItems = catalog.items || [];
      document.getElementById("catalog-note").textContent =
        "共 " + (catalog.count || 0) + " 条 · " + (catalog.note || "");
      document.getElementById("catalog-items").innerHTML = renderCatalog(catalogItems);
      const cs = await getJSON("/api/workflow/cs/drafts");
      const csDrafts = cs.drafts || [];
      document.getElementById("cs-drafts").innerHTML = csDrafts.length
        ? "<table><thead><tr><th>会话</th><th>类目</th><th>状态</th><th>白名单</th><th>已 stub 发送</th><th>摘要</th></tr></thead><tbody>" +
          csDrafts.map(d => {
            const wl = d.whitelist ? "是" : "否";
            const sent = d.sent_stub ? "是" : "否";
            const summary = (d.body_th || d.body_en || "").slice(0, 80);
            return "<tr><td>" + escapeHtml(d.conversation_id) +
              "</td><td>" + escapeHtml(d.category) +
              "</td><td>" + escapeHtml(d.status) +
              "</td><td>" + wl +
              "</td><td>" + sent +
              "</td><td>" + escapeHtml(summary) + "</td></tr>";
          }).join("") + "</tbody></table>"
        : "<p class='muted'>暂无客服草稿。可用 POST /api/workflow/cs/inbox 与 /cs/draft 写入。</p>";
      await loadProfitShops();
      await refreshProfit();
    }
    document.getElementById("refresh-cs").onclick = () => refresh();
    document.getElementById("refresh-profit").onclick = () => refreshProfit();
    document.getElementById("generate").onclick = async () => {
      const kind = document.getElementById("kind").value;
      const name = document.getElementById("name").value;
      const specs = document.getElementById("specs").value;
      const item = kind === "customer_reply" ? { name, message: specs } : { name, specs };
      await fetch("/api/workflow/drafts/generate", { method: "POST", headers: apiHeaders({ "content-type": "application/json" }), body: JSON.stringify({ kind, items: [item] }) });
      refresh();
    };
    document.getElementById("drafts").onclick = async (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      const act = button.dataset.act;
      const url = "/api/workflow/drafts/" + button.dataset.id + "/" + act;
      const response = await fetch(url, { method: "POST", headers: apiHeaders({ "content-type": "application/json" }), body: "{}" });
      if (!response.ok) alert(await response.text());
      refresh();
    };
    document.getElementById("sync-orders").onclick = async () => {
      const response = await fetch("/api/workflow/orders/sync", { method: "POST", headers: apiHeaders({ "content-type": "application/json" }), body: "{}" });
      if (!response.ok) alert(await response.text());
      refresh();
    };
    document.getElementById("refresh-sla").onclick = () => refresh();
    document.getElementById("refresh-catalog").onclick = () => refresh();
    document.getElementById("import-demo").onclick = async () => {
      const response = await fetch("/api/workflow/catalog/import-demo", {
        method: "POST",
        headers: apiHeaders({ "content-type": "application/json" }),
        body: "{}"
      });
      if (!response.ok) alert(await response.text());
      else {
        const body = await response.json();
        alert("导入 " + body.imported + " 条。" + (body.note || ""));
      }
      refresh();
    };
    document.getElementById("catalog-items").onclick = (event) => {
      const button = event.target.closest("button[data-catalog-edit]");
      if (!button) return;
      const id = button.getAttribute("data-catalog-edit");
      const row = catalogItems.find(item => item.id === id);
      if (!row) return;
      document.getElementById("catalog-edit").hidden = false;
      document.getElementById("edit-id").value = row.id;
      document.getElementById("edit-title").value = row.title || "";
      document.getElementById("edit-price").value = row.price_amount || "";
      document.getElementById("edit-currency").value = row.currency || "";
      document.getElementById("edit-specs").value = row.specs || "";
    };
    document.getElementById("edit-cancel").onclick = () => {
      document.getElementById("catalog-edit").hidden = true;
    };
    document.getElementById("edit-save").onclick = async () => {
      const id = document.getElementById("edit-id").value;
      const body = {
        title: document.getElementById("edit-title").value,
        price_amount: document.getElementById("edit-price").value,
        currency: document.getElementById("edit-currency").value,
        specs: document.getElementById("edit-specs").value
      };
      const response = await fetch("/api/workflow/catalog/" + encodeURIComponent(id), {
        method: "PATCH",
        headers: apiHeaders({ "content-type": "application/json" }),
        body: JSON.stringify(body)
      });
      if (!response.ok) {
        alert(await response.text());
        return;
      }
      document.getElementById("catalog-edit").hidden = true;
      refresh();
    };
    async function runJob(name) {
      await fetch("/api/workflow/jobs/" + name, { method: "POST", headers: apiHeaders() });
      refresh();
    }
    document.getElementById("run-research").onclick = () => runJob("research");
    document.getElementById("run-daily").onclick = () => runJob("daily");

    let logSource = null;
    function renderTaskList(tasks) {
      if (!tasks.length) {
        document.getElementById("task-list").innerHTML = "<p class='muted'>还没有任务。点上面按钮跑一次 mock 选品。</p>";
        return;
      }
      document.getElementById("task-list").innerHTML =
        "<table><thead><tr><th>任务</th><th>agent</th><th>状态</th><th></th></tr></thead><tbody>" +
        tasks.map(t => "<tr><td>" + escapeHtml(t.task_id) + "</td><td>" + escapeHtml(t.agent) + "</td><td>" + escapeHtml(t.status) +
          "</td><td><button type='button' data-task='" + escapeHtml(t.task_id) + "'>看日志</button></td></tr>").join("") +
        "</tbody></table>";
    }
    async function refreshTasks() {
      const body = await getJSON("/tasks?limit=20");
      renderTaskList(body.tasks || []);
    }
    function showLogs(lines) {
      document.getElementById("task-logs").textContent = lines.join("\n") || "（无 tool_calls）";
    }
    async function openTaskLogs(taskId) {
      if (logSource) { logSource.close(); logSource = null; }
      const polled = await getJSON("/tasks/" + taskId + "/logs");
      const lines = (polled.logs || []).map(l =>
        "#" + l.id + " " + l.adapter + "." + l.method + " [" + l.status + "] " + JSON.stringify(l.request || {})
      );
      lines.unshift("task=" + taskId + " status=" + polled.task_status + "（轮询）");
      showLogs(lines);
      logSource = new EventSource("/tasks/" + taskId + "/logs/stream");
      const live = lines.slice();
      logSource.addEventListener("log", (ev) => {
        const l = JSON.parse(ev.data);
        live.push("SSE #" + l.id + " " + l.adapter + "." + l.method + " [" + l.status + "]");
        showLogs(live);
      });
      logSource.addEventListener("done", () => { logSource.close(); logSource = null; });
      logSource.onerror = () => { if (logSource) { logSource.close(); logSource = null; } };
    }
    document.getElementById("refresh-tasks").onclick = () => refreshTasks();
    document.getElementById("task-list").onclick = (event) => {
      const button = event.target.closest("button[data-task]");
      if (!button) return;
      openTaskLogs(button.dataset.task);
    };
    document.getElementById("run-scout").onclick = async () => {
      const url = document.getElementById("demo-url").value;
      const response = await fetch("/products/from-1688", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const body = await response.json();
      await refreshTasks();
      if (body.task_id) openTaskLogs(body.task_id);
    };

    refresh().then(refreshTasks);
  </script>
</body>
</html>
"""


def healthz_payload(service: WorkflowService) -> dict[str, Any]:
    """无鉴权探活：仅 ok / 版本 / 写通道布尔，不含密钥。"""
    status = service.status()
    return {
        "ok": True,
        "version": __version__,
        "publish_enabled": bool(status.get("publish_enabled")),
    }


def render_workflow_page(profile: SiteProfile | None = None) -> str:
    """工作台 HTML。标题与页眉读站点画像；默认画像与接线前逐字相同。"""
    current = profile or active_site_profile()
    return (
        PAGE.replace("__WORKBENCH_TITLE__", f"{current.display_stem}工作台")
        .replace("__WORKBENCH_HEADING__", f"TikTok Shop {current.shop_display_name}工作台")
        .replace("__PROFILE_CURRENCY__", profile_currency(current))
    )


def mount_workflow(app: FastAPI, service: WorkflowService) -> None:
    # 会话/CSRF/角色门；/healthz 不在 /api/* 面，中间件放行（Docker/systemd 探活）。
    app.middleware("http")(build_auth_middleware(service.store))
    mount_auth(app, service.store)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return healthz_payload(service)

    def _backup_age_seconds() -> int | None:
        from shopkeeper.workflow.backup import backup_dir, latest_valid_backup_stamp

        try:
            root = backup_dir()
            stamp = latest_valid_backup_stamp(root)
        except Exception:
            return None
        if not stamp:
            return None
        try:
            ts = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        jobs = service.store.job_health()
        workers = service.store.worker_health()
        require_worker = str(os.getenv("WORKFLOW_REQUIRE_WORKER", "0")).strip().lower() in {"1", "true", "yes", "on"}
        require_backup = str(os.getenv("WORKFLOW_REQUIRE_BACKUP", "0")).strip().lower() in {"1", "true", "yes", "on"}
        backup_age = _backup_age_seconds()
        backup_fresh = backup_age is not None and backup_age <= int(os.getenv("WORKFLOW_BACKUP_MAX_AGE_S", "129600"))
        ok = bool(jobs) and (not require_worker or workers["alive"] > 0) and (not require_backup or backup_fresh)
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "ok": ok,
                "require_worker": require_worker,
                "require_backup": require_backup,
                "backup_age_seconds": backup_age,
                "backup_fresh": backup_fresh,
                "jobs": jobs,
                "workers": workers,
            },
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        jobs = service.store.job_health()
        workers = service.store.worker_health()
        lines = [
            "# HELP shopkeeper_jobs_pending Pending durable jobs.",
            "# TYPE shopkeeper_jobs_pending gauge",
            f"shopkeeper_jobs_pending {jobs['pending']}",
            "# HELP shopkeeper_jobs_running Running durable jobs.",
            "# TYPE shopkeeper_jobs_running gauge",
            f"shopkeeper_jobs_running {jobs['running']}",
            "# HELP shopkeeper_jobs_dead Dead-letter durable jobs.",
            "# TYPE shopkeeper_jobs_dead gauge",
            f"shopkeeper_jobs_dead {jobs['dead']}",
            "# HELP shopkeeper_workers_alive Workers with a fresh heartbeat.",
            "# TYPE shopkeeper_workers_alive gauge",
            f"shopkeeper_workers_alive {workers['alive']}",
            "# HELP shopkeeper_backup_age_seconds Seconds since latest daily backup.",
            "# TYPE shopkeeper_backup_age_seconds gauge",
            f"shopkeeper_backup_age_seconds {_backup_age_seconds() if _backup_age_seconds() is not None else -1}",
        ]
        return "\n".join(lines) + "\n"

    from shopkeeper.adapters.alibaba_dropship import build_fulfillment

    # 一个 service 只能有一个代发适配器：review 与 orchestrator 共用同一个实例。
    fulfillment = getattr(service, "fulfillment", None)
    if fulfillment is None:
        fulfillment = build_fulfillment()
        service.fulfillment = fulfillment
    mount_review(
        app,
        store=service.store,
        provider=service.provider,
        platform=service.platform,
        fulfillment=fulfillment,
    )
    mount_admin(app, service)

    # AI 工作流编排器（/admin「AI 工作流」页与同套 API）
    from shopkeeper.orchestrator.engine import Orchestrator
    from shopkeeper.orchestrator.routes import mount_orchestrator

    orch = getattr(service, "orchestrator", None) or Orchestrator(
        service.store,
        platform=service.platform,
        fulfillment=fulfillment,
    )
    service.orchestrator = orch
    mount_orchestrator(app, orch)

    @app.get("/workflow", response_class=HTMLResponse)
    def workflow_page() -> str:
        return render_workflow_page()

    @app.get("/workflow/catalog", response_class=HTMLResponse)
    def workflow_catalog_page() -> HTMLResponse:
        """相邻入口：同页商品库区块。开发自测用；生产请用 /admin。"""
        html = render_workflow_page()
        # 打开后滚到商品库。
        html = html.replace("</script>", "location.hash='catalog';</script>", 1)
        return HTMLResponse(html)

    @app.get("/workflow/profit", response_class=HTMLResponse)
    def workflow_profit_page() -> HTMLResponse:
        """相邻入口：同页利润看板区块（只读）。"""
        html = render_workflow_page()
        html = html.replace("</script>", "location.hash='profit';</script>", 1)
        return HTMLResponse(html)

    @app.get("/api/workflow/status")
    def status() -> dict[str, Any]:
        return service.status()

    @app.get("/api/workflow/snapshot")
    def snapshot() -> dict[str, Any]:
        return service.snapshot()

    @app.get("/api/workflow/reports")
    def reports() -> dict[str, Any]:
        return {"reports": service.store.list_reports()}

    @app.post("/api/workflow/jobs/{name}")
    def run_job(name: str) -> dict[str, Any]:
        try:
            report = service.run_job(name)
        except DraftError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return report
