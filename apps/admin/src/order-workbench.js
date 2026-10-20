/** 订单履约面板：服务器草稿与审批是状态来源，页面只协调下一步。 */
const esc = (value) => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");

export function orderPhase(data) {
  const ds = data?.dropship;
  const tr = data?.tracking;
  if (tr?.draft?.status === "executed") return "complete";
  if (!ds?.draft) return "prepare";
  if (ds.draft.status === "awaiting_manual_po") return "manual_po";
  if (ds.draft.status !== "executed") {
    if (ds.approval?.status === "executing") return "executing";
    if (ds.approval?.status === "pending") return "approve_dropship";
    return ds.approval?.status === "approved" ? "execute_dropship" : "prepare";
  }
  if (!tr?.draft) {
    const result = ds.draft.result || {};
    return result.carrier && result.tracking_no ? "prepare_tracking" : "tracking_details";
  }
  if (tr.approval?.status === "executing") return "executing";
  if (tr.approval?.status === "pending") return "approve_tracking";
  return tr.approval?.status === "approved" ? "push_tracking" : "tracking_details";
}

export class OrderWorkbench {
  constructor(request) {
    this.request = request;
    this.orderId = "";
    this.generation = 0;
    this.role = "viewer";
    this.busy = false;
    this.data = null;
    this.error = "";
    this.form = {};
    this.sheet = "";
    this.shopId = "default";
  }

  clear() {
    this.generation += 1;
    this.orderId = "";
    this.data = null;
    this.sheet = "";
    this.form = {};
    this.busy = false;
    this.error = "";
    this.paint();
  }

  attach(host, role) {
    this.host = host;
    this.role = role || "viewer";
    this.paint();
  }

  current(id, generation) {
    return this.orderId === id && this.generation === generation;
  }

  path(id, suffix) {
    const shop = encodeURIComponent(this.shopId || "default");
    return `/api/workflow/orders/${encodeURIComponent(id)}/${suffix}?shop_id=${shop}`;
  }

  async read(id, generation) {
    const optional = async (suffix) => {
      try { return await this.request(this.path(id, suffix)); }
      catch (err) { if (err.status === 404) return null; throw err; }
    };
    const [dropship, tracking] = await Promise.all([optional("dropship"), optional("tracking")]);
    if (!this.current(id, generation)) return false;
    this.data = { dropship, tracking };
    return true;
  }

  async open(id) {
    this.clear();
    this.orderId = id;
    const generation = this.generation;
    this.busy = true;
    this.error = "";
    this.paint();
    try { await this.read(id, generation); }
    catch (err) { if (this.current(id, generation)) this.error = err.message; }
    finally { if (this.current(id, generation)) { this.busy = false; this.paint(); } }
  }

  async post(id, suffix, body = {}) {
    return this.request(this.path(id, suffix), { method: "POST", body: JSON.stringify(body) });
  }

  async advance(id, generation) {
    for (let count = 0; count < 4; count += 1) {
      if (!await this.read(id, generation)) return;
      const phase = orderPhase(this.data);
      const next = { prepare: "dropship/draft", execute_dropship: "dropship/execute", prepare_tracking: "tracking/draft", push_tracking: "tracking/push" }[phase];
      if (!next) return;
      await this.post(id, next);
    }
    throw new Error("流程状态未推进，请刷新后核对执行结果。");
  }

  async act(action, values = {}) {
    if (this.busy || !this.orderId) return;
    if (action !== "refresh" && this.role === "viewer") return;
    if ((action === "approve" || action === "reject") && this.role !== "owner") return;
    const id = this.orderId;
    const generation = this.generation;
    this.busy = true;
    this.error = "";
    this.form = { ...this.form, ...values };
    this.paint();
    try {
      if (!await this.read(id, generation)) return;
      if (action === "approve" || action === "reject") {
        const phase = orderPhase(this.data);
        const approval = phase === "approve_dropship" ? this.data.dropship.approval : phase === "approve_tracking" ? this.data.tracking.approval : null;
        if (!approval || approval.id !== values.approvalId) throw new Error("审批内容或状态已变化，请重新核对后操作。");
        if (action === "reject" && !values.note?.trim()) throw new Error("请填写驳回原因。");
        await this.request(`/api/workflow/approvals/${encodeURIComponent(approval.id)}/${action}`, {
          method: "POST",
          body: JSON.stringify({
            note: values.note || "订单履约工作台批准并继续",
            expected_hash: approval.payload_hash || "",
          }),
        });
        if (action === "approve" && this.current(id, generation)) await this.advance(id, generation);
      } else if (action === "continue") {
        await this.advance(id, generation);
      } else if (action === "record_po") {
        await this.post(id, "dropship/record-po", { alibaba_order_id: values.alibaba_order_id });
        if (this.current(id, generation)) await this.advance(id, generation);
      } else if (action === "tracking") {
        await this.post(id, "tracking/draft", { carrier: values.carrier, tracking_no: values.tracking_no });
      } else if (action === "sheet") {
        const out = await this.request(this.path(id, "dropship/manual-sheet"));
        if (this.current(id, generation)) this.sheet = out.copy_text || "";
      }
      await this.read(id, generation);
    } catch (err) {
      if (this.current(id, generation)) {
        this.error = err.message || "处理失败，请核对服务器执行结果。";
        try { await this.read(id, generation); } catch { /* 保留原错误，刷新后重新读取。 */ }
      }
    } finally {
      if (this.current(id, generation)) { this.busy = false; this.paint(); }
    }
  }

  paint() {
    if (!this.host?.isConnected) return;
    this.host.innerHTML = this.html();
    this.host.querySelectorAll("[data-order-action]").forEach((button) => {
      button.onclick = () => this.act(button.dataset.orderAction, { approvalId: button.dataset.approvalId, note: this.host.querySelector("[name=order_note]")?.value || "" });
    });
    this.host.querySelector("#order-po-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      this.act("record_po", Object.fromEntries(new FormData(event.currentTarget)));
    });
    this.host.querySelector("#order-tracking-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      this.act("tracking", Object.fromEntries(new FormData(event.currentTarget)));
    });
  }

  html() {
    if (!this.orderId) return '<p class="muted">选择订单后可查看代发与物流处理进度。</p>';
    const phase = orderPhase(this.data);
    const ds = this.data?.dropship;
    const tr = this.data?.tracking;
    const approval = phase === "approve_dropship" ? ds.approval : phase === "approve_tracking" ? tr.approval : null;
    const titles = { prepare: "准备代发草稿", approve_dropship: "等待代发审批", execute_dropship: "代发已批准，可继续", manual_po: "等待人工回填采购单", prepare_tracking: "准备物流草稿", tracking_details: "等待填写物流信息", approve_tracking: "等待物流审批", push_tracking: "物流已批准，可继续", executing: "服务器正在执行，请刷新核对", complete: "物流信息处理完成" };
    const disabled = this.busy || this.role === "viewer" ? "disabled" : "";
    const button = (action, text, extra = "", kind = "secondary") => `<button type="button" class="btn ${kind}" data-order-action="${action}" ${disabled} ${extra}>${text}</button>`;
    const failureText = (node) => {
      const result = node?.draft?.result;
      if (result && (result.code || result.message)) return [result.code, result.message].filter(Boolean).join(" ");
      if (node?.approval?.execution_error) return `执行失败：${node.approval.execution_error}`;
      if (node?.approval?.status === "rejected") return `已驳回：${node.approval.decision_note || "未填写原因"}`;
      return "";
    };
    // 服务器已落库的失败原因必须在刷新后仍然可见，不能只依赖一次性的 this.error。
    const serverError = failureText(tr) || failureText(ds);
    const errorText = [this.error, serverError].filter(Boolean).filter((value, index, all) => all.indexOf(value) === index).join("；");
    const phaseHint = {
      prepare: "从已同步订单生成代发草稿，生成后仍需店主审批。",
      approve_dropship: "代发草稿已生成，请核对供应商、数量与收货信息后审批。",
      execute_dropship: "代发已批准，可执行本地写入或打开人工下单信息。",
      manual_po: "在 1688 人工下单后，把采购单号回填到这里；系统不代付。",
      prepare_tracking: "代发结果已就绪，请核对承运商与物流单号后生成物流草稿。",
      tracking_details: "请填写真实承运商与物流单号；不得使用演示数据冒充平台事实。",
      approve_tracking: "物流草稿已生成，请核对后审批；批准后才会写平台。",
      push_tracking: "物流已批准，可执行平台写入；失败原因会保留在服务器。",
      executing: "服务器正在执行外部写，请刷新核对结果，不要重复点击。",
      complete: "服务器已有物流处理记录；这不表示平台已发货或已签收。",
    };
    const stepIndex = ["prepare", "approve_dropship", "execute_dropship"].includes(phase) ? 1
      : ["manual_po", "prepare_tracking", "tracking_details", "approve_tracking", "push_tracking"].includes(phase) ? 2
      : phase === "complete" ? 3 : 1;
    const steps = [
      { label: "订单同步", hint: "已同步" },
      { label: "代发审批", hint: ds?.draft?.status === "executed" ? "已完成" : "当前" },
      { label: "物流审批", hint: phase === "complete" ? "已完成" : stepIndex === 2 ? "当前" : "待处理" },
      { label: "完成", hint: phase === "complete" ? "已完成" : "待处理" },
    ];
    const stepsHtml = steps.map((item, index) => {
      const state = index < stepIndex || phase === "complete" ? "done" : index === stepIndex ? "current" : "";
      return `<li class="wb-step ${state}"><span class="wb-step-index">${index + 1}</span><span><strong>${item.label}</strong><small>${item.hint}</small></span></li>`;
    }).join("");
    const statusLabel = this.busy ? "正在读取或处理…" : this.data ? titles[phase] : "未能读取处理进度";
    const tone = phase === "complete" ? "ok" : phase === "executing" ? "warn" : "pending";
    const dsDraft = ds?.draft;
    const trDraft = tr?.draft;
    const trackingPayload = trDraft?.payload || {};
    const shipment = tr?.shipment || {};
    const approvalCard = approval
      ? `<section class="wb-card wb-approval">
          <div class="wb-card-head"><div><span class="wb-kicker">待人工审批</span><h3>${phase === "approve_tracking" ? "物流信息" : "代发草稿"}</h3></div><span class="status-pill ${tone}">${esc(approval.status || "pending")}</span></div>
          <p class="wb-approval-summary">请核对本次${phase === "approve_tracking" ? `物流：${esc(approval.payload?.carrier)} / ${esc(approval.payload?.tracking_no)}` : "代发草稿"}。</p>
          <details class="wb-details"><summary>查看审批内容</summary><pre>${esc(JSON.stringify(approval.payload || {}, null, 2))}</pre></details>
          ${this.role === "owner"
            ? `<label class="wb-field">审批说明 / 驳回原因<input name="order_note" placeholder="驳回时必填" /></label>
               <div class="wb-action-row">${button("approve", "批准并继续", `data-approval-id="${esc(approval.id)}"`, "")} ${button("reject", "驳回", `data-approval-id="${esc(approval.id)}"`, "danger")}</div>`
            : '<p class="muted">等待店主审批；批准后可继续处理。</p>'}
        </section>`
      : "";
    const poForm = phase === "manual_po"
      ? `<section class="wb-card">
          <div class="wb-card-head"><div><span class="wb-kicker">人工步骤</span><h3>回填 1688 采购单</h3></div>${button("sheet", "查看人工下单信息", "", "ghost")}</div>
          <p class="muted">先在 1688 由人完成下单，再回填采购单号或订单链接。本后台不会自动付款。</p>
          <form id="order-po-form" class="wb-form"><label class="wb-field">1688 采购单号或订单链接<input name="alibaba_order_id" required placeholder="例如 1234567890" value="${esc(this.form.alibaba_order_id || "")}" /></label><button class="btn" ${disabled}>保存采购单并继续</button></form>
        </section>`
      : "";
    const trackingForm = dsDraft?.status === "executed" && !["complete", "executing"].includes(phase)
      ? `<section class="wb-card">
          <div class="wb-card-head"><div><span class="wb-kicker">人工填写</span><h3>物流信息</h3></div></div>
          <p class="muted">请填写真实承运商与物流单号。保存后生成待审批草稿，批准后才会写平台。</p>
          <form id="order-tracking-form" class="wb-form wb-form-grid">
            <label class="wb-field">承运商<input name="carrier" required placeholder="例如 JNT" value="${esc(this.form.carrier ?? trackingPayload.carrier ?? "")}" /></label>
            <label class="wb-field">物流单号<input name="tracking_no" required placeholder="例如 JT123456789012" value="${esc(this.form.tracking_no ?? trackingPayload.tracking_no ?? "")}" /></label>
            <button class="btn secondary" ${disabled}>保存物流草稿，核对后审批</button>
          </form>
        </section>`
      : "";
    const continueAction = ["prepare", "execute_dropship", "prepare_tracking", "push_tracking"].includes(phase) && this.role !== "viewer"
      ? `<div class="wb-action-row wb-primary-action">${button("continue", "推进到下一审批或人工步骤", "", "")}</div>`
      : "";
    const sheetCard = this.sheet
      ? `<section class="wb-card"><div class="wb-card-head"><div><span class="wb-kicker">本次查看</span><h3>人工下单信息</h3></div></div><label class="wb-field">复制以下信息到 1688 下单<textarea readonly rows="8">${esc(this.sheet)}</textarea></label></section>`
      : "";
    const completeCard = phase === "complete"
      ? `<section class="wb-card wb-complete"><div class="wb-card-head"><div><span class="wb-kicker">已完成</span><h3>物流信息处理完成</h3></div><span class="status-pill ok">completed</span></div><dl class="wb-facts"><div><dt>承运商</dt><dd>${esc(shipment.carrier || trackingPayload.carrier || "-")}</dd></div><div><dt>运单号</dt><dd class="mono">${esc(shipment.tracking_no || trackingPayload.tracking_no || "-")}</dd></div></dl><p class="muted">此进度表示服务器已有物流处理记录，不表示已发货或已签收。发货与退款请在 TikTok Shop 卖家中心由人操作。</p></section>`
      : "";
    const rail = `<aside class="wb-rail">
      <section class="wb-rail-card"><span class="wb-kicker">当前记录</span><dl class="wb-facts">
        <div><dt>代发</dt><dd><span class="status-dot ${dsDraft?.status === "executed" ? "ok" : ""}"></span>${esc(dsDraft?.status || "未开始")}</dd></div>
        <div><dt>物流</dt><dd><span class="status-dot ${trDraft?.status === "executed" ? "ok" : ""}"></span>${esc(trDraft?.status || "未开始")}</dd></div>
        <div><dt>承运商</dt><dd>${esc(shipment.carrier || trackingPayload.carrier || "-")}</dd></div>
        <div><dt>运单号</dt><dd class="mono">${esc(shipment.tracking_no || trackingPayload.tracking_no || "-")}</dd></div>
      </dl></section>
      <section class="wb-rail-card wb-guard"><span class="wb-kicker">边界</span><p>代发与物流都必须先过审批。本后台不自动下单、不付款、不发货、不退款。</p><p class="muted">外部写失败不会被静默重试；失败原因以服务器记录为准。</p></section>
      ${this.role === "viewer" ? '<p class="muted">当前为只读权限。</p>' : ""}
    </aside>`;
    return `<section class="panel wb-panel" aria-label="订单履约工作台">
      <header class="wb-head"><div><span class="wb-kicker">订单详情</span><h2>${esc(this.orderId)}</h2><p class="wb-sub">审批驱动的代发与物流处理</p></div><div class="wb-head-right"><span class="status-pill ${tone}">${statusLabel}</span><button type="button" class="btn secondary" data-order-action="refresh" ${this.busy ? "disabled" : ""}>刷新进度</button></div></header>
      <ol class="wb-steps">${stepsHtml}</ol>
      <div class="wb-body">
        <div class="wb-main">
          <div class="wb-status ${tone}"><span class="wb-status-mark"></span><div><strong role="status">${statusLabel}</strong><p>${phaseHint[phase] || ""}</p></div></div>
          ${errorText ? `<p class="error" role="alert">${esc(errorText)}</p>` : ""}
          ${continueAction}
          ${approvalCard}
          ${poForm}
          ${trackingForm}
          ${sheetCard}
          ${completeCard}
        </div>
        ${rail}
      </div>
    </section>`;

  }
}
