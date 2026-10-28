const OPERATIONS_ROUTES = { operations: "运营总览", candidates: "选品工作台", materials: "素材中心", campaigns: "投放计划", monitoring: "数据监控", records: "调用记录" };
const MODULE_NAMES = { research: "研究建议", verify: "事实核验", write_copy: "文案生成", quality_check: "素材质检", prepare_campaign: "投放计划", record_metrics: "数据复盘", orchestrator: "总控建议" };
const escapeHtml = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const e = escapeHtml;
const names = { researching: "调研中", shortlisted: "已入选", rejected: "不采用", draft: "草拟", manual_active: "平台人工启用", completed: "已结束", manual: "人工录入", csv: "平台导出 / CSV", mock: "模拟数据", text: "文案", image: "图片" };
function optionalNumber(v) {
  if (v === "" || v == null) return null;
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) throw Error("请输入非负数字，未知请留空");
  return n;
}
function displayMetric(v, percent = false) {
  return v == null ? "缺数据" : percent ? `${(Number(v) * 100).toFixed(2)}%` : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}
function safeContentUrl(v) {
  return typeof v === "string" && v.startsWith("/api/admin/operations/assets/") && !v.includes("\\") ? v : "";
}
const field = (n, t, v = "", type = "text", extra = "") => `<label class="ops-field">${e(t)}<input name="${n}" type="${type}" value="${e(v)}" ${extra}></label>`;
const area = (n, t, v = "") => `<label class="ops-field">${e(t)}<textarea name="${n}" rows="3">${e(v)}</textarea></label>`;
const select = (n, t, options, v = "") => `<label class="ops-field">${e(t)}<select name="${n}">${options.map(([k, s]) => `<option value="${e(k)}" ${String(k) === String(v) ? "selected" : ""}>${e(s)}</option>`).join("")}</select></label>`;
const opts = (a) => a.map((k) => [k, names[k] || k]);
const link = (r, t) => `<a class="btn secondary" data-nav="${r}" href="/admin/${r}">${t}</a>`;
const empty = (t) => `<div class="ops-empty">${t}</div>`;
const nums = ["impressions", "clicks", "spend", "revenue", "orders"];
const numLabels = { impressions: "展示次数", clicks: "点击次数", spend: "实际花费", revenue: "实际收入", orders: "订单数" };
class OperationsWorkbench {
  constructor(api, context) {
    this.api = api;
    this.context = context;
    this.token = 0;
    this.data = {};
    this.references = {};
    this.edit = null;
    this.note = "";
    this.error = "";
    // 一次性提示（操作结果）。与 this.error 分开：this.error 表示「读取失败」，
    // 会把整个内容区换成「重试加载」按钮；操作提示不该把面板藏掉。
    this.flash = "";
    this.flashError = false;
    this.candidateSuggestions = {};
    this.copyState = {};
    this.planState = {};
    this.reviewState = {};
    this.records = [];
    this.recordsTotal = 0;
    this.recordsOffset = 0;
    this.recordModule = "";
    this.recordDetail = {};
    this.recordDetailOpen = "";
  }
  clear() {
    this.token++;
    this.data = {};
    this.references = {};
    this.edit = null;
    this.note = "";
    this.error = "";
    this.flash = "";
    this.flashError = false;
    this.scope = "";
    this.offset = 0;
    this.filters = {};
    this.candidateSuggestions = {};
    this.copyState = {};
    this.planState = {};
    this.reviewState = {};
    this.records = [];
    this.recordsTotal = 0;
    this.recordsOffset = 0;
    this.recordModule = "";
    this.recordDetail = {};
    this.recordDetailOpen = "";
  }
  capture() {
    return { ...this.context() };
  }
  current(c) {
    const n = this.context();
    return n.shop === c.shop && n.generation === c.generation && n.route === c.route && n.user === c.user;
  }
  url(p, c) {
    if ((p.startsWith("dashboard") || p.startsWith("metrics?")) && this.filters) {
      const q = new URLSearchParams(Object.entries(this.filters).filter(([, v]) => v)).toString();
      if (q) p += `${p.includes("?") ? "&" : "?"}${q}`;
    }
    return `/api/admin/operations/${p}${p.includes("?") ? "&" : "?"}shop_id=${encodeURIComponent(c.shop)}`;
  }
  async all(p, c, token) {
    const primary = { candidates: "candidates", materials: "assets", campaigns: "campaigns", monitoring: "metrics" }[c.route];
    const offset = p === primary ? this.offset || 0 : 0;
    const r = await this.api(this.url(`${p}?limit=50&offset=${offset}`, c));
    if (!this.current(c) || token !== this.token) return [];
    if (p === primary) this.total = r.total;
    else this.references[p] = { offset: r.items.length, total: r.total };
    return r.items;
  }
  async loadMoreReferences(kind) {
    const c = this.capture(), token = this.token, ref = this.references[kind];
    if (!ref || ref.offset >= ref.total || ref.busy) return null;
    ref.busy = true;
    try {
      const r = await this.api(this.url(`${kind}?limit=50&offset=${ref.offset}`, c));
      if (!this.current(c) || token !== this.token) return null;
      const known = new Set((this.data[kind] || []).map((x) => x.id));
      const added = r.items.filter((x) => !known.has(x.id));
      this.data[kind] = [...this.data[kind] || [], ...added];
      ref.offset += r.items.length;
      ref.total = r.items.length ? r.total : ref.offset;
      return added;
    } finally {
      ref.busy = false;
    }
  }
  referenceControls(kind) {
    const ref = this.references[kind];
    if (!ref) return "";
    return `<button class="btn secondary" type="button" data-ops-more="${kind}" ${ref.offset >= ref.total ? "disabled" : ""}>加载更多${{ candidates: "商品", assets: "素材", campaigns: "计划", drafts: "草稿" }[kind]}（已加载 ${ref.offset} / ${ref.total}）</button>`;
  }
  async load() {
    const c = this.capture(), token = ++this.token, s = `${c.shop}:${c.generation}:${c.route}`;
    if (this.scope !== s) {
      this.data = {};
      this.references = {};
      this.edit = null;
      this.note = "";
      this.error = "";
      this.flash = "";
      this.flashError = false;
      this.scope = s;
      this.offset = 0;
      this.filters = {};
      this.candidateSuggestions = {};
      this.copyState = {};
      this.planState = {};
      this.reviewState = {};
      this.records = [];
      this.recordsTotal = 0;
      this.recordsOffset = 0;
      this.recordModule = "";
      this.recordDetail = {};
      this.recordDetailOpen = "";
    }
    if (c.route === "records") {
      await this.loadRecords(c, token);
      return;
    }
    const keys = c.route === "operations" ? ["dashboard"] : c.route === "candidates" ? ["candidates"] : c.route === "materials" ? ["assets", "candidates", "drafts"] : c.route === "campaigns" ? ["campaigns", "candidates", "assets"] : ["metrics", "campaigns"];
    try {
      const rows = await Promise.all(keys.map((k) => k === "dashboard" ? this.api(this.url(k, c)) : this.all(k, c, token)));
      if (!this.current(c) || token !== this.token) return false;
      this.data = Object.fromEntries(keys.map((k, i) => [k, rows[i]]));
      this.error = "";
      return true;
    } catch (err) {
      if (this.current(c) && token === this.token) {
        this.data = {};
        this.error = err.message;
      }
      return false;
    }
  }
  /** 调用记录面板：只读分页读取。读取与刷新都不触发生成或采纳。 */
  async loadRecords(c, token) {
    const query = new URLSearchParams({ limit: "50", offset: String(this.recordsOffset || 0) });
    if (this.recordModule) query.set("module", this.recordModule);
    try {
      const r = await this.api(this.url(`work-model-records?${query.toString()}`, c));
      if (!this.current(c) || token !== this.token) return false;
      this.records = r.items || [];
      this.recordsTotal = r.total || 0;
      this.error = "";
      return true;
    } catch (err) {
      if (this.current(c) && token === this.token) {
        this.records = [];
        this.recordsTotal = 0;
        this.error = err.message;
      }
      return false;
    }
  }
  /** 打开一条调用记录的详情（按需读取结果正文）。 */
  async openRecord(id) {
    const c = this.capture(), token = this.token;
    this.recordDetailOpen = id;
    try {
      const r = await this.api(this.url(`work-model-records/${encodeURIComponent(id)}`, c));
      if (!this.current(c) || token !== this.token) return false;
      this.recordDetail = { ...(this.recordDetail || {}), [id]: r };
      return true;
    } catch (err) {
      if (this.current(c) && token === this.token) {
        this.recordDetail = { ...(this.recordDetail || {}), [id]: { error: err.message } };
      }
      return false;
    }
  }
  render() {
    const c = this.capture(), ro = c.user?.role === "viewer";
    const subtitle = c.route === "records"
      ? "研究 → 核验 → 文案 → 质检 → 计划 → 复盘：每一次模型调用都可回读"
      : "选品 → 素材 → 人工投放计划 → 每日数据复盘";
    // 操作提示（flash）与读取失败（error）分区渲染：error 会把内容区换成
    // 「重试加载」按钮，操作提示必须与它分开显示。
    const flashHtml = this.flash
      ? `<p class="${this.flashError ? "error" : "ops-success"}">${e(this.flash)}</p>`
      : "";
    return `<div class="ops-workbench"><div class="page-head"><div><h1>${OPERATIONS_ROUTES[c.route]}</h1><p>${e(subtitle)}</p></div><button class="btn secondary" data-ops-refresh>刷新</button></div><p class="ops-boundary">小团队辅助工作台 · 店铺 ${e(c.shop)} · 记录保存在本地。投放、支付和履约由平台完成；这里不会执行广告投放。</p><div id="ops-feedback" role="status" aria-live="polite">${this.note ? `<p class="ops-success">${e(this.note)}</p>` : ""}${flashHtml}${this.error ? `<p class="error">${e(this.error)}</p>` : ""}</div>${ro ? '<p class="muted">当前账号只读。</p>' : ""}${["operations", "monitoring"].includes(c.route) ? this.filterForm() : ""}${this.error ? '<button class="btn secondary" data-ops-refresh>重试加载</button>' : this.content(c.route, ro)}</div>`;
  }
  filterForm() {
    return `<form id="ops-filter" class="ops-filters">${field("start_date", "开始日期", this.filters?.start_date || "", "date")}${field("end_date", "结束日期", this.filters?.end_date || "", "date")}${select("source", "数据来源", [["", "全部来源"], ...opts(["manual", "csv", "mock"])], this.filters?.source || "")}${this.capture().route === "monitoring" ? select("campaign_id", "投放计划", [["", "全部计划"], ...associationOptions(this.data.campaigns || [], this.filters?.campaign_id)], this.filters?.campaign_id || "") : ""}<button class="btn secondary">查询</button><button class="btn secondary" type="button" data-ops-reset>全部日期</button></form>`;
  }
  content(r, ro) {
    if (r === "operations") return this.dashboard();
    if (r === "records") return this.recordsPanel();
    const k = { candidates: "candidates", materials: "assets", campaigns: "campaigns", monitoring: "metrics" }[r], items = this.data[k] || [];
    const extras = r === "candidates" ? link("scout", "AI 选品与 Listing 草稿") + link("workflows", "Scout / Stylist 工作流") : r === "materials" ? link("scout", "生成 AI 文案与图片") + link("runs", "查看 Agent 产物") + link("spotcheck", "人工抽检") : "";
    return `<div class="ops-links">${extras}</div>${extras ? '<p class="muted">沿用现有 AI 生成通道，mock 产物用于演示。核对商品事实后，将文案或图片保存到素材中心，填写原始来源。</p>' : ""}<div class="ops-layout"><section class="panel"><div class="panel-head"><h2>记录 · ${this.total ?? items.length}</h2></div><div class="panel-body ops-records">${items.length ? items.map((x) => this.card(k, x, ro)).join("") : empty("暂无记录。先建立候选商品，再整理素材、计划和每日数据。")}<div class="ops-actions"><button class="btn secondary" data-ops-prev ${(this.offset || 0) === 0 ? "disabled" : ""}>上一页</button><span>第 ${Math.floor((this.offset || 0) / 50) + 1} 页 · 每页 50 条</span><button class="btn secondary" data-ops-next ${(this.offset || 0) + items.length >= (this.total || 0) ? "disabled" : ""}>下一页</button></div></div></section>${ro ? "" : `<section class="panel"><div class="panel-head"><h2>${this.edit ? "编辑记录" : "新增记录"}</h2></div><div class="panel-body">${this.form(k)}${r === "materials" ? this.draftForm() : ""}${r === "monitoring" ? this.importForm() : ""}</div></section>`}</div>`;
  }
  dashboard() {
    const d = this.data.dashboard || {};
    return `<div class="ops-stats">${[["candidates", "候选商品"], ["assets", "素材"], ["campaigns", "投放计划"]].map(([k, t]) => `<div class="panel ops-stat"><span>${t}</span><strong>${e(d.counts?.[k] ?? "缺数据")}</strong></div>`).join("")}</div><div class="ops-links">${link("candidates", "1. 记录选品")}${link("materials", "2. 整理素材")}${link("campaigns", "3. 制定计划")}${link("monitoring", "4. 每日数据")}</div><section class="panel"><div class="panel-head"><h2>已记录表现 · 各币种独立汇总（含所录入的模拟数据）</h2></div><div class="panel-body">${d.currencies?.length ? `<div class="ops-table"><table><thead><tr><th>币种 / 来源</th>${["展示", "点击", "花费", "收入", "订单", "CTR", "CPC", "ROAS"].map((t) => `<th>${t}</th>`).join("")}</tr></thead><tbody>${d.currencies.map((x) => `<tr><td>${e(x.currency)}<br><small>${e(Array.isArray(x.sources) ? x.sources.map((k) => names[k] || k).join(" / ") : JSON.stringify(x.sources || {}))}</small></td>${[...nums, "ctr", "cpc", "roas"].map((k) => `<td>${displayMetric(x[k], k === "ctr")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : empty("暂无数据。填写实际报表后显示指标，未知数值不按零计算。")}<p class="muted">金额为标注币种的主单位，不换汇。ROAS 是记录收入 ÷ 花费，不等于利润；计划状态不证明平台已投放。</p></div></section>`;
  }
  /** 调用记录面板：分页列出调用元数据，详情按需读取；纯只读，不提供任何生成或采纳控件。 */
  recordsPanel() {
    const moduleOptions = [["", "全部模块"], ...Object.entries(MODULE_NAMES)];
    const rows = this.records || [];
    const total = this.recordsTotal || 0;
    const offset = this.recordsOffset || 0;
    const shown = rows.length;
    const body = rows.length
      ? rows.map((r) => this.recordCard(r)).join("")
      : empty("暂无调用记录。发起研究、核验、文案、质检、计划或复盘后，这里会留下可回读的记录。");
    return `<div class="ops-links">${link("candidates", "回到选品工作台")}</div><form id="ops-record-filter" class="ops-filters">${select("module", "工作模块", moduleOptions, this.recordModule || "")}<button class="btn secondary">筛选</button></form><div class="ops-layout"><section class="panel"><div class="panel-head"><h2>调用记录 · ${total}</h2></div><div class="panel-body ops-records">${body}<div class="ops-actions"><button class="btn secondary" data-ops-record-prev ${offset === 0 ? "disabled" : ""}>上一页</button><span>第 ${Math.floor(offset / 50) + 1} 页 · 每页 50 条 · 本页 ${shown} 条</span><button class="btn secondary" data-ops-record-next ${offset + shown >= total ? "disabled" : ""}>下一页</button></div></div></section></div>`;
  }
  /** 一条调用记录的元数据卡片；失败原因与人工改动可见。 */
  recordCard(r) {
    const run = r.run || null;
    const failed = Boolean(r.failure_reason) || run?.status === "failed";
    let body = `<p><span class="tag">${e(MODULE_NAMES[r.module] || r.module)}</span> · 通道 ${e(r.model_channel || "未知")} · 提示词 ${e(r.prompt_version || "未知")}</p>`;
    body += `<p class="muted">时间 ${e(r.created_at || "")}${run?.id ? ` · 运行 ${e(run.id)}` : ""}${run?.input_version_digest ? ` · 输入摘要 ${e(String(run.input_version_digest).slice(0, 12))}` : ""}</p>`;
    if (r.input_object_ids?.length) body += `<p class="muted">输入对象：${r.input_object_ids.map((x) => e(x)).join("、")}</p>`;
    if (r.output_object_ids?.length) body += `<p class="muted">关联对象：${r.output_object_ids.map((x) => e(x)).join("、")}</p>`;
    if (failed) {
      body += `<p class="error">失败：${e(run?.failure_code || "")}${r.failure_reason ? ` · ${e(r.failure_reason)}` : ""}</p>`;
    } else {
      body += `<p class="muted">状态：${e(run?.status || "已留痕")}</p>`;
    }
    if (r.human_review || r.human_changes?.length) {
      body += `<p class="muted">人工处理：${r.human_changes?.length ? r.human_changes.map((x) => e(x)).join("； ") : "已复核"}</p>`;
    }
    body += `<div class="ops-actions"><button class="btn secondary" type="button" data-ops-record-open="${e(r.id)}">${this.recordDetailOpen === r.id && this.recordDetail?.[r.id] ? "收起详情" : "查看详情"}</button></div>`;
    if (this.recordDetailOpen === r.id) body += this.recordDetailBody(r);
    return `<article class="ops-record" data-record="${e(r.id)}"><h3>${e(MODULE_NAMES[r.module] || r.module)} · ${e(r.id)}</h3>${body}</article>`;
  }
  /** 详情正文：模型返回内容只作文本渲染（逐字转义），不解析为 HTML。 */
  recordDetailBody(r) {
    const detail = this.recordDetail?.[r.id];
    if (!detail) return `<div class="ops-suggestion-box" data-record-detail="${e(r.id)}"><p class="muted">正在读取详情…</p></div>`;
    if (detail.error) {
      return `<div class="ops-suggestion-box" data-record-detail="${e(r.id)}"><p class="error">读取失败：${e(detail.error)}</p></div>`;
    }
    const run = detail.run || null;
    let out = `<div class="ops-suggestion-box" data-record-detail="${e(r.id)}">`;
    out += `<h4>调用详情</h4>`;
    out += `<p class="muted">模块 ${e(MODULE_NAMES[detail.call?.module] || detail.call?.module || "")} · 通道 ${e(detail.call?.model_channel || "")} · 来源 ${e(detail.call?.source || "")}</p>`;
    if (detail.call?.human_changes?.length) {
      out += `<p><strong>人工改动：</strong>${detail.call.human_changes.map((x) => e(x)).join("； ")}</p>`;
    }
    if (!run) {
      out += `<p class="muted">该调用没有关联的运行记录（可能来自旧的调用入口）。</p></div>`;
      return out;
    }
    out += `<p class="muted">运行 ${e(run.id)} · 状态 ${e(run.status || "")}${run.failure_code ? ` · 失败码 ${e(run.failure_code)}` : ""}</p>`;
    if (run.failure_reason) out += `<p class="error">失败原因：${e(run.failure_reason)}</p>`;
    if (run.output_object_ids?.length) {
      out += `<p class="muted">关联对象：${run.output_object_ids.map((x) => e(x)).join("、")}</p>`;
    }
    if (run.output) {
      out += `<h4>结构化结果</h4><pre class="ops-copy">${e(JSON.stringify(run.output, null, 2))}</pre>`;
    }
    if (run.raw_output) {
      out += `<details class="ops-import"><summary>原始输出</summary><pre class="ops-copy">${e(run.raw_output)}</pre></details>`;
    }
    if (run.context && Object.keys(run.context).length) {
      out += `<details class="ops-import"><summary>服务端关联上下文</summary><pre class="ops-copy">${e(JSON.stringify(run.context, null, 2))}</pre></details>`;
    }
    out += `</div>`;
    return out;
  }
  card(k, x, ro) {
    const title = x.name || this.data.campaigns?.find((a) => a.id === x.campaign_id)?.name || x.campaign_id;
    let body = "";
    if (k === "candidates") {
      let sug = "";
      if (this.candidateSuggestions?.[x.id]) {
        const s = this.candidateSuggestions[x.id];
        sug = `<div class="ops-suggestion-box" data-suggestion-cand="${e(x.id)}">`;
        if (s.module === "research") {
          sug += `<h4>AI 研究建议（模型：${e(s.model_channel || "mock")}）</h4>`;
          if (s.output?.recommendation) sug += `<p><strong>建议动作：</strong>${e(s.output.recommendation)}</p>`;
          if (s.output?.facts?.length) sug += `<p><strong>提取事实：</strong>${e(s.output.facts.join("； "))}</p>`;
          if (s.output?.hypotheses?.length) sug += `<p><strong>分析假设：</strong>${e(s.output.hypotheses.join("； "))}</p>`;
          if (s.output?.missing_info?.length) sug += `<p class="muted"><strong>缺失信息：</strong>${e(s.output.missing_info.join("； "))}</p>`;
          if (!ro) {
            sug += `<div class="ops-actions"><button type="button" class="btn" data-ops-adopt-run="${e(s.id)}" data-cand-id="${e(x.id)}" data-version="${e(s.input_version_digest)}">采纳研究建议</button></div>`;
          }
        } else if (s.module === "verify") {
          sug += `<h4>AI 事实核验（结论：${s.output?.decision === "pass" ? "通过" : "不通过"}）</h4>`;
          if (s.output?.claims?.length) {
            sug += `<ul class="ops-claims-list">${s.output.claims.map(c => `<li><strong>${e(c.claim)}</strong> [${e(c.status)}] ${c.evidence ? `证据: ${e(c.evidence)}` : '<span class="warn">缺证据</span>'}</li>`).join("")}</ul>`;
          }
          if (s.output?.missing_evidence?.length) {
            sug += `<p class="muted"><strong>缺失证据：</strong>${e(s.output.missing_evidence.join("； "))}</p>`;
          }
          if (!ro && s.output?.decision === "pass") {
            sug += `<div class="ops-actions"><button type="button" class="btn" data-ops-adopt-run="${e(s.id)}" data-cand-id="${e(x.id)}" data-version="${e(s.input_version_digest)}">采纳已核验事实</button></div>`;
          }
        }
        sug += `</div>`;
      }
      sug += this.copyPanel(x, ro);
      sug += this.planPanel(x, ro);
      body = `<span class="tag">${e(names[x.status])}</span><p>${e(x.facts)}</p><p>来源 ${e(x.source_url || "未填写")} · 成本 ${displayMetric(x.cost_amount)} ${e(x.currency)}</p>${sug}`;
    }
    if (k === "assets") body = (x.kind === "image" && safeContentUrl(x.content_url) ? `<img class="ops-preview" src="${e(safeContentUrl(x.content_url))}" alt="${e(x.name)}" loading="lazy">` : `<pre class="ops-copy">${e(x.text)}</pre>`) + `<p>语言 ${e(x.language || "未填写")} · 来源 ${e([x.source, x.draft_source].filter(Boolean).join(" · ") || "未填写")}</p><p>状态：${e(x.status === "usable" ? "可用素材（已通过质检）" : "草稿（未通过质检）")}</p>`;
    if (k === "campaigns") body = `<p>${e(x.platform)} · ${e(names[x.status])}</p><p>${e(x.notes)}</p><p>人工执行记录：${e(x.execution_notes || "未填写")}</p>` + this.reviewPanel(x, ro);
    if (k === "metrics") body = `<p>${e(x.day)} · ${e(x.currency)} · ${e(names[x.source] || x.source || "人工录入")}</p><p>${nums.map((n) => `${numLabels[n]} ${displayMetric(x[n])}`).join(" · ")}</p>`;
    return `<article class="ops-record"><h3>${e(title)}</h3>${body}${ro ? "" : `<div class="ops-actions">${k === "candidates" ? `<button class="btn" type="button" data-ops-quick-list="${e(x.id)}" data-name="${e(x.name)}">生成 Listing 草稿</button><button class="btn secondary" type="button" data-ops-research="${e(x.id)}" data-name="${e(x.name)}">研究建议</button><button class="btn secondary" type="button" data-ops-verify="${e(x.id)}" data-name="${e(x.name)}">核验事实</button><button class="btn secondary" type="button" data-ops-write-copy="${e(x.id)}" data-name="${e(x.name)}">生成泰英文案</button><button class="btn secondary" type="button" data-ops-plan="${e(x.id)}" data-name="${e(x.name)}">生成投放计划建议</button>` : ""}${k === "campaigns" ? `<button class="btn secondary" type="button" data-ops-review="${e(x.id)}" data-name="${e(x.name)}">复盘这份数据</button>` : ""}${k !== "assets" ? `<button class="btn secondary" data-ops-edit="${e(x.id)}" data-kind="${k}">编辑</button>` : '<span class="muted">素材不可覆盖，修改请新增版本</span>'}<button class="btn secondary" data-ops-delete="${e(x.id)}" data-kind="${k}">删除</button></div>`}</article>`;
  }

  /** 投放计划助手面板：只展示服务端返回的建议，采纳与否由人决定。 */
  planPanel(x, ro) {
    const state = this.planState?.[x.id];
    // 采纳成功后前端会删掉 state.run。此时 state 仍是对象但 run 为空——
    // 直接读 run.output 会抛 TypeError，整页卡在「正在加载…」。
    if (!state || !state.run) return "";
    const run = state.run;
    const out = run.output || {};
    let panel = `<div class="ops-suggestion-box" data-plan-panel="${e(x.id)}">`;
    panel += `<h4>投放计划建议</h4>`;
    if (run.status !== "succeeded") {
      panel += `<p class="error">计划建议未通过：${e(run.failure_code || "未知原因")}</p>`;
      panel += `</div>`;
      return panel;
    }
    panel += `<p><strong>计划名称：</strong>${e(out.name || "未命名")}</p>`;
    if (out.objective) panel += `<p><strong>目标：</strong>${e(out.objective)}</p>`;
    if (out.audience_hypotheses?.length) {
      panel += `<p><strong>受众假设：</strong>${e(out.audience_hypotheses.join("； "))}</p>`;
    }
    if (out.budget_note) panel += `<p><strong>预算说明：</strong>${e(out.budget_note)}</p>`;
    if (out.execution_checklist?.length) {
      panel += `<ul class="ops-claims-list">${out.execution_checklist.map((i) => `<li>${e(i)}</li>`).join("")}</ul>`;
    }
    if (out.tracking_fields?.length) {
      panel += `<p class="muted"><strong>跟踪字段：</strong>${e(out.tracking_fields.join("、"))}</p>`;
    }
    if (out.missing?.length) {
      panel += `<p class="muted"><strong>待补信息：</strong>${e(out.missing.join("； "))}</p>`;
    }
    panel += `<p class="muted">这是本地人工执行计划建议，不是平台回执；采纳后状态为草稿。</p>`;
    if (!ro) {
      panel += `<div class="ops-actions"><button type="button" class="btn" data-ops-adopt-campaign="${e(run.id)}" data-cand-id="${e(x.id)}">采纳为本地计划（草稿）</button></div>`;
    }
    panel += `</div>`;
    return panel;
  }

  /** 指标复盘面板：展示服务端读到的原始指标与模型解读，失败原因可见。 */
  reviewPanel(x, ro) {
    const state = this.reviewState?.[x.id];
    // 同 planPanel：状态对象存在但 run 已被清空时不得解引用。
    if (!state || !state.run) return "";
    const run = state.run;
    const out = run.output || {};
    let panel = `<div class="ops-suggestion-box" data-review-panel="${e(x.id)}">`;
    panel += `<h4>数据复盘</h4>`;
    if (run.status !== "succeeded") {
      panel += `<p class="error">复盘未通过：${e(run.failure_code || "未知原因")}</p>`;
      panel += `</div>`;
      return panel;
    }
    const scope = out.scope || {};
    if (scope.currency) panel += `<p class="muted">范围：${e(scope.campaign_id || x.id)} · ${e(scope.currency)}${scope.from ? ` · ${e(scope.from)} 起` : ""}${scope.to ? ` · 至 ${e(scope.to)}` : ""}</p>`;
    const known = out.known_metrics || {};
    panel += `<ul class="ops-claims-list">${nums.map((n) => `<li>${e(numLabels[n])} ${displayMetric(known[n])}</li>`).join("")}${["ctr", "cpc", "roas"].map((n) => `<li>${n.toUpperCase()} ${displayMetric(known[n], n === "ctr")}</li>`).join("")}</ul>`;
    if (out.observations?.length) panel += `<p><strong>观察：</strong>${e(out.observations.join("； "))}</p>`;
    if (out.data_gaps?.length) panel += `<p class="muted"><strong>数据缺口：</strong>${e(out.data_gaps.join("； "))}</p>`;
    if (out.next_actions?.length) panel += `<p class="muted"><strong>建议下一步：</strong>${e(out.next_actions.join("； "))}</p>`;
    panel += `<p class="muted">比率由服务端按原始指标重算；原始指标不被复盘结果改写。</p>`;
    panel += `</div>`;
    return panel;
  }

  /** 文案 → 质检 → 采纳为素材 的面板；只展示服务端返回的结果，不本地判定通过。 */
  copyPanel(x, ro) {
    const state = this.copyState?.[x.id];
    // 采纳素材后前端会删掉 state.quality；state 仍在但 quality 为空。
    if (!state) return "";
    const copy = state.copy;
    const quality = state.quality;
    // 运行记录的对外正文在 ``output`` 里（服务端契约：结构化结果与元数据分字段），
    // 不在顶层。直接从顶层读 title_th/body_th 会渲染出空正文——这是真实浏览器
    // 验收发现的存量缺陷：页面只显示四个空行，看不到任何文案。
    const copyOutput = copy?.output || {};
    let panel = `<div class="ops-suggestion-box" data-copy-panel="${e(x.id)}">`;
    panel += `<h4>泰英文案</h4>`;
    if (copy) {
      panel += `<pre class="ops-copy">${e(copyOutput.title_th || "")}\n${e(copyOutput.body_th || "")}\n${e(copyOutput.title_en || "")}\n${e(copyOutput.body_en || "")}</pre>`;
      panel += `<p class="muted">模型：${e(copy.model_channel || "mock")}</p>`;
    } else {
      panel += `<p class="muted">尚未生成文案。生成需先有当前版本的有效核验作为事实背书。</p>`;
    }
    if (quality) {
      const checks = quality.output?.checks || {};
      const labels = {
        facts_supported: "事实有据",
        language_ok: "语言可用",
        sensitive_claims_checked: "敏感声明已查",
        copyright_source_present: "版权来源齐备",
      };
      panel += `<h4>素材质检（结论：${quality.output?.decision === "pass" ? "通过" : quality.status === "failed" ? "未通过" : "待人工复核"}）</h4>`;
      panel += `<ul class="ops-claims-list">${Object.entries(labels).map(([key, label]) => `<li>${e(label)}：${checks[key] === true ? "是" : "否"}</li>`).join("")}</ul>`;
      if (quality.output?.issues?.length) {
        panel += `<p class="muted"><strong>问题：</strong>${e(quality.output.issues.join("； "))}</p>`;
      }
      if (quality.output?.required_edits?.length) {
        panel += `<p class="muted"><strong>必改项：</strong>${e(quality.output.required_edits.join("； "))}</p>`;
      }
      if (quality.status === "failed") {
        panel += `<p class="error">质检未通过：${e(quality.failure_code || "")}</p>`;
      }
    }
    if (!ro && quality?.status === "succeeded" && quality.output?.decision === "pass") {
      panel += `<div class="ops-actions"><button type="button" class="btn" data-ops-adopt-asset="${e(quality.id)}" data-cand-id="${e(x.id)}">采纳为可用素材</button></div>`;
    }
    if (!ro && copy?.status === "succeeded" && !quality) {
      panel += `<div class="ops-actions"><button type="button" class="btn secondary" data-ops-quality-check="${e(copy.id)}" data-cand-id="${e(x.id)}" data-name="${e(x.name)}">质检这份文案</button></div>`;
    }
    panel += `</div>`;
    return panel;
  }
  form(k) {
    const x = this.edit || {}, cs = [["", "不关联"], ...associationOptions(this.data.candidates || [], x.candidate_id)];
    let f = "";
    if (k === "candidates") f = field("name", "商品名称", x.name, "text", 'required maxlength="200"') + `<div class="ops-url-row">${field("source_url", "货源链接", x.source_url, "url")}<button type="button" class="btn secondary" data-ops-extract-url>自动提取</button></div>` + area("facts", "已确认商品事实", x.facts) + area("notes", "调研备注", x.notes) + select("status", "选品判断", opts(["researching", "shortlisted", "rejected"]), x.status || "researching") + field("cost_amount", "成本金额（未知留空）", x.cost_amount, "number", 'min="0" step="0.01"') + field("currency", "成本币种", x.currency || "THB", "text", 'required pattern="[A-Z]{3}" maxlength="3"');
    if (k === "assets") f = field("name", "素材名称", x.name, "text", 'required maxlength="200"') + select("candidate_id", "关联商品", cs, x.candidate_id) + select("kind", "素材类型", opts(["text", "image"]), x.kind || "text") + field("language", "语言（th / en / vi 等）", x.language) + field("source", "来源（人工 / mock / AI 通道 / 原始链接）", x.source, "text", "required") + area("text", "文案正文", x.text) + '<label class="ops-field">上传图片（PNG / JPEG / WebP，最大 5 MiB）<input name="file" type="file" accept="image/png,image/jpeg,image/webp"></label><div id="ops-file-preview"></div><p class="muted">请确认素材使用权。已保存素材不可覆盖，修改请新增版本。</p>';
    if (k === "campaigns") f = field("name", "计划名称", x.name, "text", 'required maxlength="200"') + select("candidate_id", "关联商品", cs, x.candidate_id) + select("platform", "计划平台", [["tiktok", "TikTok"], ["shopee", "Shopee"], ["other", "其他"]], x.platform || "tiktok") + `<fieldset class="ops-assets"><legend>选用素材</legend>${(this.data.assets || []).map((a) => `<label><input type="checkbox" name="asset_ids" value="${e(a.id)}" ${(x.asset_ids || []).includes(a.id) ? "checked" : ""}>${e(a.name)} · ${e(a.language)} · ${e(a.source)}</label>`).join("") || "请先添加素材"}</fieldset>` + area("notes", "目标 / 预算说明（仅计划）", x.notes) + select("status", "人工计划状态", opts(["draft", "manual_active", "completed"]), x.status || "draft") + area("execution_notes", "平台人工操作记录 / 计划编号", x.execution_notes) + '<p class="muted">人工标记启用不会开通账户、扣费或向平台发送投放请求。</p>';
    if (k === "metrics") f = select("campaign_id", "关联投放计划", associationOptions(this.data.campaigns || [], x.campaign_id), x.campaign_id) + field("day", "平台报表日期", x.day || (/* @__PURE__ */ new Date()).toLocaleDateString("en-CA"), "date", "required") + field("currency", "金额币种", x.currency || "THB", "text", 'required pattern="[A-Z]{3}" maxlength="3"') + select("source", "数据来源", opts(["manual", "csv", "mock"]), x.source || "manual") + nums.map((n) => field(n, numLabels[n] + (["spend", "revenue"].includes(n) ? "（主单位）" : ""), x[n], "number", `min="0" step="${["spend", "revenue"].includes(n) ? "0.01" : "1"}"`)).join("") + '<p class="muted">未知留空，实际为零填 0。同计划、日期、币种再次保存会覆盖该日记录，修改日期会新增。</p>';
    return `<form id="ops-form" class="ops-form" data-kind="${k}">${f}<div class="ops-actions">${k === "assets" ? this.referenceControls("candidates") : k === "campaigns" ? this.referenceControls("candidates") + this.referenceControls("assets") : k === "metrics" ? this.referenceControls("campaigns") : ""}</div><div class="ops-actions"><button class="btn" type="submit">保存${k === "campaigns" ? "本地计划" : ""}</button>${this.edit ? '<button class="btn secondary" type="button" data-ops-cancel>取消编辑</button>' : ""}</div></form>`;
  }
  draftForm() {
    return `<details class="ops-import"><summary>从 AI 草稿导入文案</summary><p>保留草稿来源，不修改审批、不执行发布。图片请从产物下载并上传。</p><form id="ops-draft-import">${select("draft_id", "现有草稿", (this.data.drafts || []).map((x) => [x.id, `${x.title} · ${x.source || x.kind}`]))}${select("candidate_id", "关联商品", [["", "不关联"], ...(this.data.candidates || []).map((x) => [x.id, x.name])])}${select("language", "语言", [["en", "英文"], ["th", "泰文"]])}${this.referenceControls("drafts")}<button type="submit" class="btn secondary" ${(this.data.drafts || []).length ? "" : "disabled"}>导入为素材</button></form></details>`;
  }
  importForm() {
    return `<details class="ops-import"><summary>批量导入每日数据（JSON）</summary><p>粘贴行数组。逐行保存，重复键覆盖；失败时显示已保存行数。</p><form id="ops-import">${select("campaign_id", "计划", (this.data.campaigns || []).map((a) => [a.id, a.name]))}${area("rows", "数据数组", '[{"day":"2026-10-22","currency":"THB","impressions":100,"clicks":5,"spend":20,"revenue":60,"orders":1,"source":"manual"}]')}<button class="btn secondary">导入记录</button></form></details>`;
  }
  feedback(text, error = false) {
    // 提示既写进 DOM，也记进状态：写入 DOM 的即时反馈会在紧随其后的
    // redraw() 里被整块重绘抹掉（真实浏览器验收发现的存量缺陷），用户因此
    // 看不到「已采纳」「已保存」这类操作结果。
    this.flash = text;
    this.flashError = Boolean(error);
    const b = document.getElementById("ops-feedback");
    if (b) b.innerHTML = `<p class="${error ? "error" : "ops-success"}">${e(text)}</p>`;
  }
  bind(root, redraw) {
    root.querySelectorAll("[data-ops-more]").forEach((btn) => btn.onclick = async () => {
      const c = this.capture(), kind = btn.dataset.opsMore;
      btn.disabled = true;
      try {
        const added = await this.loadMoreReferences(kind);
        if (!added || !this.current(c) || !btn.isConnected) return;
        const fieldName = { candidates: "candidate_id", campaigns: "campaign_id", drafts: "draft_id" }[kind];
        if (fieldName) root.querySelectorAll(`select[name="${fieldName}"]`).forEach((sel) => {
          const known = new Set([...sel.options].map((o) => o.value));
          for (const x of added) {
            if (known.has(x.id)) continue;
            const option = document.createElement("option");
            option.value = x.id;
            option.textContent = x.name || x.title || x.id;
            sel.append(option);
          }
        });
        if (kind === "assets") {
          const container = root.querySelector(".ops-assets");
          if (container) {
            for (const x of added) {
              const row = document.createElement("label"), input = document.createElement("input");
              input.type = "checkbox";
              input.name = "asset_ids";
              input.value = x.id;
              input.checked = (this.edit?.asset_ids || []).includes(x.id);
              row.append(input, document.createTextNode(`${x.name} · ${x.language || ""} · ${x.source || ""}`));
              container.append(row);
            }
          }
        }
        const ref = this.references[kind];
        btn.textContent = `加载更多（已加载 ${ref.offset} / ${ref.total}）`;
        this.feedback("已加载更多选项，表单输入和已选内容保留。");
      } catch (err) {
        if (this.current(c)) this.feedback(err.message, true);
      } finally {
        if (this.current(c) && btn.isConnected) {
          const ref = this.references[kind];
          btn.disabled = !ref || ref.offset >= ref.total;
        }
      }
    });
    const filters = root.querySelector("#ops-filter");
    if (filters) filters.onsubmit = (ev) => {
      ev.preventDefault();
      this.filters = Object.fromEntries(new FormData(filters));
      this.offset = 0;
      redraw();
    };
    const reset = root.querySelector("[data-ops-reset]");
    if (reset) reset.onclick = () => {
      this.filters = {};
      this.offset = 0;
      redraw();
    };
    const prev = root.querySelector("[data-ops-prev]"), next = root.querySelector("[data-ops-next]");
    if (prev) prev.onclick = () => {
      this.offset = Math.max(0, (this.offset || 0) - 50);
      this.edit = null;
      redraw();
    };
    if (next) next.onclick = () => {
      this.offset = (this.offset || 0) + 50;
      this.edit = null;
      redraw();
    };
    root.querySelectorAll("[data-ops-refresh]").forEach((b) => b.onclick = () => redraw());
    const recordFilter = root.querySelector("#ops-record-filter");
    if (recordFilter) recordFilter.onsubmit = (ev) => {
      ev.preventDefault();
      this.recordModule = new FormData(recordFilter).get("module") || "";
      this.recordsOffset = 0;
      this.recordDetail = {};
      this.recordDetailOpen = "";
      redraw();
    };
    root.querySelectorAll("[data-ops-record-prev]").forEach((b) => b.onclick = () => {
      this.recordsOffset = Math.max(0, (this.recordsOffset || 0) - 50);
      this.recordDetail = {};
      this.recordDetailOpen = "";
      redraw();
    });
    root.querySelectorAll("[data-ops-record-next]").forEach((b) => b.onclick = () => {
      this.recordsOffset = (this.recordsOffset || 0) + 50;
      this.recordDetail = {};
      this.recordDetailOpen = "";
      redraw();
    });
    root.querySelectorAll("[data-ops-record-open]").forEach((b) => b.onclick = async () => {
      const id = b.dataset.opsRecordOpen;
      const c = this.capture();
      if (this.recordDetailOpen === id && this.recordDetail?.[id]) {
        this.recordDetailOpen = "";
        redraw(false);
        return;
      }
      b.disabled = true;
      try {
        await this.openRecord(id);
        if (!this.current(c)) return;
        redraw(false);
      } finally {
        if (this.current(c) && b.isConnected) b.disabled = false;
      }
    });
    root.querySelectorAll("[data-ops-edit]").forEach((b) => b.onclick = () => {
      this.edit = this.data[b.dataset.kind].find((x) => String(x.id) === b.dataset.opsEdit);
      redraw(false);
    });
    const cancel = root.querySelector("[data-ops-cancel]");
    if (cancel) cancel.onclick = () => {
      this.edit = null;
      redraw(false);
    };
    root.querySelectorAll("[data-ops-delete]").forEach((b) => b.onclick = async () => {
      if (!window.confirm("确定删除这条本地记录？被引用的记录无法删除。")) return;
      const c = this.capture();
      b.disabled = true;
      try {
        await this.api(this.url(`${b.dataset.kind}/${encodeURIComponent(b.dataset.opsDelete)}`, c), { method: "DELETE" });
        if (this.current(c)) {
          this.edit = null;
          this.note = "已删除本地记录。";
          await redraw();
        }
      } catch (err) {
        if (this.current(c)) this.feedback(err.message, true);
      } finally {
        if (this.current(c) && b.isConnected) b.disabled = false;
      }
    });
    const form = root.querySelector("#ops-form");
    if (form) {
      const file = form.querySelector("[name=file]");
      if (file) file.onchange = async () => {
        const c = this.capture(), selected = file.files[0];
        if (!selected) return;
        try {
          const data = await readImage(selected);
          if (!this.current(c) || !file.isConnected || file.files[0] !== selected) return;
          root.querySelector("#ops-file-preview").innerHTML = `<img class="ops-preview" alt="待保存图片预览" src="data:${e(selected.type)};base64,${data}">`;
          form.elements.kind.value = "image";
        } catch (err) {
          if (this.current(c)) this.feedback(err.message, true);
        }
      };
      form.onsubmit = async (ev) => {
        ev.preventDefault();
        const c = this.capture(), fd = new FormData(form), kind = form.dataset.kind, id = this.edit?.id, payload = Object.fromEntries(fd);
        delete payload.file;
        if ("candidate_id" in payload) payload.candidate_id ||= null;
        if (kind === "campaigns") payload.asset_ids = preserveAssets(this.edit?.asset_ids || [], this.data.assets || [], fd.getAll("asset_ids"));
        const btn = form.querySelector("[type=submit]");
        btn.disabled = true;
        this.feedback("正在保存…");
        try {
          for (const k of kind === "metrics" ? nums : kind === "candidates" ? ["cost_amount"] : []) payload[k] = optionalNumber(payload[k]);
          if (kind === "assets" && file.files[0]) {
            payload.mime_type = file.files[0].type;
            payload.data_base64 = await readImage(file.files[0]);
          }
          if (!this.current(c)) return;
          await this.api(this.url(`${kind}${id && kind !== "metrics" ? `/${encodeURIComponent(id)}` : ""}`, c), { method: id && kind !== "metrics" ? "PATCH" : "POST", body: JSON.stringify(payload) });
          if (!this.current(c)) return;
          this.edit = null;
          this.note = "已保存到当前店铺。未执行平台投放或付款。";
          await redraw();
        } catch (err) {
          if (this.current(c)) this.feedback(err.message, true);
        } finally {
          if (this.current(c) && btn.isConnected) btn.disabled = false;
        }
      };
    }
    const extractBtn = root.querySelector("[data-ops-extract-url]");
    if (extractBtn) extractBtn.onclick = async () => {
      const urlInput = root.querySelector('input[name="source_url"]');
      const url = (urlInput?.value || "").trim();
      if (!url) {
        this.feedback("请先填写货源链接（须以 http:// 或 https:// 开头）。", true);
        urlInput?.focus();
        return;
      }
      if (!url.startsWith("http://") && !url.startsWith("https://")) {
        this.feedback("货源链接须包含 http:// 或 https:// 协议头。", true);
        urlInput?.focus();
        return;
      }
      const c = this.capture();
      extractBtn.disabled = true;
      extractBtn.textContent = "提取中…";
      this.feedback("正在从链接解析商品信息…");
      try {
        const res = await this.api(this.url("candidates/extract-url", c), {
          method: "POST",
          body: JSON.stringify({ url })
        });
        if (!this.current(c)) return;
        const nameInput = root.querySelector('input[name="name"]');
        const factsArea = root.querySelector('textarea[name="facts"]');
        const notesArea = root.querySelector('textarea[name="notes"]');
        const costInput = root.querySelector('input[name="cost_amount"]');
        const currInput = root.querySelector('input[name="currency"]');
        if (nameInput && res.name) nameInput.value = res.name;
        if (factsArea && res.facts) factsArea.value = res.facts;
        if (notesArea && res.notes) notesArea.value = res.notes;
        if (costInput && res.cost_amount != null) costInput.value = res.cost_amount;
        if (currInput && res.currency) currInput.value = res.currency;
        if (res.is_mock) {
          this.feedback("已填入离线 Mock 演示数据，不是真实商品信息，请核对修改。", true);
        } else {
          this.feedback("已自动提取商品信息，请核对后保存。");
        }
      } catch (err) {
        if (this.current(c)) this.feedback(`提取失败：${err.message || "未能解析"}，请手动填写。`, true);
      } finally {
        if (extractBtn.isConnected) {
          extractBtn.disabled = false;
          extractBtn.textContent = "自动提取";
        }
      }
    };
    root.querySelectorAll("[data-ops-quick-list]").forEach((btn) => btn.onclick = async () => {
      const id = btn.dataset.opsQuickList;
      const name = btn.dataset.name || "商品";
      const c = this.capture();
      btn.disabled = true;
      btn.textContent = "草稿生成中…";
      this.feedback(`正在为【${name}】生成 Listing 草稿…`);
      try {
        const res = await this.api(this.url(`candidates/${id}/quick-list`, c), {
          method: "POST",
          body: "{}"
        });
        if (!this.current(c)) return;
        const source = res.degraded ? "降级模板" : res.source === "mock" ? "Mock 模板" : `模型 ${res.source || "未知"}`;
        const needsRevision = res.status === "needs_revision";
        this.feedback(`【${res.title}】草稿已生成（来源：${source}）。${needsRevision ? "质检未通过，请修改候选商品事实后重新生成。" : "请前往审批页查看内容后，由 owner 人工审批。"}`, needsRevision);
        btn.textContent = needsRevision ? "需修改" : "待审批";
      } catch (err) {
        if (this.current(c)) this.feedback(`生成失败：${err.message || "未能处理"}`, true);
        btn.disabled = false;
        btn.textContent = "生成 Listing 草稿";
      }
    });

    root.querySelectorAll("[data-ops-research]").forEach((btn) => btn.onclick = async () => {
      const id = btn.dataset.opsResearch;
      const name = btn.dataset.name || "商品";
      const c = this.capture();
      btn.disabled = true;
      btn.textContent = "研究建议中…";
      this.feedback(`正在为【${name}】生成 AI 研究建议…`);
      try {
        const res = await this.api(this.url("work-model-runs", c), {
          method: "POST",
          body: JSON.stringify({
            module: "research",
            object_id: id,
            idempotency_key: `research-${id}-${Date.now()}`
          })
        });
        if (!this.current(c)) return;
        if (!this.candidateSuggestions) this.candidateSuggestions = {};
        this.candidateSuggestions[id] = res;
        this.feedback(`【${name}】研究建议生成完成，请预览并决定是否采纳。`);
        redraw(false);
      } catch (err) {
        if (this.current(c)) this.feedback(`生成失败：${err.message || "未能处理"}`, true);
        btn.disabled = false;
        btn.textContent = "研究建议";
      }
    });

    root.querySelectorAll("[data-ops-verify]").forEach((btn) => btn.onclick = async () => {
      const id = btn.dataset.opsVerify;
      const name = btn.dataset.name || "商品";
      const c = this.capture();
      btn.disabled = true;
      btn.textContent = "事实核验中…";
      this.feedback(`正在核验【${name}】的商品事实…`);
      try {
        const res = await this.api(this.url("work-model-runs", c), {
          method: "POST",
          body: JSON.stringify({
            module: "verify",
            object_id: id,
            idempotency_key: `verify-${id}-${Date.now()}`
          })
        });
        if (!this.current(c)) return;
        if (!this.candidateSuggestions) this.candidateSuggestions = {};
        this.candidateSuggestions[id] = res;
        const dec = res.output?.decision === "pass" ? "通过" : "不通过";
        this.feedback(`【${name}】事实核验完成（${dec}），请核对声明与证据来源。`);
        redraw(false);
      } catch (err) {
        if (this.current(c)) this.feedback(`核验失败：${err.message || "未能处理"}`, true);
        btn.disabled = false;
        btn.textContent = "核验事实";
      }
    });

    root.querySelectorAll("[data-ops-adopt-run]").forEach((btn) => btn.onclick = async () => {
      const runId = btn.dataset.opsAdoptRun;
      const candId = btn.dataset.candId;
      const version = btn.dataset.version;
      const c = this.capture();
      btn.disabled = true;
      btn.textContent = "采纳中…";
      this.feedback("正在采纳分析结果并更新商品事实…");
      try {
        const res = await this.api(this.url(`candidates/${candId}/adopt-run`, c), {
          method: "POST",
          body: JSON.stringify({
            run_id: runId,
            expected_version_digest: version
          })
        });
        if (!this.current(c)) return;
        if (this.candidateSuggestions) delete this.candidateSuggestions[candId];
        if (this.data.candidates) {
          const idx = this.data.candidates.findIndex(x => x.id === candId);
          if (idx >= 0) this.data.candidates[idx] = res;
        }
        this.feedback("分析结果已成功采纳至商品候选事实。");
        redraw(false);
      } catch (err) {
        if (this.current(c)) this.feedback(`采纳失败：${err.message || "未能处理"}`, true);
        btn.disabled = false;
        btn.textContent = "采纳分析结果";
      }
    });

    root.querySelectorAll("[data-ops-write-copy]").forEach((btn) => btn.onclick = async () => {
      const id = btn.dataset.opsWriteCopy;
      const name = btn.dataset.name || "商品";
      const c = this.capture();
      btn.disabled = true;
      btn.textContent = "文案生成中…";
      this.feedback(`正在为【${name}】生成泰英文案（需先有当前版本的有效核验）…`);
      try {
        const res = await this.api(this.url("work-model-runs", c), {
          method: "POST",
          body: JSON.stringify({
            module: "write_copy",
            object_id: id,
            idempotency_key: `copy-${id}-${Date.now()}`
          })
        });
        if (!this.current(c)) return;
        if (!this.copyState) this.copyState = {};
        this.copyState[id] = { copy: res };
        if (res.status !== "succeeded") {
          this.feedback(`文案生成未通过：${res.failure_code || "未知原因"}`, true);
        } else {
          this.feedback(`【${name}】文案已生成，请预览后发起质检。`);
        }
        redraw(false);
      } catch (err) {
        if (this.current(c)) this.feedback(`文案生成失败：${err.message || "未能处理"}`, true);
        btn.disabled = false;
        btn.textContent = "生成泰英文案";
      }
    });

    root.querySelectorAll("[data-ops-quality-check]").forEach((btn) => btn.onclick = async () => {
      const copyId = btn.dataset.opsQualityCheck;
      const candId = btn.dataset.candId;
      const name = btn.dataset.name || "商品";
      const c = this.capture();
      btn.disabled = true;
      btn.textContent = "质检中…";
      this.feedback(`正在质检【${name}】的文案…`);
      try {
        const res = await this.api(this.url("work-model-runs", c), {
          method: "POST",
          body: JSON.stringify({
            module: "quality_check",
            object_id: candId,
            idempotency_key: `quality-${copyId}-${Date.now()}`,
            scope: { copy_run_id: copyId }
          })
        });
        if (!this.current(c)) return;
        if (!this.copyState) this.copyState = {};
        const state = this.copyState[candId] || {};
        this.copyState[candId] = { ...state, quality: res };
        if (res.status !== "succeeded") {
          this.feedback(`质检未通过：${res.failure_code || "未知原因"}`, true);
        } else if (res.output?.decision !== "pass") {
          this.feedback(`质检结论为 ${res.output?.decision || "未知"}，请按问题清单修改后重新质检。`, true);
        } else {
          this.feedback(`【${name}】质检通过，可由人工预览后采纳为可用素材。`);
        }
        redraw(false);
      } catch (err) {
        if (this.current(c)) this.feedback(`质检失败：${err.message || "未能处理"}`, true);
        btn.disabled = false;
        btn.textContent = "质检这份文案";
      }
    });

    root.querySelectorAll("[data-ops-adopt-asset]").forEach((btn) => btn.onclick = async () => {
      const qualityId = btn.dataset.opsAdoptAsset;
      const candId = btn.dataset.candId;
      const c = this.capture();
      btn.disabled = true;
      btn.textContent = "采纳中…";
      this.feedback("正在把质检通过的文案采纳为可用素材…");
      try {
        const res = await this.api(this.url(`candidates/${candId}/adopt-asset-run`, c), {
          method: "POST",
          body: JSON.stringify({ quality_run_id: qualityId })
        });
        if (!this.current(c)) return;
        this.feedback(res.replayed ? "该质检结果此前已采纳，已回放原素材。" : "文案已采纳为可用素材，素材中心可预览。");
        if (this.copyState?.[candId]) delete this.copyState[candId].quality;
        if (this.copyState?.[candId] && !this.copyState[candId].quality && !this.copyState[candId].copy) {
          delete this.copyState[candId];
        }
        await redraw();
      } catch (err) {
        if (this.current(c)) this.feedback(`采纳失败：${err.message || "未能处理"}`, true);
        btn.disabled = false;
        btn.textContent = "采纳为可用素材";
      }
    });

    root.querySelectorAll("[data-ops-plan]").forEach((btn) => btn.onclick = async () => {
      const id = btn.dataset.opsPlan;
      const name = btn.dataset.name || "商品";
      const c = this.capture();
      btn.disabled = true;
      btn.textContent = "计划建议中…";
      this.feedback(`正在为【${name}】生成本地投放计划建议…`);
      try {
        // 本次输入的素材：当前店铺里属于该候选的素材。
        // 选品页不预载素材列表，这里按需读取一页；服务端会再逐个校验归属。
        let assets = this.data.assets || [];
        if (!assets.length) {
          const list = await this.api(this.url("assets?limit=50&offset=0", c));
          if (!this.current(c)) return;
          assets = list.items || [];
        }
        const assetIds = assets.filter((a) => a.candidate_id === id).map((a) => a.id);
        const res = await this.api(this.url("work-model-runs", c), {
          method: "POST",
          body: JSON.stringify({
            module: "prepare_campaign",
            object_id: id,
            idempotency_key: `plan-${id}-${Date.now()}`,
            scope: { asset_ids: assetIds }
          })
        });
        if (!this.current(c)) return;
        if (!this.planState) this.planState = {};
        this.planState[id] = { run: res };
        if (res.status !== "succeeded") {
          this.feedback(`计划建议未通过：${res.failure_code || "未知原因"}`, true);
        } else {
          this.feedback(`【${name}】计划建议已生成，请预览后决定是否采纳。`);
        }
        redraw(false);
      } catch (err) {
        if (this.current(c)) this.feedback(`计划建议失败：${err.message || "未能处理"}`, true);
        btn.disabled = false;
        btn.textContent = "生成投放计划建议";
      }
    });

    root.querySelectorAll("[data-ops-adopt-campaign]").forEach((btn) => btn.onclick = async () => {
      const runId = btn.dataset.opsAdoptCampaign;
      const candId = btn.dataset.candId;
      const c = this.capture();
      btn.disabled = true;
      btn.textContent = "采纳中…";
      this.feedback("正在把计划建议采纳为本地草稿计划…");
      try {
        const res = await this.api(this.url(`candidates/${candId}/adopt-campaign-run`, c), {
          method: "POST",
          body: JSON.stringify({ run_id: runId })
        });
        if (!this.current(c)) return;
        this.feedback(res.replayed ? "该计划建议此前已采纳，已回放原计划。" : "已保存为本地草稿计划；执行说明需人工在平台操作后回填。");
        if (this.planState?.[candId]) delete this.planState[candId].run;
        if (this.planState?.[candId] && !this.planState[candId].run) delete this.planState[candId];
        await redraw();
      } catch (err) {
        if (this.current(c)) this.feedback(`采纳失败：${err.message || "未能处理"}`, true);
        btn.disabled = false;
        btn.textContent = "采纳为本地计划（草稿）";
      }
    });

    root.querySelectorAll("[data-ops-review]").forEach((btn) => btn.onclick = async () => {
      const campaignId = btn.dataset.opsReview;
      const name = btn.dataset.name || "计划";
      const c = this.capture();
      btn.disabled = true;
      btn.textContent = "复盘中…";
      this.feedback(`正在按数据库原始指标复盘【${name}】…`);
      try {
        const res = await this.api(this.url("work-model-runs", c), {
          method: "POST",
          body: JSON.stringify({
            module: "record_metrics",
            object_id: campaignId,
            idempotency_key: `review-${campaignId}-${Date.now()}`,
            scope: {
              from: this.filters?.start_date || undefined,
              to: this.filters?.end_date || undefined,
              source: this.filters?.source || undefined
            }
          })
        });
        if (!this.current(c)) return;
        if (!this.reviewState) this.reviewState = {};
        this.reviewState[campaignId] = { run: res };
        if (res.status !== "succeeded") {
          this.feedback(`复盘未通过：${res.failure_code || "未知原因"}`, true);
        } else {
          this.feedback(`【${name}】复盘完成，比率由服务端按原始指标重算。`);
        }
        redraw(false);
      } catch (err) {
        if (this.current(c)) this.feedback(`复盘失败：${err.message || "未能处理"}`, true);
        btn.disabled = false;
        btn.textContent = "复盘这份数据";
      }
    });

    const draftForm = root.querySelector("#ops-draft-import");
    if (draftForm) draftForm.onsubmit = async (ev) => {
      ev.preventDefault();
      const c = this.capture(), payload = Object.fromEntries(new FormData(draftForm));
      payload.candidate_id ||= null;
      const btn = draftForm.querySelector("[type=submit]");
      btn.disabled = true;
      try {
        await this.api(this.url("assets/from-draft", c), { method: "POST", body: JSON.stringify(payload) });
        if (this.current(c)) {
          this.note = "AI 草稿已导入素材，保留原始来源。";
          await redraw();
        }
      } catch (err) {
        if (this.current(c)) this.feedback(err.message, true);
      } finally {
        if (this.current(c) && btn.isConnected) btn.disabled = false;
      }
    };
    const importer = root.querySelector("#ops-import");
    if (importer) importer.onsubmit = async (ev) => {
      ev.preventDefault();
      const c = this.capture(), fd = new FormData(importer), btn = importer.querySelector("button");
      btn.disabled = true;
      let saved = 0;
      try {
        const rows = JSON.parse(fd.get("rows"));
        if (!Array.isArray(rows) || !rows.length || rows.length > 100) throw Error("请提供 1–100 行 JSON 数组");
        for (const row of rows) {
          if (!this.current(c)) return;
          await this.api(this.url("metrics", c), { method: "POST", body: JSON.stringify({ ...row, campaign_id: fd.get("campaign_id") }) });
          saved++;
        }
        if (this.current(c)) {
          this.note = `已导入 ${saved} 行。`;
          await redraw();
        }
      } catch (err) {
        if (this.current(c)) this.feedback(`已保存 ${saved} 行，第 ${saved + 1} 行失败：${err.message}。修正后可重新导入。`, true);
      } finally {
        if (this.current(c) && btn.isConnected) btn.disabled = false;
      }
    };
  }
}
async function readImage(file) {
  if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) throw Error("仅接受 PNG、JPEG、WebP 图片");
  if (file.size > 5 * 1024 * 1024) throw Error("图片不能超过 5 MiB");
  const b = new Uint8Array(await file.arrayBuffer());
  let s = "";
  for (let i = 0; i < b.length; i += 8192) s += String.fromCharCode(...b.subarray(i, i + 8192));
  return btoa(s);
}
function associationOptions(items, id) {
  const out = items.map((x) => [x.id, x.name]);
  if (id && !items.some((x) => x.id === id)) out.unshift([id, `当前关联（列表外） ${id}`]);
  return out;
}
function preserveAssets(previous, visible, selected) {
  const ids = new Set(visible.map((x) => x.id));
  return [.../* @__PURE__ */ new Set([...previous.filter((id) => !ids.has(id)), ...selected])];
}
export {
  OPERATIONS_ROUTES,
  OperationsWorkbench,
  associationOptions,
  displayMetric,
  escapeHtml,
  optionalNumber,
  preserveAssets,
  readImage,
  safeContentUrl
};
