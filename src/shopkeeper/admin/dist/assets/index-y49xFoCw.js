(function(){const e=document.createElement("link").relList;if(e&&e.supports&&e.supports("modulepreload"))return;for(const o of document.querySelectorAll('link[rel="modulepreload"]'))r(o);new MutationObserver(o=>{for(const d of o)if(d.type==="childList")for(const i of d.addedNodes)i.tagName==="LINK"&&i.rel==="modulepreload"&&r(i)}).observe(document,{childList:!0,subtree:!0});function n(o){const d={};return o.integrity&&(d.integrity=o.integrity),o.referrerPolicy&&(d.referrerPolicy=o.referrerPolicy),o.crossOrigin==="use-credentials"?d.credentials="include":o.crossOrigin==="anonymous"?d.credentials="omit":d.credentials="same-origin",d}function r(o){if(o.ep)return;o.ep=!0;const d=n(o);fetch(o.href,d)}})();function It(){const t=document.cookie.match(/(?:^|;\s*)sk_csrf=([^;]+)/);return t?decodeURIComponent(t[1]):""}async function v(t,e={}){const n=Object.assign({Accept:"application/json"},e.headers||{}),r=(e.method||"GET").toUpperCase();r!=="GET"&&r!=="HEAD"&&(n["X-CSRF-Token"]=It()||window.__CSRF__||"",e.body&&!n["Content-Type"]&&(n["Content-Type"]="application/json"));const o=await fetch(t,{credentials:"same-origin",...e,method:r,headers:n}),d=await o.text();let i=null;try{i=d?JSON.parse(d):null}catch{i={detail:d}}if(!o.ok){const c=i&&i.detail?i.detail:o.statusText,f=new Error(typeof c=="string"?c:JSON.stringify(c));throw f.status=o.status,f.data=i,f}return i}function Rt(t,e){return v("/api/auth/login",{method:"POST",body:JSON.stringify({username:t,password:e})})}function Ot(){return v("/api/auth/logout",{method:"POST",body:"{}"})}function Tt(){return v("/api/auth/me")}const O=t=>String(t??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");function st(t){var r,o,d,i,c,f,m;const e=t==null?void 0:t.dropship,n=t==null?void 0:t.tracking;if(((r=n==null?void 0:n.draft)==null?void 0:r.status)==="executed")return"complete";if(!(e!=null&&e.draft))return"prepare";if(e.draft.status==="awaiting_manual_po")return"manual_po";if(e.draft.status!=="executed")return((o=e.approval)==null?void 0:o.status)==="executing"?"executing":((d=e.approval)==null?void 0:d.status)==="pending"?"approve_dropship":((i=e.approval)==null?void 0:i.status)==="approved"?"execute_dropship":"prepare";if(!(n!=null&&n.draft)){const p=e.draft.result||{};return p.carrier&&p.tracking_no?"prepare_tracking":"tracking_details"}return((c=n.approval)==null?void 0:c.status)==="executing"?"executing":((f=n.approval)==null?void 0:f.status)==="pending"?"approve_tracking":((m=n.approval)==null?void 0:m.status)==="approved"?"push_tracking":"tracking_details"}class At{constructor(e){this.request=e,this.orderId="",this.generation=0,this.role="viewer",this.busy=!1,this.data=null,this.error="",this.form={},this.sheet="",this.shopId="default"}clear(){this.generation+=1,this.orderId="",this.data=null,this.sheet="",this.form={},this.busy=!1,this.error="",this.paint()}attach(e,n){this.host=e,this.role=n||"viewer",this.paint()}current(e,n){return this.orderId===e&&this.generation===n}path(e,n){const r=encodeURIComponent(this.shopId||"default");return`/api/workflow/orders/${encodeURIComponent(e)}/${n}?shop_id=${r}`}async read(e,n){const r=async i=>{try{return await this.request(this.path(e,i))}catch(c){if(c.status===404)return null;throw c}},[o,d]=await Promise.all([r("dropship"),r("tracking")]);return this.current(e,n)?(this.data={dropship:o,tracking:d},!0):!1}async open(e){this.clear(),this.orderId=e;const n=this.generation;this.busy=!0,this.error="",this.paint();try{await this.read(e,n)}catch(r){this.current(e,n)&&(this.error=r.message)}finally{this.current(e,n)&&(this.busy=!1,this.paint())}}async post(e,n,r={}){return this.request(this.path(e,n),{method:"POST",body:JSON.stringify(r)})}async advance(e,n){for(let r=0;r<4;r+=1){if(!await this.read(e,n))return;const o=st(this.data),d={prepare:"dropship/draft",execute_dropship:"dropship/execute",prepare_tracking:"tracking/draft",push_tracking:"tracking/push"}[o];if(!d)return;await this.post(e,d)}throw new Error("流程状态未推进，请刷新后核对执行结果。")}async act(e,n={}){var d;if(this.busy||!this.orderId||e!=="refresh"&&this.role==="viewer"||(e==="approve"||e==="reject")&&this.role!=="owner")return;const r=this.orderId,o=this.generation;this.busy=!0,this.error="",this.form={...this.form,...n},this.paint();try{if(!await this.read(r,o))return;if(e==="approve"||e==="reject"){const i=st(this.data),c=i==="approve_dropship"?this.data.dropship.approval:i==="approve_tracking"?this.data.tracking.approval:null;if(!c||c.id!==n.approvalId)throw new Error("审批内容或状态已变化，请重新核对后操作。");if(e==="reject"&&!((d=n.note)!=null&&d.trim()))throw new Error("请填写驳回原因。");await this.request(`/api/workflow/approvals/${encodeURIComponent(c.id)}/${e}`,{method:"POST",body:JSON.stringify({note:n.note||"订单履约工作台批准并继续",expected_hash:c.payload_hash||""})}),e==="approve"&&this.current(r,o)&&await this.advance(r,o)}else if(e==="continue")await this.advance(r,o);else if(e==="record_po")await this.post(r,"dropship/record-po",{alibaba_order_id:n.alibaba_order_id}),this.current(r,o)&&await this.advance(r,o);else if(e==="tracking")await this.post(r,"tracking/draft",{carrier:n.carrier,tracking_no:n.tracking_no});else if(e==="sheet"){const i=await this.request(this.path(r,"dropship/manual-sheet"));this.current(r,o)&&(this.sheet=i.copy_text||"")}await this.read(r,o)}catch(i){if(this.current(r,o)){this.error=i.message||"处理失败，请核对服务器执行结果。";try{await this.read(r,o)}catch{}}}finally{this.current(r,o)&&(this.busy=!1,this.paint())}}paint(){var e,n,r;(e=this.host)!=null&&e.isConnected&&(this.host.innerHTML=this.html(),this.host.querySelectorAll("[data-order-action]").forEach(o=>{o.onclick=()=>{var d;return this.act(o.dataset.orderAction,{approvalId:o.dataset.approvalId,note:((d=this.host.querySelector("[name=order_note]"))==null?void 0:d.value)||""})}}),(n=this.host.querySelector("#order-po-form"))==null||n.addEventListener("submit",o=>{o.preventDefault(),this.act("record_po",Object.fromEntries(new FormData(o.currentTarget)))}),(r=this.host.querySelector("#order-tracking-form"))==null||r.addEventListener("submit",o=>{o.preventDefault(),this.act("tracking",Object.fromEntries(new FormData(o.currentTarget)))}))}html(){var h,l,u,b,A;if(!this.orderId)return'<p class="muted">选择订单后可查看代发与物流处理进度。</p>';const e=st(this.data),n=(h=this.data)==null?void 0:h.dropship,r=(l=this.data)==null?void 0:l.tracking,o=e==="approve_dropship"?n.approval:e==="approve_tracking"?r.approval:null,d={prepare:"准备代发草稿",approve_dropship:"等待代发审批",execute_dropship:"代发已批准，可继续",manual_po:"等待人工回填采购单",prepare_tracking:"准备物流草稿",tracking_details:"等待填写物流信息",approve_tracking:"等待物流审批",push_tracking:"物流已批准，可继续",executing:"服务器正在执行，请刷新核对",complete:"物流信息处理完成"},i=this.busy||this.role==="viewer"?"disabled":"",c=(w,B,F="",E="secondary")=>`<button type="button" class="btn ${E}" data-order-action="${w}" ${i} ${F}>${B}</button>`,f=w=>{var F,E,U;const B=(F=w==null?void 0:w.draft)==null?void 0:F.result;return B&&(B.code||B.message)?[B.code,B.message].filter(Boolean).join(" "):(E=w==null?void 0:w.approval)!=null&&E.execution_error?`执行失败：${w.approval.execution_error}`:((U=w==null?void 0:w.approval)==null?void 0:U.status)==="rejected"?`已驳回：${w.approval.decision_note||"未填写原因"}`:""},m=f(r)||f(n),p=[this.error,m].filter(Boolean).filter((w,B,F)=>F.indexOf(w)===B).join("；"),_={prepare:"从已同步订单生成代发草稿，生成后仍需店主审批。",approve_dropship:"代发草稿已生成，请核对供应商、数量与收货信息后审批。",execute_dropship:"代发已批准，可执行本地写入或打开人工下单信息。",manual_po:"在 1688 人工下单后，把采购单号回填到这里；系统不代付。",prepare_tracking:"代发结果已就绪，请核对承运商与物流单号后生成物流草稿。",tracking_details:"请填写真实承运商与物流单号；不得使用演示数据冒充平台事实。",approve_tracking:"物流草稿已生成，请核对后审批；批准后才会写平台。",push_tracking:"物流已批准，可执行平台写入；失败原因会保留在服务器。",executing:"服务器正在执行外部写，请刷新核对结果，不要重复点击。",complete:"服务器已有物流处理记录；这不表示平台已发货或已签收。"},S=["prepare","approve_dropship","execute_dropship"].includes(e)?1:["manual_po","prepare_tracking","tracking_details","approve_tracking","push_tracking"].includes(e)?2:e==="complete"?3:1,$=[{label:"订单同步",hint:"已同步"},{label:"代发审批",hint:((u=n==null?void 0:n.draft)==null?void 0:u.status)==="executed"?"已完成":"当前"},{label:"物流审批",hint:e==="complete"?"已完成":S===2?"当前":"待处理"},{label:"完成",hint:e==="complete"?"已完成":"待处理"}].map((w,B)=>`<li class="wb-step ${B<S||e==="complete"?"done":B===S?"current":""}"><span class="wb-step-index">${B+1}</span><span><strong>${w.label}</strong><small>${w.hint}</small></span></li>`).join(""),y=this.busy?"正在读取或处理…":this.data?d[e]:"未能读取处理进度",L=e==="complete"?"ok":e==="executing"?"warn":"pending",R=n==null?void 0:n.draft,x=r==null?void 0:r.draft,P=(x==null?void 0:x.payload)||{},H=(r==null?void 0:r.shipment)||{},W=o?`<section class="wb-card wb-approval">
          <div class="wb-card-head"><div><span class="wb-kicker">待人工审批</span><h3>${e==="approve_tracking"?"物流信息":"代发草稿"}</h3></div><span class="status-pill ${L}">${O(o.status||"pending")}</span></div>
          <p class="wb-approval-summary">请核对本次${e==="approve_tracking"?`物流：${O((b=o.payload)==null?void 0:b.carrier)} / ${O((A=o.payload)==null?void 0:A.tracking_no)}`:"代发草稿"}。</p>
          <details class="wb-details"><summary>查看审批内容</summary><pre>${O(JSON.stringify(o.payload||{},null,2))}</pre></details>
          ${this.role==="owner"?`<label class="wb-field">审批说明 / 驳回原因<input name="order_note" placeholder="驳回时必填" /></label>
               <div class="wb-action-row">${c("approve","批准并继续",`data-approval-id="${O(o.id)}"`,"")} ${c("reject","驳回",`data-approval-id="${O(o.id)}"`,"danger")}</div>`:'<p class="muted">等待店主审批；批准后可继续处理。</p>'}
        </section>`:"",C=e==="manual_po"?`<section class="wb-card">
          <div class="wb-card-head"><div><span class="wb-kicker">人工步骤</span><h3>回填 1688 采购单</h3></div>${c("sheet","查看人工下单信息","","ghost")}</div>
          <p class="muted">先在 1688 由人完成下单，再回填采购单号或订单链接。本后台不会自动付款。</p>
          <form id="order-po-form" class="wb-form"><label class="wb-field">1688 采购单号或订单链接<input name="alibaba_order_id" required placeholder="例如 1234567890" value="${O(this.form.alibaba_order_id||"")}" /></label><button class="btn" ${i}>保存采购单并继续</button></form>
        </section>`:"",J=(R==null?void 0:R.status)==="executed"&&!["complete","executing"].includes(e)?`<section class="wb-card">
          <div class="wb-card-head"><div><span class="wb-kicker">人工填写</span><h3>物流信息</h3></div></div>
          <p class="muted">请填写真实承运商与物流单号。保存后生成待审批草稿，批准后才会写平台。</p>
          <form id="order-tracking-form" class="wb-form wb-form-grid">
            <label class="wb-field">承运商<input name="carrier" required placeholder="例如 JNT" value="${O(this.form.carrier??P.carrier??"")}" /></label>
            <label class="wb-field">物流单号<input name="tracking_no" required placeholder="例如 JT123456789012" value="${O(this.form.tracking_no??P.tracking_no??"")}" /></label>
            <button class="btn secondary" ${i}>保存物流草稿，核对后审批</button>
          </form>
        </section>`:"",z=["prepare","execute_dropship","prepare_tracking","push_tracking"].includes(e)&&this.role!=="viewer"?`<div class="wb-action-row wb-primary-action">${c("continue","推进到下一审批或人工步骤","","")}</div>`:"",X=this.sheet?`<section class="wb-card"><div class="wb-card-head"><div><span class="wb-kicker">本次查看</span><h3>人工下单信息</h3></div></div><label class="wb-field">复制以下信息到 1688 下单<textarea readonly rows="8">${O(this.sheet)}</textarea></label></section>`:"",Y=e==="complete"?`<section class="wb-card wb-complete"><div class="wb-card-head"><div><span class="wb-kicker">已完成</span><h3>物流信息处理完成</h3></div><span class="status-pill ok">completed</span></div><dl class="wb-facts"><div><dt>承运商</dt><dd>${O(H.carrier||P.carrier||"-")}</dd></div><div><dt>运单号</dt><dd class="mono">${O(H.tracking_no||P.tracking_no||"-")}</dd></div></dl><p class="muted">此进度表示服务器已有物流处理记录，不表示已发货或已签收。发货与退款请在 TikTok Shop 卖家中心由人操作。</p></section>`:"",Z=`<aside class="wb-rail">
      <section class="wb-rail-card"><span class="wb-kicker">当前记录</span><dl class="wb-facts">
        <div><dt>代发</dt><dd><span class="status-dot ${(R==null?void 0:R.status)==="executed"?"ok":""}"></span>${O((R==null?void 0:R.status)||"未开始")}</dd></div>
        <div><dt>物流</dt><dd><span class="status-dot ${(x==null?void 0:x.status)==="executed"?"ok":""}"></span>${O((x==null?void 0:x.status)||"未开始")}</dd></div>
        <div><dt>承运商</dt><dd>${O(H.carrier||P.carrier||"-")}</dd></div>
        <div><dt>运单号</dt><dd class="mono">${O(H.tracking_no||P.tracking_no||"-")}</dd></div>
      </dl></section>
      <section class="wb-rail-card wb-guard"><span class="wb-kicker">边界</span><p>代发与物流都必须先过审批。本后台不自动下单、不付款、不发货、不退款。</p><p class="muted">外部写失败不会被静默重试；失败原因以服务器记录为准。</p></section>
      ${this.role==="viewer"?'<p class="muted">当前为只读权限。</p>':""}
    </aside>`;return`<section class="panel wb-panel" aria-label="订单履约工作台">
      <header class="wb-head"><div><span class="wb-kicker">订单详情</span><h2>${O(this.orderId)}</h2><p class="wb-sub">审批驱动的代发与物流处理</p></div><div class="wb-head-right"><span class="status-pill ${L}">${y}</span><button type="button" class="btn secondary" data-order-action="refresh" ${this.busy?"disabled":""}>刷新进度</button></div></header>
      <ol class="wb-steps">${$}</ol>
      <div class="wb-body">
        <div class="wb-main">
          <div class="wb-status ${L}"><span class="wb-status-mark"></span><div><strong role="status">${y}</strong><p>${_[e]||""}</p></div></div>
          ${p?`<p class="error" role="alert">${O(p)}</p>`:""}
          ${z}
          ${W}
          ${C}
          ${J}
          ${X}
          ${Y}
        </div>
        ${Z}
      </div>
    </section>`}}const V=new At(v),M={overview:'<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',workflows:'<svg viewBox="0 0 24 24"><path d="M4 6h10"/><path d="M4 12h16"/><path d="M4 18h8"/><circle cx="18" cy="6" r="2"/><circle cx="10" cy="18" r="2"/></svg>',runs:'<svg viewBox="0 0 24 24"><path d="M8 6h12"/><path d="M8 12h12"/><path d="M8 18h12"/><circle cx="4.2" cy="6" r="1.3"/><circle cx="4.2" cy="12" r="1.3"/><circle cx="4.2" cy="18" r="1.3"/></svg>',agents:'<svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="2.4"/><circle cx="16" cy="8" r="2.4"/><path d="M4.2 18.2c.7-2.2 2.2-3.3 3.8-3.3s3.1 1.1 3.8 3.3"/><path d="M12.2 18.2c.7-2.2 2.2-3.3 3.8-3.3s3.1 1.1 3.8 3.3"/></svg>',scout:'<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><path d="M20 20l-3.5-3.5"/></svg>',products:'<svg viewBox="0 0 24 24"><path d="M4 8l8-4 8 4-8 4-8-4z"/><path d="M4 12l8 4 8-4"/><path d="M4 16l8 4 8-4"/></svg>',approvals:'<svg viewBox="0 0 24 24"><path d="M8 6h11"/><path d="M8 12h11"/><path d="M8 18h11"/><path d="M4 6h.01"/><path d="M4 12h.01"/><path d="M4 18h.01"/></svg>',orders:'<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h8"/><path d="M8 13h5"/></svg>',reports:'<svg viewBox="0 0 24 24"><path d="M5 19V9"/><path d="M10 19V5"/><path d="M15 19v-7"/><path d="M20 19V8"/></svg>',profit:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 7v10"/><path d="M9.4 9.3c.7-1 1.7-1.5 2.6-1.5 1.4 0 2.4.8 2.4 2 0 2.4-4 1.6-4 3.8 0 1.1.9 2 2.4 2 .9 0 1.9-.4 2.6-1.3"/></svg>',settings:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4"/></svg>',cs:'<svg viewBox="0 0 24 24"><path d="M5 6.5A2.5 2.5 0 0 1 7.5 4h9A2.5 2.5 0 0 1 19 6.5v7A2.5 2.5 0 0 1 16.5 16H9l-4 3.2V6.5z"/></svg>',outreach:'<svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="2.2"/><path d="M4.2 16.8c.6-2 2-3 3.8-3s3.2 1 3.8 3"/><path d="M14 6.5h6.2v5.2H16L14 14.2V6.5z"/></svg>',moon:'<svg viewBox="0 0 24 24"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 7 7 0 1 0 20 14.5z"/></svg>',sun:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M17.7 6.3l1.4-1.4M4.9 19.1l1.4-1.4"/></svg>',user:'<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.5"/><path d="M5 19c1.5-3 4-4.5 7-4.5s5.5 1.5 7 4.5"/></svg>',check:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9"/></svg>',logo:'<svg viewBox="0 0 24 24"><path d="M5 16l7-12 7 12H5z"/><path d="M9 16l3 5 3-5"/></svg>'},bt=[{label:"运营",items:[{id:"overview",label:"总览"},{id:"workflows",label:"AI 工作流"},{id:"runs",label:"编排运行"},{id:"agents",label:"AI 代理"},{id:"scout",label:"选品"},{id:"spotcheck",label:"人工抽检"},{id:"products",label:"商品"},{id:"profit",label:"利润"}]},{label:"治理",items:[{id:"approvals",label:"审批"},{id:"cs",label:"客服"},{id:"outreach",label:"达人建联"},{id:"orders",label:"订单"},{id:"reports",label:"报表"},{id:"settings",label:"设置"}]}],$t=bt.flatMap(t=>t.items),s={user:null,status:null,shop:null,route:"overview",approvals:[],selectedApproval:null,publishPreview:null,runs:[],selectedRun:null,orchRuns:[],orchRun:null,agentStats:null,agentRun:null,agentFrom:"",agentTo:"",error:"",theme:localStorage.getItem("admin-theme")||"light",lang:localStorage.getItem("admin-lang")||"zh",profitShop:"",shops:[],activeShop:"",shopGeneration:0,selectedOrderId:"",orderPlatform:"",profitFrom:"",profitTo:"",profitShops:[],profit:null,adSpendRows:[],csInbox:[],csDrafts:[],csNote:"",csError:"",outreachDrafts:[],outreachNote:"",outreachError:""},Ct="https://detail.1688.com/offer/600123456789.html";function a(t){return String(t??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}function ct(){document.documentElement.setAttribute("data-theme",s.theme==="dark"?"dark":"light")}function Bt(){if(location.pathname==="/dashboard/profit")return"profit";const e=(location.pathname.replace(/^\/admin\/?/,"").split("/")[0]||"overview").toLowerCase();return e==="login"?"login":$t.some(n=>n.id===e)?e:"overview"}function lt(){try{return new URLSearchParams(location.search).get("run")||""}catch{return""}}function jt(){try{return new URLSearchParams(location.search).get("id")||""}catch{return""}}function K(t,e){const n=e&&Object.keys(e).length?`?${new URLSearchParams(e).toString()}`:"",r=t==="login"?"/admin/login":`/admin/${t}${n}`;`${location.pathname}${location.search}`!==r&&history.pushState({},"",r),s.route=t,g()}function Lt(){const t=$t.find(e=>e.id===s.route);return t?t.label:"总览"}function k(t,e){return`<div class="empty"><strong>${a(t)}</strong>${a(e)}</div>`}function et(){ct();const t=document.getElementById("app");t.innerHTML=`
    <div class="login-page">
      <form class="login-card" id="login-form">
        <div class="brand">
          <div class="brand-logo">${M.logo}</div>
          <div class="brand-text">
            <div class="brand-name">AI 店长</div>
            <p class="brand-sub">管理控制台</p>
          </div>
        </div>
        <h1>登录</h1>
        <p class="lead">TikTok Shop 泰国站 · 家居收纳。涉钱动作仍走审批闸门。</p>
        ${s.error?`<p class="error">${a(s.error)}</p>`:""}
        <div class="field"><label>用户名</label><input name="username" autocomplete="username" required /></div>
        <div class="field"><label>密码</label><input name="password" type="password" autocomplete="current-password" required /></div>
        <button class="btn" type="submit" style="width:100%">登录</button>
        <p class="muted" style="margin:14px 0 0;font-size:11px">开发自测页仍在 <a href="/workflow">/workflow</a>。</p>
      </form>
    </div>
  `,document.getElementById("login-form").onsubmit=async e=>{e.preventDefault();const n=new FormData(e.target);s.error="";try{const r=await Rt(String(n.get("username")||""),String(n.get("password")||""));s.user=r.user,window.__CSRF__=r.csrf_token||"",K("overview"),await N(),g()}catch(r){s.error=r.message||"登录失败",et()}}}function Nt(){var n;const t=s.shops||[];if(t.length<=1){const r=t[0]||{};return`<span class="capsule" title="当前店铺">${a(r.name||((n=s.shop)==null?void 0:n.name)||"默认店铺")}</span>`}const e=s.activeShop||"";return`<span class="capsule shop-switch">
    <select id="shop-switch" title="切换店铺">${t.map(r=>`<option value="${a(r.shop_id)}" ${r.shop_id===e?"selected":""}>${a(r.name||r.shop_id)}</option>`).join("")}</select>
  </span>`}function Pt(t){ct();const e=s.user||{},n=s.shop||{name:"泰国站 · 家居收纳"},r=s.overview?s.overview.system_ok!==!1:!0,o=bt.map(d=>`
      <div class="nav-group">
        <div class="nav-label">${a(d.label)}</div>
        ${d.items.map(i=>`<a href="/admin/${i.id}" data-nav="${i.id}" class="${s.route===i.id?"active":""}">${M[i.id]||""}<span>${a(i.label)}</span></a>`).join("")}
      </div>`).join("");return`
    <div class="app-shell">
      <aside class="sidebar" id="sidebar">
        <div class="brand">
          <div class="brand-logo">${M.logo}</div>
          <div class="brand-text">
            <div class="brand-name">AI 店长</div>
            <p class="brand-sub">管理控制台</p>
          </div>
        </div>
        <nav class="nav">${o}</nav>
        <div class="sidebar-foot">店铺 ${a(n.name||n.id||"-")}<br/>发货 / 退款请到卖家中心</div>
      </aside>
      <div class="nav-scrim" id="nav-scrim"></div>
      <div class="main">
        <header class="topbar">
          <div class="topbar-left">
            <button class="capsule nav-toggle" type="button" id="btn-menu" aria-label="打开导航" aria-controls="sidebar" aria-expanded="false">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
            </button>
            <div class="top-title">${a(Lt())}</div>
          </div>
          <div class="top-right">
            ${Nt()}
            <button class="capsule" type="button" id="btn-theme" title="主题">${s.theme==="dark"?M.sun:M.moon}</button>
            <button class="capsule" type="button" id="btn-lang">${a(s.lang==="en"?"EN":"中文")}</button>
            <span class="capsule"><span class="dot" style="${r?"":"background:var(--bad)"}"></span>${r?"系统正常":"需关注"}</span>
            <button class="capsule user" type="button" id="btn-logout">${M.user}<span>${a(e.username||"-")}</span></button>
          </div>
        </header>
        <main class="content">${t}</main>
      </div>
    </div>
  `}function Mt(){const t=document.querySelector(".app-shell"),e=document.getElementById("btn-menu"),n=document.getElementById("nav-scrim"),r=m=>{t==null||t.classList.toggle("nav-open",m),e==null||e.setAttribute("aria-expanded",m?"true":"false")};e==null||e.addEventListener("click",()=>r(!(t!=null&&t.classList.contains("nav-open")))),n==null||n.addEventListener("click",()=>r(!1)),document.onkeydown=m=>{m.key==="Escape"&&r(!1)};const o={overview:"OPERATIONS",workflows:"ORCHESTRATION",runs:"RUN QUEUE",agents:"AI AGENTS",scout:"SOURCING",spotcheck:"QUALITY",products:"CATALOG",profit:"PROFIT",approvals:"GOVERNANCE",cs:"SUPPORT",outreach:"CREATOR OPS",orders:"FULFILLMENT",reports:"REPORTS",settings:"SETTINGS"};document.querySelectorAll(".page-head").forEach(m=>{const p=m.querySelector(":scope > div:first-child");if(!p||p.querySelector(".eyebrow"))return;const _=document.createElement("div");_.className="eyebrow",_.textContent=o[s.route]||"WORKSPACE",p.prepend(_)}),document.querySelectorAll("[data-nav]").forEach(m=>{m.addEventListener("click",p=>{p.preventDefault(),K(m.getAttribute("data-nav"))})});const d=document.getElementById("shop-switch");d&&(d.onchange=()=>{s.activeShop=d.value,s.shopGeneration+=1,s.syncedOrders=[],s.sla=null,s.selectedOrderId="",V.clear(),s.route==="orders"&&history.replaceState({},"","/admin/orders");try{window.localStorage.setItem("shopkeeper.activeShop",s.activeShop)}catch{}g()});const i=document.getElementById("btn-theme");i&&(i.onclick=()=>{s.theme=s.theme==="dark"?"light":"dark",localStorage.setItem("admin-theme",s.theme),ct(),g()});const c=document.getElementById("btn-lang");c&&(c.onclick=()=>{s.lang=s.lang==="en"?"zh":"en",localStorage.setItem("admin-lang",s.lang),g()});const f=document.getElementById("btn-logout");f&&(f.onclick=async()=>{try{await Ot()}catch{}s.user=null,K("login")})}async function N(){const[t,e]=await Promise.all([v("/api/admin/overview"),v("/api/admin/status")]);s.overview=t,s.status=e,s.shop=t.shop,await Ut()}async function Ut(){var t;try{const e=await v("/api/workflow/shops");s.shops=e.shops||[]}catch{s.shops=[{shop_id:"default",name:((t=s.shop)==null?void 0:t.name)||"默认店铺"}];return}if(!s.activeShop){try{s.activeShop=window.localStorage.getItem("shopkeeper.activeShop")||""}catch{s.activeShop=""}(s.shops||[]).some(n=>n.shop_id===s.activeShop)||(s.activeShop=(s.shops[0]||{}).shop_id||"")}}function Ft(t){return t&&t.tone==="bad"?"bad":t&&t.tone==="warn"?"warn":t&&t.tone==="idle"?"idle":t&&t.ok===!1?"bad":""}function pt(){const t=s.overview||{},e=t.banner||"系统正常 · 全部组件运行中",n=t.system_ok!==!1,r=t.components||[],o=t.facts||[],d=t.switches||[],i=s.user&&s.user.role!=="viewer",c=[["待审批",t.pending_approvals??0,"八态审批收件箱"],["运行中工作流",t.running_workflows??0,"编排器未终态 run"],["临期订单",t.orders_due_soon??0,"本地 SLA 提醒"],["超时订单",t.orders_overdue??0,"请去卖家中心打单"]],f=t.alerts||[],m=o.length?`<div class="banner-facts">${o.map(p=>`<span class="pill ${p.ok===!1?"bad":""}" title="${a(p.hint||"")}">${a(p.label)} ${a(p.value)}</span>`).join("")}</div>`:"";return`
    <div class="banner ${n?"":"warn"}">${M.check}<div class="banner-body"><span>${a(e)}</span>${m}</div></div>
    <div class="page-head">
      <div>
        <h1>总览</h1>
        <p>组件健康只读自 /healthz、工作流 status 与编排 flows。无数据标缺数据，不编造。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" id="btn-demo-orch" ${i?"":"disabled"} title="${i?"强制 mock，写入本服务工作台库":"只读账号不能触发演示"}">一键 mock 演示</button>
        <button class="btn secondary" type="button" id="btn-refresh-overview">刷新</button>
      </div>
    </div>
    <div class="grid-4" style="margin-bottom:12px">
      ${r.map(p=>`
        <section class="card health-card">
          <div class="health-top">
            <span class="health-dot ${Ft(p)}"></span>
            <span class="health-name">${a(p.name)}</span>
            <span class="tag-mono">${a(p.tag)}</span>
          </div>
          <div class="health-desc">${a(p.desc||"")}</div>
        </section>`).join("")}
    </div>
    <div class="grid-4">
      ${c.map(([p,_,S])=>`
        <section class="card">
          <h3>${a(p)}</h3>
          <div class="metric">${a(_)}</div>
          <div class="hint">${a(S)}</div>
        </section>`).join("")}
    </div>
    <div class="split" style="margin-top:12px">
      <section class="panel">
        <div class="panel-head"><h2>快捷开关</h2><span class="muted">只读 · 危险项不可在线打开</span></div>
        <div class="panel-body">
          ${d.map(p=>`
            <div class="switch-row">
              <div class="switch-meta">
                <strong>${a(p.name)}</strong>
                <span>${a(p.hint||"")}</span>
              </div>
              <div class="toggle-wrap">
                <span class="tag">${p.on?"已开启":"已关闭"}</span>
                <div class="toggle ${p.on?"on":""}" title="只读"></div>
              </div>
            </div>`).join("")||k("无开关","环境开关会显示在这里。")}
        </div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>最近告警 / 任务失败</h2></div>
        <div class="panel-body">
          ${f.length?`<table><thead><tr><th>类型</th><th>标题</th><th>详情</th><th>时间</th></tr></thead><tbody>
              ${f.map(p=>`<tr>
                  <td>${a(p.kind)}</td><td>${a(p.title)}</td>
                  <td>${a(p.detail)}</td><td>${a(p.at)}</td></tr>`).join("")}
              </tbody></table>`:k("暂无告警","任务失败或审批执行失败会出现在这里。")}
        </div>
      </section>
    </div>
  `}async function dt(t){const e=await v("/api/admin/approvals");s.approvals=e.approvals||[],t?(s.selectedApproval=await v(`/api/admin/approvals/${encodeURIComponent(t)}`),s.publishPreview=await ut(t)):s.approvals[0]?(s.selectedApproval=await v(`/api/admin/approvals/${encodeURIComponent(s.approvals[0].id)}`),s.publishPreview=await ut(s.approvals[0].id)):s.selectedApproval=null}async function ut(t){try{return await v(`/api/admin/approvals/${encodeURIComponent(t)}/publish-preview`)}catch(e){return{error:String(e&&e.message?e.message:e)}}}function Ht(t){const e=t&&t.payload&&t.payload.input||{},n=!!(t&&t.platform_product_id);return`
    <h3 style="margin-top:14px">上架必需字段</h3>
    <p class="muted" style="margin:0 0 8px">
      真写上架需要类目、仓库与主图。补齐后草稿会回到「待审」，需重新通过才能上架。
    </p>
    <div class="field"><label>类目 ID（category_id）</label>
      <input id="pi-category" value="${a(e.category_id||"")}" placeholder="例如 600001" /></div>
    <div class="field"><label>仓库 ID（warehouse_id）</label>
      <input id="pi-warehouse" value="${a(e.warehouse_id||"")}" placeholder="例如 1234567890" /></div>
    <div class="field"><label>价格（THB）</label>
      <input id="pi-price" value="${a(e.price_amount||"")}" placeholder="例如 99" /></div>
    <div class="field"><label>本地主图路径（工作目录内）</label>
      <input id="pi-image" value="${a(e.image_path||"")}" placeholder="例如 artifacts/demo/images/x_EN.png" /></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
      <button class="btn" type="button" id="btn-save-inputs">保存上架字段</button>
      ${n?'<span class="muted">该草稿已有平台商品 ID，改动不影响已上架记录</span>':""}
    </div>
    <div id="pi-msg" class="muted" style="margin-top:6px"></div>
  `}function qt(t){if(!t)return"";if(t.error)return`<h3 style="margin-top:14px">上架前预览</h3><p class="muted">取不到预览：${a(t.error)}</p>`;const e=(t.warnings||[]).length?`<ul class="compliance">${t.warnings.map(o=>`<li>${a(o)}</li>`).join("")}</ul>`:'<p class="muted">无阻塞项。</p>',n=t.copy||{};return`
    <h3 style="margin-top:14px">上架前预览</h3>
    <p class="muted">写通道 ${t.write_armed?'<span class="tag approved">已武装</span>':'<span class="tag pending">未武装</span>'} · 载荷哈希 ${a(String(t.payload_hash||"").slice(0,12))}…${t.preview_consistent?"":" · <strong>草稿在审批后被改过</strong>"}</p>
    ${e}
    <h3 style="margin-top:12px">将发给平台的文案</h3>
    <div class="diff">
      <div><div class="muted" style="margin-bottom:6px">泰语标题</div><pre>${a(n.title_th||"—")}</pre></div>
      <div><div class="muted" style="margin-bottom:6px">英语标题</div><pre>${a(n.title_en||"—")}</pre></div>
    </div>
    <div class="diff" style="margin-top:8px">
      <div><div class="muted" style="margin-bottom:6px">泰语正文</div><pre>${a(n.body_th||"—")}</pre></div>
      <div><div class="muted" style="margin-bottom:6px">英语正文</div><pre>${a(n.body_en||"—")}</pre></div>
    </div>
    <h3 style="margin-top:12px">图片</h3>
    <div class="images">${(t.images||[]).length?t.images.map(o=>`<img src="${a(o)}" alt="主图" />`).join(""):'<span class="muted">无主图</span>'}</div>
  `}function Dt(){const t=s.approvals||[],e=s.selectedApproval,n=s.user&&s.user.role==="owner",r=t.length?`<table><thead><tr><th>动作</th><th>状态</th><th>实体</th><th>创建</th></tr></thead><tbody>
      ${t.map(d=>{const i=e&&e.id===d.id?' style="background:var(--bg)"':"";return`<tr class="clickable" data-approval="${a(d.id)}"${i}>
            <td>${a(d.action_type)}</td>
            <td><span class="tag ${a(d.status)}">${a(d.status)}</span></td>
            <td>${a(d.entity_type)} / ${a(d.entity_id)}</td>
            <td>${a(d.created_at)}</td>
          </tr>`}).join("")}
      </tbody></table>`:k("暂无审批单","生成 listing 草稿后会出现 create_listing 待审单。");let o=k("选择审批单","左侧点一行查看详情。");if(e){const d=e.detail||{},i=(d.images||[]).map(c=>`<img src="${a(c)}" alt="商品图" />`).join("");o=`
      <div class="panel-head">
        <h2>${a(e.action_type)} · ${a(e.status)}</h2>
        <span class="muted">审批人 ${a(e.decided_by||"—")}</span>
      </div>
      <div class="panel-body">
        <p class="muted">实体 ${a(e.entity_type)} / ${a(e.entity_id)} · 载荷哈希 ${a(String(e.payload_hash||"").slice(0,12))}…</p>
        <h3>图片</h3>
        <div class="images">${i||'<span class="muted">无图片</span>'}</div>
        <h3 style="margin-top:14px">改动前后</h3>
        <div class="diff">
          <div><div class="muted" style="margin-bottom:6px">改前</div><pre>${a(JSON.stringify(d.before||{},null,2))}</pre></div>
          <div><div class="muted" style="margin-bottom:6px">改后</div><pre>${a(JSON.stringify(d.after||{},null,2))}</pre></div>
        </div>
        <h3 style="margin-top:14px">合规提示</h3>
        <ul class="compliance">${(d.compliance||[]).map(c=>`<li>${a(c)}</li>`).join("")}</ul>
        ${Ht(e)}
        ${qt(s.publishPreview)}
        ${e.status==="pending"?`<div style="margin-top:16px">
                <div class="field"><label>决定说明（打回必填）</label>
                <textarea id="decision-note" placeholder="通过可写简短说明；打回必须写原因"></textarea></div>
                <div style="display:flex;gap:8px;flex-wrap:wrap">
                  <button class="btn" type="button" id="btn-approve" ${n?"":"disabled"}>通过</button>
                  <button class="btn danger" type="button" id="btn-reject" ${n?"":"disabled"}>打回</button>
                  ${n?"":'<span class="muted">仅 owner 可审批</span>'}
                </div>
              </div>`:`<p class="muted" style="margin-top:12px">决定说明：${a(e.decision_note||"—")}</p>`}
      </div>
    `}return`
    <div class="page-head">
      <div><h1>审批</h1><p>八态审批收件箱。通过/打回写入审批人账号。</p></div>
    </div>
    <div class="split">
      <section class="panel"><div class="panel-head"><h2>列表</h2></div><div class="panel-body">${r}</div></section>
      <section class="panel" id="approval-detail">${o}</section>
    </div>
  `}function Jt(t){const e=String(t.platform_audit_status||"").toUpperCase();if(!e)return t.platform_product_id?'<span class="muted">未查询</span>':'<span class="muted">—</span>';const n=e==="APPROVED"?"approved":e==="FAILED"?"failed":"pending",r=t.platform_audit_reason?` title="${a(t.platform_audit_reason)}"`:"";return`<span class="tag ${n}"${r}>${a(e)}</span>`}function Gt(){const t=s.catalog||[];return`
    <div class="page-head">
      <div><h1>商品</h1><p>本地商品库（接现有 catalog API）。</p></div>
      <button class="btn secondary" type="button" id="btn-import-demo">导入 Demo 包</button>
    </div>
    <section class="panel"><div class="panel-body">
      ${t.length?`<table><thead><tr><th>标题</th><th>审批</th><th>平台商品 ID</th><th>平台审核</th><th>更新</th></tr></thead><tbody>
          ${t.map(e=>`<tr>
              <td>${a(e.title)}</td>
              <td><span class="tag ${a(e.approval_status)}">${a(e.approval_status)}</span></td>
              <td>${a(e.platform_product_id||"—")}</td>
              <td>${Jt(e)}</td>
              <td>${a(e.updated_at||e.created_at||"")}</td>
            </tr>`).join("")}
          </tbody></table>`:k("商品库为空","可从 Demo 交付包导入，或审批上架成功后自动写入。")}
    </div></section>
  `}function Wt(){const t=s.syncedOrders||[],e=s.sla||{},n=[...new Set(t.map(i=>String(i.status||"").trim()).filter(Boolean))],r=[...new Set(t.map(i=>String(i.platform||"tiktok")))],o=(i,c,f,m="")=>`
    <article class="kpi-card ${m}">
      <span class="kpi-label">${a(i)}</span>
      <strong class="kpi-value">${a(c)}</strong>
      <span class="kpi-hint">${a(f)}</span>
    </article>`,d=t.map(i=>{const c=String(i.order_id||i.id||""),f=String(i.status||"未知状态"),m=String(i._synced_at||"—"),p=String(i.platform||"tiktok"),_=p!=="tiktok",S=s.selectedOrderId===c?"is-selected":"",I=_?' disabled title="Shopee 当前只读导入；代发/物流写尚未接入。"':"";return`<button type="button" class="order-row ${S}" data-order-work="${a(c)}" data-order-status="${a(f)}" data-order-platform="${a(p)}" data-order-search="${a(`${c} ${f} ${p} ${m}`.toLowerCase())}"${I}>
        <span class="order-row-avatar">${a(c.slice(-2).toUpperCase()||"?")}</span>
        <span class="order-row-main"><strong>${a(c)}</strong><span class="order-row-meta">${a(p)} · ${a(f)} · ${a(m)}</span></span>
        <span class="order-row-arrow" aria-hidden="true">${_?"只读":"→"}</span>
      </button>`}).join("");return`
    <div class="orders-page">
      <div class="page-head orders-head">
        <div>
          <div class="eyebrow">FULFILLMENT</div>
          <h1>订单履约</h1>
          <p>同步订单、代发审批与物流回填。所有外部写都需要人工确认。</p>
        </div>
        <div class="page-actions">
          <span class="safety-chip"><span class="dot"></span>真实写保护中</span>
          <button class="btn secondary" type="button" id="btn-sync-orders" ${s.user&&s.user.role==="viewer"?'disabled title="只读账号不能执行同步写操作"':""}>只读同步</button>
        </div>
      </div>
      <div class="orders-safety">代发与物流回填须先过对应审批；本后台不自动下单、不付款、不发货、不退款。发货与退款请在 TikTok Shop 卖家中心由人操作。</div>
      <div class="orders-kpis">
        ${o("临期",e.due_soon??0,"接近履约时限",Number(e.due_soon||0)>0?"is-warn":"")}
        ${o("超时",e.overdue??0,"需优先处理",Number(e.overdue||0)>0?"is-bad":"")}
        ${o("窗口内",e.ok??0,"仍在时限内")}
        ${o("已同步",t.length,s.orderPlatform?`当前：${s.orderPlatform}`:"当前店铺 · 全部平台")}
      </div>
      <div class="orders-workspace">
        <section class="panel order-queue" aria-label="已同步订单">
          <div class="queue-head">
            <div><div class="eyebrow">QUEUE</div><h2>已同步订单</h2><p class="muted">${t.length} 个订单</p></div>
            <div class="queue-tools">
              <input id="order-search" type="search" placeholder="搜索订单号或状态" aria-label="搜索订单" />
              <select id="order-platform-filter" aria-label="按平台筛选"><option value="">全部平台</option>${r.map(i=>`<option value="${a(i)}"${s.orderPlatform===i?" selected":""}>${a(i)}</option>`).join("")}</select>
              <select id="order-status-filter" aria-label="按状态筛选"><option value="">全部状态</option>${n.map(i=>`<option value="${a(i)}">${a(i)}</option>`).join("")}</select>
              <label class="btn secondary" for="shopee-csv" title="Shopee 只读 CSV 导入；不调用平台 API">导入 Shopee CSV</label>
              <input id="shopee-csv" type="file" accept=".csv,text/csv" hidden />
            </div>
          </div>
          <div class="order-list" id="order-list">
            ${d||k("暂无订单","点击「只读同步」从平台适配器拉取（mock/offline 可能为空）。")}
          </div>
        </section>
        <div id="order-workbench" class="order-detail"></div>
      </div>
    </div>
  `}const zt=[{v:"pass",label:"通过"},{v:"fail",label:"不通过"}];function Qt(t){const e=t.spotCheck||[],n=t.spotCheckCriteria||[];if(!e.length)return k("暂无可抽检样本","先用 demo 包或生成 listing 草稿，样本会出现在这里。");const r=n.map(d=>`<th>${a(d.label)}</th>`).join(""),o=e.map((d,i)=>{const c=n.map(f=>`<td class="sc-cell">${zt.map(m=>`<label class="sc-opt"><input type="radio" name="sc-${i}-${a(f.id)}" value="${m.v}" />${m.label}</label>`).join("")}</td>`).join("");return`<tr data-offer="${a(d.offer_id)}" data-title="${a(d.title)}" data-idx="${i}">
        <td>${i+1}</td><td>${a(d.offer_id)}</td><td>${a(d.title)}</td>${c}</tr>`}).join("");return`
    <div class="muted" style="margin-bottom:8px">
      门槛：通过率 ≥ ${(t.spotCheckThreshold*100).toFixed(0)}%（DESIGN §9 Done 条件 2）。
      未打分的格子不计入通过率。
    </div>
    <table class="sc-table"><thead><tr><th>#</th><th>offer_id</th><th>输入标题</th>${r}</tr></thead>
    <tbody>${o}</tbody></table>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
      <button class="btn" type="button" id="btn-spot-score">计算通过率</button>
      <button class="btn secondary" type="button" id="btn-spot-export">导出 md</button>
    </div>
    <div id="spot-result" style="margin-top:12px"></div>
  `}async function yt(){const t=await v("/api/admin/spot-check");s.spotCheck=t.items||[],s.spotCheckCriteria=t.criteria||[],s.spotCheckThreshold=t.threshold??.8,s.spotCheckReport=""}function Vt(){const t=[];return document.querySelectorAll("#spot-body tr[data-idx]").forEach(e=>{const n=e.getAttribute("data-idx"),r={};(s.spotCheckCriteria||[]).forEach(o=>{const d=document.querySelector(`input[name="sc-${n}-${o.id}"]:checked`);r[o.id]=d?d.value:"pending"}),t.push({offer_id:e.getAttribute("data-offer")||"",title:e.getAttribute("data-title")||"",scores:r})}),t}async function Kt(){const t=Vt(),e=await v("/api/admin/spot-check/report",{method:"POST",body:JSON.stringify({rows:t})});s.spotCheckReport=e.report_md||"";const n=e.scores||{},r=n.pass_rate===null||n.pass_rate===void 0?"无数据（尚未打分）":`${(n.pass_rate*100).toFixed(1)}%`,o=n.meets_threshold===null||n.meets_threshold===void 0?"":n.meets_threshold?'<span class="tag approved">达标</span>':'<span class="tag failed">未达标</span>',d=document.getElementById("spot-result");d&&(d.innerHTML=`
      <div><strong>通过率：${a(r)}</strong> ${o}</div>
      <div class="muted">已打分 ${n.scored??0} 格（通过 ${n.passed??0} / 不通过 ${n.failed??0}），未打分 ${n.pending??0} 格</div>
      <div class="muted">未打分的格子不计入通过率。</div>`)}function Xt(){return`
    <div class="page-head">
      <div><h1>人工抽检</h1><p>四判据逐条人工判定；自动计算通过率并可导出报告。</p></div>
      <button class="btn secondary" type="button" id="btn-spot-reload">刷新样本</button>
    </div>
    <section class="panel"><div class="panel-head"><h2>样本</h2></div>
    <div class="panel-body" id="spot-body">${Qt(s)}</div></section>
  `}function Yt(){const t=(s.drafts||[]).filter(e=>e.kind==="listing"||e.kind==="video_script");return`
    <div class="page-head">
      <div><h1>选品</h1><p>草稿生成与选品摘要（接现有 drafts / jobs API）。</p></div>
      <div style="display:flex;gap:8px">
        <button class="btn secondary" type="button" id="btn-gen-listing">生成 listing 草稿</button>
        <button class="btn secondary" type="button" id="btn-run-research">跑选品摘要</button>
      </div>
    </div>
    <section class="panel"><div class="panel-head"><h2>最近草稿</h2></div><div class="panel-body">
      ${t.length?`<table><thead><tr><th>标题</th><th>类型</th><th>状态</th><th>创建</th></tr></thead><tbody>
          ${t.slice(0,30).map(e=>`<tr>
              <td>${a(e.title)}</td><td>${a(e.kind)}</td>
              <td><span class="tag ${a(e.status)}">${a(e.status)}</span></td>
              <td>${a(e.created_at)}</td></tr>`).join("")}
          </tbody></table>`:k("暂无选品草稿","可生成 mock listing，或跑本地选品摘要报表。")}
    </div></section>
  `}const j="缺数据";function nt(t){if(t==null||t==="")return"—";if(t===j)return j;const e=Number(t);return Number.isFinite(e)?(e/100).toFixed(2):a(t)}function ht(t){return t==null||t===""?"—":a(t)}function D(t){return t==null||t===""||t===j}function G(t){if(D(t))return j;const e=Number(t);return Number.isFinite(e)?(e/100).toFixed(2):j}function Zt(...t){if(t.some(n=>D(n)))return j;let e=0;for(const n of t){const r=Number(n);if(!Number.isFinite(r))return j;e+=r}return e}function T(t){return D(t)||!Number.isFinite(Number(t))?`<span class="missing">${j}</span>`:a(G(t))}function at(t){return D(t)?`<span class="missing">${j}</span>`:a(t)}function wt(t){return D(t)||!Number.isFinite(Number(t))?`<span class="missing">${j}</span>`:`${a(t)}%`}function _t(t){return t&&t.data_status==="ok"?"完整":`<span class="missing">${j}</span>`}function te(){const t=s.reports||[],e=s.profit||{},n=e.totals||{},r=e.alerts||[],o=s.adSpendRows||[],d=s.user&&s.user.role!=="viewer",i=s.profitShop||"default";return`
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
      <article class="card"><h3>广告费 (THB)</h3><div class="metric">${a(nt(n.ad_spend??e.ad_spend??0))}</div><p class="hint">店铺 ${a(i)} · 记账</p></article>
      <article class="card"><h3>ROAS</h3><div class="metric">${ht(n.ROAS??e.ROAS)}</div><p class="hint">销售额 / 广告费</p></article>
      <article class="card"><h3>ACOS</h3><div class="metric">${ht(n.ACOS??e.ACOS)}</div><p class="hint">广告费 / 销售额</p></article>
      <article class="card"><h3>扣广告后利润 (THB)</h3><div class="metric">${a(nt(n.profit_after_ads??e.profit_after_ads))}</div><p class="hint">已扣记账广告费</p></article>
    </div>
    <div id="ad-roas-alert">
      ${r.length?r.map(c=>`<div class="banner warn">${a(c.message||"")} 保本 ROAS ${a(c.break_even_roas??"—")} · 最高 CPA ${a(c.max_cpa_minor==null?"—":nt(c.max_cpa_minor))} THB。只提醒，不自动操作。</div>`).join(""):'<div class="banner">没有低于保本的提醒。本页不修改广告预算。</div>'}
    </div>
    <section class="panel" style="margin-bottom:12px">
      <div class="panel-head"><h2>广告费明细</h2><span class="muted">${o.length} 行 · 只读拉取为 mock</span></div>
      <div class="panel-body" id="ad-spend-table">
        ${o.length?`<table><thead><tr><th>日期</th><th>店铺</th><th>SKU</th><th>渠道</th><th>花费 THB</th><th>展示</th><th>点击</th><th>转化</th><th>来源</th></tr></thead><tbody>
            ${o.map(c=>`<tr>
                <td>${a(c.spend_date)}</td>
                <td>${a(c.shop_id)}</td>
                <td>${a(c.sku||c.product_id||"—")}</td>
                <td>${a(c.channel)}</td>
                <td>${a(c.amount_thb)}</td>
                <td>${a(c.impressions??"—")}</td>
                <td>${a(c.clicks??"—")}</td>
                <td>${a(c.conversions??"—")}</td>
                <td>${a(c.source)}</td>
              </tr>`).join("")}
            </tbody></table>`:k("还没有广告费","operator 可手工记账，或从 mock 拉取。不会调用 TikTok Ads。")}
        <form id="ad-spend-form" class="grid-3" style="margin-top:16px">
          <div class="field"><label>日期</label><input name="spend_date" type="date" required ${d?"":"disabled"} /></div>
          <div class="field"><label>花费 (THB)</label><input name="amount_thb" inputmode="decimal" placeholder="12.50" required ${d?"":"disabled"} /></div>
          <div class="field"><label>渠道</label><input name="channel" value="tiktok_ads" ${d?"":"disabled"} /></div>
          <div class="field"><label>SKU</label><input name="sku" placeholder="可空，表示整店" ${d?"":"disabled"} /></div>
          <div class="field"><label>展示 / 点击 / 转化</label><input name="metrics" placeholder="1000,25,1 可空" ${d?"":"disabled"} /></div>
          <div class="field" style="display:flex;align-items:flex-end;gap:8px">
            <button class="btn" type="submit" ${d?"":"disabled"}>记账</button>
            <button class="btn secondary" type="button" id="btn-ads-pull" ${d?"":"disabled"}>从 mock 拉取</button>
          </div>
        </form>
        <p class="muted">${d?"记账只写本地表。mock 拉取不外呼、不改预算。":"当前是只读账号，不能记账。"}</p>
        <p id="ad-spend-form-error" class="error"></p>
      </div>
    </section>
    <section class="panel"><div class="panel-head"><h2>本地报表</h2></div><div class="panel-body">
      ${t.length?t.map(c=>`<article style="margin-bottom:16px">
                <h3 style="margin:0 0 6px">${a(c.kind)} · ${a(c.created_at)}</h3>
                <pre class="mono">${a(c.body_md||"")}</pre>
              </article>`).join(""):k("暂无报表","跑一次选品摘要或日报后会出现在这里。")}
    </div></section>
  `}function rt(t,e){if(!t.length)return k("暂无汇总","选定范围没有可汇总的订单。");const n=`<th>${a(e)}</th><th>订单数</th><th>完整</th><th>缺数据</th><th>收入(THB)</th><th>佣金</th><th>支付费</th><th>广告占位</th><th>广告费</th><th>ROAS</th><th>ACOS</th><th>扣广告后利润</th><th>采购成本</th><th>运费</th><th>毛利</th><th>毛利率</th><th>状态</th>`,r=t.map(o=>`<tr>
        <td>${a(o.key||o.shop_id||"")}</td>
        <td>${a(o.orders??0)}</td>
        <td>${a(o.complete_orders??0)}</td>
        <td>${a(o.incomplete_orders??0)}</td>
        <td>${T(o.revenue)}</td>
        <td>${T(o.platform_commission)}</td>
        <td>${T(o.payment_fee)}</td>
        <td>${T(o.ads)}</td>
        <td>${T(o.ad_spend)}</td>
        <td>${at(o.ROAS)}</td>
        <td>${at(o.ACOS)}</td>
        <td>${T(o.profit_after_ads)}</td>
        <td>${T(o.purchase_cost)}</td>
        <td>${T(o.shipping)}</td>
        <td>${T(o.gross_profit)}</td>
        <td>${wt(o.gross_margin_pct)}</td>
        <td>${_t(o)}</td>
      </tr>`).join("");return`<div class="table-wrap"><table><thead><tr>${n}</tr></thead><tbody>${r}</tbody></table></div>`}function ee(t){if(!t.length)return k("暂无订单","选定店铺与日期范围内没有已同步订单。");const e="<th>订单</th><th>店铺</th><th>日期</th><th>收入(THB)</th><th>佣金</th><th>支付费</th><th>广告占位</th><th>广告费</th><th>ROAS</th><th>ACOS</th><th>扣广告后利润</th><th>采购成本</th><th>运费</th><th>毛利</th><th>毛利率</th><th>状态</th>",n=t.map(r=>`<tr>
        <td>${a(r.order_id)}</td>
        <td>${a(r.shop_id)}</td>
        <td>${a(r.date||"")}</td>
        <td>${T(r.revenue)}</td>
        <td>${T(r.platform_commission)}</td>
        <td>${T(r.payment_fee)}</td>
        <td>${T(r.ads)}</td>
        <td>${T(r.ad_spend)}</td>
        <td>${at(r.ROAS)}</td>
        <td>${at(r.ACOS)}</td>
        <td>${T(r.profit_after_ads)}</td>
        <td>${T(r.purchase_cost)}</td>
        <td>${T(r.shipping)}</td>
        <td>${T(r.gross_profit)}</td>
        <td>${wt(r.gross_margin_pct)}</td>
        <td>${_t(r)}</td>
      </tr>`).join("");return`<div class="table-wrap"><table><thead><tr>${e}</tr></thead><tbody>${n}</tbody></table></div>`}function ae(){const t=s.profit||{},e=t.totals||{},n=t.alerts||[],r=s.profitShops||[],o=['<option value="">全部店铺</option>'].concat(r.map(C=>`<option value="${a(C.shop_id)}"${s.profitShop===C.shop_id?" selected":""}>${a(C.shop_id)}</option>`)).join(""),d=Number(t.ad_spend_row_count||0),i=t.rates||{},c=i.placeholder!==!1||d===0,f=e.revenue??t.revenue,m=e.purchase_cost,p=Zt(e.platform_commission,e.payment_fee),_=e.ad_spend??t.ad_spend,S=e.ROAS??t.ROAS,I=e.ACOS??t.ACOS,$=e.profit_after_ads??t.profit_after_ads,y=e.gross_margin_pct,L=n.length>0,R=L?"低于保本：只提醒（alert_only），不自动改价、不建审批、不调预算":"只读利润控制台 · 数字来自已同步订单与广告费记账，缺数据不编造",x=[["收入 (THB)",G(f),"已同步订单销售额"],["采购成本 COGS (THB)",G(m),"代发采购；缺则标缺数据"],["平台费 (THB)",G(p),"佣金 + 支付费"],["广告费 (THB)",G(_),d?"记账 ad_spend":"无记账不编造"],["ROAS",D(S)?j:String(S),"销售额 / 记账广告费"],["ACOS",D(I)?j:String(I),"广告费 / 销售额"],["扣广告后利润 (THB)",G($),"profit_after_ads"],["毛利率",D(y)?j:`${y}%`,"gross_margin_pct"]],P=[`佣金 ${i.commission_pct??"?"}%`,`支付 ${i.payment_fee_pct??"?"}%`,`广告 ${i.ads_pct??"?"}%`,`FX ${i.fx_rate??"?"}（${i.fx_source||"env"}）`],H=c?`占位费率（PRICING_*，非平台结算实收）：${P.join(" · ")}。广告记账为空或走占位路径时，扣广告后利润用占位 ads_pct，不编造广告费。${i.note||t.note||""} 低于保本只提醒（alert_only）。本页无付款/退款/提现按钮，也不改价、不建审批、不调预算。`:`费率：${P.join(" · ")}。${i.note||t.note||""} 低于保本只提醒（alert_only）。本页无付款/退款/提现按钮。`,W=L?n.map(C=>`<div class="banner warn">${a(C.message||"")} 保本 ROAS ${a(C.break_even_roas??"—")} · 最高 CPA ${a(C.max_cpa_minor==null?"—":G(C.max_cpa_minor))} THB。只提醒（alert_only），不自动改价、不建审批、不调预算。</div>`).join(""):"";return`
    <div class="banner ${L?"warn":""}">${M.check}<span>${a(R)}</span></div>
    <div class="page-head">
      <div>
        <h1>利润</h1>
        <p>正式控制台利润页。复用 <code>GET /api/workflow/profit</code>。viewer 只读。不是 /workflow 自测面板。</p>
      </div>
      <form class="filters" id="profit-console-form">
        <div class="field"><label for="profit-shop">店铺</label>
          <select id="profit-shop" name="shop_id">${o}</select>
        </div>
        <div class="field"><label for="profit-from">从</label>
          <input id="profit-from" name="from" type="date" value="${a(s.profitFrom)}" />
        </div>
        <div class="field"><label for="profit-to">到</label>
          <input id="profit-to" name="to" type="date" value="${a(s.profitTo)}" />
        </div>
        <button class="btn secondary" type="submit" id="btn-profit-query">查询</button>
      </form>
    </div>
    <div id="profit-summary-cards" class="grid-4" style="margin-bottom:12px">
      ${x.map(([C,J,z])=>`
        <article class="card">
          <h3>${a(C)}</h3>
          <div class="metric">${J===j?`<span class="missing">${j}</span>`:a(J)}</div>
          <p class="hint">${a(z)}</p>
        </article>`).join("")}
    </div>
    <div id="profit-alerts">${W}</div>
    <p class="muted" id="profit-summary">订单 ${a(t.order_count??0)} · 缺数据 ${a(t.incomplete_order_count??0)} · 单位 THB（API 为 satang/100） · 只读</p>
    <section class="panel">
      <div class="panel-head"><h2>合计</h2><span class="muted">与 API totals 同字段</span></div>
      <div class="panel-body" id="profit-totals">${rt([Object.assign({},e,{key:"合计"})],"范围")}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>按日</h2></div>
      <div class="panel-body" id="profit-by-day">${rt(t.by_day||[],"日期")}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>按周</h2></div>
      <div class="panel-body" id="profit-by-week">${rt(t.by_week||[],"ISO 周")}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>订单明细</h2><span class="muted">${a(t.order_count??0)} 行</span></div>
      <div class="panel-body" id="profit-orders">${ee(t.orders||[])}</div>
    </section>
    <footer class="profit-foot" id="profit-rates-note">${a(H)}</footer>
  `}function se(){const t=s.runs||[],e=s.selectedRun,n=s.user&&s.user.role==="owner",r=s.user&&s.user.role!=="viewer",o=t.length?`<table><thead><tr><th>状态</th><th>当前步</th><th>流程</th><th>创建</th></tr></thead><tbody>
      ${t.map(i=>{const c=e&&e.id===i.id?' style="background:var(--bg)"':"";return`<tr class="clickable" data-run="${a(i.id)}"${c}>
            <td><span class="tag ${a(i.status)}">${a(i.status)}</span></td>
            <td>${a(i.current_step||"—")}</td>
            <td>${a(i.flow_id)}</td>
            <td>${a(i.created_at)}</td>
          </tr>`}).join("")}
      </tbody></table>`:k("暂无运行","点「一键 mock 演示」把 Scout→定价→Stylist→QA→审批→上架→订单/代发/物流→客服写入本库。");let d=k("选择运行","左侧点一行查看时间线。");if(e){const i=e.timeline||e.steps||[],c=e.status==="awaiting_approval";d=`
      <div class="panel-head">
        <h2>${a(e.status)} · ${a(e.current_step||"—")}</h2>
        <span class="muted">${a(e.id)}</span>
      </div>
      <div class="panel-body">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
          <button class="btn secondary" type="button" id="btn-run-retry" ${r?"":"disabled"}>重试当前步</button>
          <button class="btn danger" type="button" id="btn-run-cancel" ${r?"":"disabled"}>取消</button>
          ${c?`<button class="btn" type="button" id="btn-run-approve" ${n?"":"disabled"}>通过审批</button>
                 <button class="btn danger" type="button" id="btn-run-reject" ${n?"":"disabled"}>打回</button>
                 ${n?"":'<span class="muted">仅 owner 可审批</span>'}`:""}
        </div>
        ${c?`<div class="field"><label>审批说明（打回必填）</label>
               <textarea id="run-decision-note" placeholder="通过可写简短说明；打回必须写原因"></textarea></div>`:""}
        <h3>时间线</h3>
        ${i.length?i.map(f=>{let m=f.rationale||"";if(!m&&Array.isArray(f.ai_trace)){const p=f.ai_trace.find(_=>_&&_.rationale);m=p&&p.rationale||""}else!m&&f.ai_trace&&f.ai_trace.rationale&&(m=f.ai_trace.rationale);return`<article class="card" style="margin-bottom:10px">
                    <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
                      <strong>${a(f.label||f.step_id)}</strong>
                      <span class="tag ${a(f.status)}">${a(f.status)}</span>
                    </div>
                    <p class="muted" style="margin:6px 0">尝试 ${a(f.attempt)} · 成本 $${a(f.cost_usd??"0")} · ${a(f.started_at||f.created_at||"")}</p>
                    <p style="margin:0 0 8px"><strong>AI 理由：</strong>${a(m||"—")}</p>
                    <div class="diff">
                      <div><div class="muted" style="margin-bottom:4px">输入</div><pre>${a(JSON.stringify(f.input||{},null,2))}</pre></div>
                      <div><div class="muted" style="margin-bottom:4px">输出</div><pre>${a(JSON.stringify(f.output||{},null,2))}</pre></div>
                    </div>
                  </article>`}).join(""):k("无步骤","启动后会出现 Scout / Stylist / 审批等步骤。")}
      </div>
    `}return`
    <div class="page-head">
      <div>
        <h1>AI 工作流</h1>
        <p>编排器 run 列表、时间线与审批挂起。不打开真实发货/退款/TikTok 写入。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" id="btn-demo-orch" ${r?"":"disabled"} title="${r?"强制 WORKFLOW_PLATFORM=mock，不外呼，TTS_WRITE 保持关闭":"只读账号不能触发演示"}">一键 mock 演示</button>
        <button class="btn secondary" type="button" id="btn-start-run" ${r?"":"disabled"}>启动流水线</button>
      </div>
    </div>
    <div class="split">
      <section class="panel"><div class="panel-head"><h2>运行列表</h2></div><div class="panel-body">${o}</div></section>
      <section class="panel" id="run-detail">${d}</section>
    </div>
  `}function ne(t){return t==="bad"?"bad":t==="warn"?"warn":t==="idle"?"idle":""}function re(t){if(!t||t.status==="missing")return"LLM 预算 · 缺数据";const e=Number(t.prompt_tokens||0)+Number(t.completion_tokens||0),n=t.cost_usd==null?"—":Number(t.cost_usd).toFixed(4);return`LLM ${e} tokens · $${n}`}function oe(){const t=s.agentStats||{agents:[],runs:[],budget:{},task_logs:{}},e=t.agents||[],n=t.runs||[],r=s.agentRun,o=e.some($=>$.health==="bad"),d=e.some($=>$.health==="warn"),i=!o&&!d,c=o?"有代理失败 · 数字来自已落库的编排记录":d?"有代理挂起、待审批或 QA 打回":"只读统计 · 数字来自编排 run、步骤、审批表与 LLM 用量",f=t.budget||{},m=f.limit_status==="missing"?"日预算上限 · 缺数据":`日预算上限 $${Number(f.daily_limit_usd).toFixed(2)}`,p=Number(f.spent_in_range_usd||0).toFixed(4),_=e.length?e.map($=>{const y=$.counts||{},L=$.recent_artifacts||[],R=L.length?`<ul class="artifact-list">${L.map(x=>`<li><button type="button" class="linkish" data-agent-run="${a(x.run_id)}" title="${a(x.href||"")}">${a(x.label)}</button></li>`).join("")}</ul>`:'<p class="hint">最近产物 · 暂无</p>';return`<section class="card agent-card" data-agent="${a($.id)}">
            <div class="health-top">
              <span class="health-dot ${ne($.health)}"></span>
              <span class="health-name">${a($.name)}</span>
              <span class="tag-mono">${a($.role||$.id)}</span>
              <span class="tag-mono agent-kind">${a($.kind||"")}</span>
            </div>
            <div class="health-desc">${a($.description||"")}</div>
            <div class="agent-metrics">
              <div><strong>${a(y.runs??0)}</strong><span>运行</span></div>
              <div><strong>${a(y.succeeded??0)}</strong><span>成功</span></div>
              <div><strong>${a(y.qa_rejected??0)}</strong><span title="QA 打回">打回</span></div>
              <div><strong>${a(y.awaiting_approval??0)}</strong><span title="等待人工审批">待审</span></div>
              <div><strong>${a(y.failed??0)}</strong><span>失败</span></div>
            </div>
            <p class="hint">${a(re($.llm))}${y.suspended?` · 挂起 ${a(y.suspended)}`:""}</p>
            ${R}
          </section>`}).join(""):k("暂无代理定义","流水线步骤未加载。"),S=n.length?`<table><thead><tr><th>Run</th><th>当前阶段</th><th>挂起原因</th><th>更新</th></tr></thead><tbody>
      ${n.map($=>{const y=r&&r.run&&r.run.id===$.id?' style="background:var(--bg)"':"";return`<tr class="clickable" data-agent-run="${a($.id)}"${y}>
            <td class="mono" title="${a($.id)}">${a(String($.id||"").slice(0,8))}</td>
            <td>${a($.current_step_label||$.current_step||"—")}</td>
            <td><span class="tag ${a($.suspend_code||$.status)}">${a($.suspend_reason||"—")}</span></td>
            <td>${a($.updated_at||$.created_at||"")}</td>
          </tr>`}).join("")}
      </tbody></table>`:k("暂无编排 run","在「AI 工作流」启动一条流水线，或让 demo-orch --db 指向本服务的工作台库之后，这里出现真实计数。");let I=k("选择运行","左侧点一行查看步骤时间线。本页只读。");if(r&&r.run){const $=r.steps||[];I=`
      <div class="panel-head">
        <h2>${a(r.run.suspend_reason||r.run.status)} · ${a(r.run.current_step_label||"—")}</h2>
        <span class="muted mono">${a(r.run.id)}</span>
      </div>
      <div class="panel-body">
        <p class="muted" style="margin-top:0">状态 ${a(r.run.status)} · 店 ${a(r.run.shop_id||"—")} · 只读时间线，不含发货 / 退款 / 付款。</p>
        ${$.length?`<ol class="timeline">${$.map(y=>{const L=y.status==="failed"?"bad":y.status==="awaiting_approval"||y.status==="suspended"?"warn":y.status==="succeeded"?"":"idle",R=y.finished_at||y.started_at||"",x=y.error&&y.error.message?y.error.message:"";return`<li>
                    <span class="health-dot ${L}" style="margin-top:6px"></span>
                    <div>
                      <strong>${a(y.label||y.step_id)}</strong>
                      <span class="tag ${a(y.status)}">${a(y.status)}</span>
                      <div class="muted">${a(R)}${y.rewrite_round?` · 重写 ${a(y.rewrite_round)}`:""}</div>
                      ${x?`<div class="error">${a(x)}</div>`:""}
                    </div>
                  </li>`}).join("")}</ol>`:k("无步骤","这条 run 还没有步骤记录。")}
      </div>`}return`
    <div class="banner ${i?"":"warn"}">${M.check}<span>${a(c)}</span></div>
    <div class="page-head">
      <div>
        <h1>AI 代理</h1>
        <p>选品 → 定价 → 文案图片 → 质检 → 审批 → 上架 → 订单 → 代发 → 物流 → 客服。只读，不打开真写。</p>
      </div>
      <form class="filters" id="agent-stats-form">
        <div class="field"><label for="agent-from">从</label><input id="agent-from" name="from" type="date" value="${a(s.agentFrom)}" /></div>
        <div class="field"><label for="agent-to">到</label><input id="agent-to" name="to" type="date" value="${a(s.agentTo)}" /></div>
        <button class="btn secondary" type="submit" id="btn-agent-stats">查询</button>
      </form>
    </div>
    <p class="muted" style="margin:-6px 0 12px">${a(m)} · 区间内已记账 $${a(p)} · 任务轨迹 ${a(t.task_logs&&t.task_logs.events||0)} 条${t.task_logs&&t.task_logs.business_tasks&&t.task_logs.business_tasks.label?` · 业务库任务日志 ${a(t.task_logs.business_tasks.label)}`:""}</p>
    <div class="grid-agents">${_}</div>
    <div class="split">
      <section class="panel"><div class="panel-head"><h2>编排 run</h2></div><div class="panel-body">${S}</div></section>
      <section class="panel" id="agent-run-detail">${I}</section>
    </div>
  `}const kt=[["logistics_query","物流查询 · 白名单"],["tracking_status","物流状态 · 白名单"],["sla_reminder","时效提醒 · 白名单"],["refund","退款 · 必审"],["compensation","赔偿 · 必审"],["price_match","差价 · 必审"],["complaint","投诉 · 必审"]];function mt(t){const e=kt.find(n=>n[0]===t);return e?e[1].split(" · ")[0]:t||"—"}function ie(){const t=s.user&&s.user.role!=="viewer",e=s.csInbox||[],n=s.csDrafts||[],r=n.filter(i=>i.status==="pending_approval"&&i.approval_id),o=e.length?`<div class="table-wrap"><table><thead><tr><th>会话</th><th>类目</th><th>正文</th><th>时间</th><th></th></tr></thead><tbody>
      ${e.map(i=>`<tr>
          <td>${a(i.conversation_id)}</td>
          <td><span class="health-dot"></span> ${a(mt(i.category))}</td>
          <td>${a(i.body)}</td>
          <td>${a(i.created_at||"")}</td>
          <td class="cs-actions"><button class="btn secondary pill" type="button" data-cs-draft-msg="${a(i.id)}" ${t?"":"disabled"}>生成草稿</button></td>
        </tr>`).join("")}
      </tbody></table></div>`:k("入站为空","记一条本地 mock 消息，或点刷新。不会去拉 TikTok 客服。"),d=i=>i.length?`<div class="table-wrap"><table><thead><tr><th>类目</th><th>状态</th><th>话术</th><th>动作</th></tr></thead><tbody>
        ${i.map(c=>{const f=c.sent_stub||c.status==="sent_stub",m=c.whitelist?'<span class="tag approved">白名单免审</span>':'<span class="tag pending">必审</span>',p=[];f?p.push('<span class="tag approved">sent_stub</span>','<span class="tag approved">dry_run</span>'):c.whitelist&&p.push(`<button class="btn pill ok" type="button" data-cs-send="${a(c.id)}" ${t?"":"disabled"}>stub 发送</button>`),c.approval_id&&!f&&p.push(`<a class="btn secondary pill" href="/admin/approvals?id=${encodeURIComponent(c.approval_id)}" data-cs-approval="${a(c.approval_id)}">查看审批</a>`);const _=p.length?p.join(""):"—";return`<tr>
              <td>${a(mt(c.category))} ${m}</td>
              <td><span class="tag ${a(c.status)}">${a(c.status)}</span></td>
              <td>${a(c.body_th||c.body_en||"")}</td>
              <td class="cs-actions">${_}</td>
            </tr>`}).join("")}
        </tbody></table></div>`:"";return`
    <div class="banner">${M.check}<span>客服白名单在本地 stub。不外呼 TikTok 客服。</span></div>
    <div class="page-head">
      <div>
        <h1>客服</h1>
        <p>入站 inbox、话术草稿、白名单 stub 发送。非白名单建 auto_reply 审批。</p>
      </div>
      <button class="btn secondary pill" type="button" id="btn-refresh-cs">刷新</button>
    </div>
    ${s.csNote?`<div class="banner">${a(s.csNote)}</div>`:""}
    ${s.csError?`<p class="error" id="cs-form-error">${a(s.csError)}</p>`:'<p class="error" id="cs-form-error"></p>'}
    <div class="split cs-layout">
      <section class="panel">
        <div class="panel-head"><h2>入站 inbox</h2><span class="muted">${e.length} 条 · 本地 mock</span></div>
        <div class="panel-body">${o}</div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>草稿</h2><span class="muted">待审 ${r.length}</span></div>
        <div class="panel-body">
          <h3 style="margin:0 0 8px">待审草稿</h3>
          ${r.length?d(r):k("没有待审草稿","非白名单类目生成草稿后会出现在这里，并可跳到审批详情。")}
          <h3 style="margin:16px 0 8px">全部草稿</h3>
          ${n.length?d(n):k("暂无客服草稿","从入站点「生成草稿」。")}
        </div>
      </section>
    </div>
    <section class="panel">
      <div class="panel-head"><h2>记入本地入站</h2><span class="muted">不外呼平台</span></div>
      <div class="panel-body">
        <form id="cs-inbox-form" class="cs-form">
          <div class="field"><label>会话 ID</label><input name="conversation_id" required placeholder="conv-1" ${t?"":"disabled"} /></div>
          <div class="field"><label>类目</label>
            <select name="category" ${t?"":"disabled"}>
              ${kt.map(([i,c])=>`<option value="${a(i)}">${a(c)}</option>`).join("")}
            </select>
          </div>
          <div class="field"><label>语言</label>
            <select name="lang" ${t?"":"disabled"}>
              <option value="th">th</option>
              <option value="en">en</option>
              <option value="zh">zh</option>
            </select>
          </div>
          <div class="field"><label>订单号（可空）</label><input name="order_id" ${t?"":"disabled"} /></div>
          <div class="field cs-form-wide"><label>买家消息</label><textarea name="body" required placeholder="พัสดุถึงไหนแล้วคะ" ${t?"":"disabled"}></textarea></div>
          <div class="field cs-form-wide">
            <button class="btn pill" type="submit" ${t?"":"disabled"}>记入本地入站</button>
            <p class="muted">${t?"只写本地 inbox，不外呼平台。":"当前是只读账号，不能写入站或发送。"}</p>
          </div>
        </form>
      </div>
    </section>
    <footer class="page-foot" id="cs-constraints">不外呼 TikTok 客服；发货/退款仍 403；不打开 TTS_WRITE_*；涉钱类（refund/compensation/price_match/complaint）必审。</footer>
  `}function de(t){return t==="pending"?"待你确认":t==="approved"?"已标记通过":t==="rejected"?"已标记拒绝":t||"—"}function ce(){const t=s.user&&s.user.role!=="viewer",e=s.outreachDrafts||[],n=e.filter(o=>o.status==="pending"),r=e.length?`<div class="outreach-grid">${e.map(o=>{const d=o.payload||{},i=d.message||{},c=Array.isArray(i.terms)?i.terms:[],f=Array.isArray(i.followups)?i.followups:[],m=c.length?`<ul class="outreach-terms">${c.map(I=>`<li><span>${a(I.label||I.key||"")}</span> ${a(I.value||"")}</li>`).join("")}</ul>`:"",p=f.length?f.map(I=>`<p class="outreach-copy"><strong>第 ${a(I.day)} 天</strong> ${a(I.body_th||"")}<br><span class="muted">${a(I.body_zh||"")}</span></p>`).join(""):"",_=o.status==="pending"?`<div class="outreach-actions">
                  <button class="btn pill ok" type="button" data-outreach-approve="${a(o.id)}" ${t?"":"disabled"}>标记通过</button>
                  <button class="btn secondary pill" type="button" data-outreach-reject="${a(o.id)}" ${t?"":"disabled"}>标记拒绝</button>
                </div>`:'<p class="muted">只改了状态。没有发送。</p>',S=d.run_id?`<a class="btn secondary pill" href="/admin/workflows?run=${encodeURIComponent(d.run_id)}">工作流</a>`:"";return`<article class="outreach-card panel">
            <div class="outreach-meta">
              <span class="tag">${a(d.creator_type||o.kind||"达人")}</span>
              <span class="tag ${a(o.status)}">${a(de(o.status))}</span>
              ${S}
            </div>
            <h3>${a(o.title||d.creator_name||"达人")}</h3>
            <p class="outreach-copy">${a(i.dm_th||o.body_th||"")}</p>
            <p class="muted outreach-copy">${a(i.dm_zh||"")}</p>
            <p class="muted outreach-copy">${a(i.dm_en||o.body_en||"")}</p>
            <p class="outreach-copy"><strong>样品</strong> ${a(i.sample_th||"")}<br><span class="muted">${a(i.sample_zh||"")}</span></p>
            ${m}
            ${p}
            <p class="muted">${a(i.closing_note||"")}</p>
            ${_}
          </article>`}).join("")}</div>`:k("还没有达人建联草稿","跑一条 AI 工作流或一键 mock 演示后，通过 QA 的商品会在这里出现。草稿不会自动发送。");return`
    <div class="banner">${M.check}<span>达人建联只产草稿。不会自动发送，也不会外呼 TikTok。</span></div>
    <div class="page-head">
      <div>
        <h1>达人建联</h1>
        <p>泰语邀约草稿待你确认。通过或拒绝只改状态，不发送私信、不寄样、不付款。</p>
      </div>
      <button class="btn secondary pill" type="button" id="btn-refresh-outreach">刷新</button>
    </div>
    ${s.outreachNote?`<div class="banner">${a(s.outreachNote)}</div>`:""}
    ${s.outreachError?`<p class="error" id="outreach-form-error">${a(s.outreachError)}</p>`:'<p class="error" id="outreach-form-error"></p>'}
    <section class="panel">
      <div class="panel-head">
        <h2>草稿</h2>
        <span class="muted">待你确认 ${n.length} / ${e.length}</span>
      </div>
      <div class="panel-body">
        <div class="field">
          <label for="outreach-note">拒绝原因</label>
          <input id="outreach-note" placeholder="标记拒绝时必填" ${t?"":"disabled"} />
        </div>
        <p class="muted">${t?"通过不需要原因。两种决定都不发送。":"当前是只读账号，不能改状态。"}</p>
      </div>
    </section>
    ${r}
    <footer class="page-foot" id="outreach-constraints">不自动发送；不外呼 TikTok；不打开 TTS_WRITE_*；批准只改状态，不发送私信、不寄样、不付款。</footer>
  `}function le(t){return!t||!t.groups?"":`${t.groups.map(n=>`
      <h3 style="margin-top:16px">${a(n.label)}</h3>
      <table>
        ${(n.items||[]).map(r=>`<tr>
            <td>${r.secret?"🔒 ":""}${a(r.name)}</td>
            <td>${r.value===""||r.value===void 0?'<span class="muted">未设置</span>':a(r.value)}</td>
            <td class="muted">${a(r.detail||"")}</td>
          </tr>`).join("")}
      </table>`).join("")}<p class="muted" style="margin-top:12px">${a(t.note||"")}</p>`}function pe(t){const e=t&&t.pii_retention;return!e||!e.ran?`<span class="muted">${a(e&&e.summary||"尚未执行")}</span>`:`${a(e.ran_at||"")}<div class="muted">${a(e.summary||"")}</div>`}function ue(t){if(!t)return'<span class="muted">加载中…</span>';if(!t.available)return`<span class="muted">${a(t.reason||"不适用")}</span>`;const e=t.used===null||t.used===void 0?"?":t.used,n=t.limit===null||t.limit===void 0?"?":t.limit,r=t.exhausted?"failed":"approved",o=t.exhausted?"今日已用完":`剩 ${t.remaining}`;return`${e} / ${n} <span class="tag ${r}">${a(o)}</span>`}function he(){var e,n;const t=s.status||{};return`
    <div class="page-head"><div><h1>设置</h1><p>环境与能力开关（只读展示）。</p></div></div>
    <section class="panel"><div class="panel-body">
      <table>
        <tr><th>项</th><th>值</th></tr>
        <tr><td>TikTok 模式</td><td>${a(t.tiktok_mode)}</td></tr>
        <tr><td>平台适配器</td><td>${a(t.platform)}</td></tr>
        <tr><td>LLM</td><td>${a(t.llm_provider)} ${t.llm_degraded?"(降级)":""}</td></tr>
        <tr><td>时区</td><td>${a(t.timezone)}</td></tr>
        <tr><td>上架写通道</td><td>${t.publish_enabled?"可（仍须审批）":"关"}</td></tr>
        <tr><td>TTS 真写武装</td><td>${t.tts_write_armed?"是":"否"}</td></tr>
        <tr><td>今日写配额</td><td>${ue(s.ttsQuota)}</td></tr>
        <tr><td>发货</td><td>禁用（去卖家中心）</td></tr>
        <tr><td>退款</td><td>禁用（去卖家中心）</td></tr>
        <tr><td>上次 PII 清理</td><td>${pe(s.settings)}</td></tr>
        <tr><td>当前用户</td><td>${a((e=s.user)==null?void 0:e.username)} / ${a((n=s.user)==null?void 0:n.role)}</td></tr>
      </table>
      ${le(s.settings)}
      <p class="muted" style="margin-top:12px">${a(t.note||"")}</p>
      <p class="muted">开发自测面板：<a href="/workflow">/workflow</a></p>
    </div></section>
  `}function ot(t){if(!t)return"缺数据";const e=new Date(t);if(Number.isNaN(e.getTime()))return String(t).slice(0,16);const n=r=>String(r).padStart(2,"0");return`${n(e.getMonth()+1)}-${n(e.getDate())} ${n(e.getHours())}:${n(e.getMinutes())}`}function vt(t,e){return t==="ok"?"succeeded":t==="waiting_approval"?"awaiting_approval":t==="failed"?"failed":t==="suspended"?"suspended":e||""}function me(t){return t==="failed"?"bad":t==="waiting_approval"||t==="suspended"?"warn":t==="ok"?"":"idle"}function ft(t,e){return!e||!e.length?"":`<div class="run-summary"><div class="muted">${a(t)}</div>${e.map(n=>`<div><span class="k">${a(n.label)}</span>${a(n.value)}</div>`).join("")}</div>`}function ve(t){if(!t||t.status!=="recorded")return'<span class="missing">LLM · 缺数据</span>';const e=Number(t.prompt_tokens||0)+Number(t.completion_tokens||0),n=t.cost_usd==null?"—":Number(t.cost_usd).toFixed(4),r=t.model?` · ${a(t.model)}`:"";return`LLM ${a(e)} tokens · $${a(n)}${r}`}function gt(t){return t?`<pre class="error-data" data-error-code="${a(t.code||"")}">${a(JSON.stringify(t,null,2))}</pre>`:""}function it(t,e){if(!t||!t.id)return"";const n=t.pending?"去审批队列":"查看审批",r=t.href||`/admin/approvals?id=${encodeURIComponent(t.id)}`;return`<a class="${e}" href="${a(r)}" data-orch-approval="${a(t.id)}">${n}</a>`}function fe(){const t=s.orchRuns||[],e=s.orchRun,n=e&&e.run,r={ok:0,waiting:0,failed:0};t.forEach(m=>{m.display_status==="ok"?r.ok+=1:m.display_status==="waiting_approval"?r.waiting+=1:m.display_status==="failed"&&(r.failed+=1)});const o=n&&(n.display_status==="failed"||n.display_status==="waiting_approval"),d=n?n.display_status==="waiting_approval"?"等待人工审批 · 从这里进入审批队列，本页不代批":n.display_status==="failed"?"有步骤失败 · 错误以数据返回，未外呼平台":"这条运行的时间线只读 · 不打开 TikTok 真写":"只读时间线 · 数字来自已落库的编排记录 · 不打开 TikTok 真写",i=[["最近运行",t.length,"本页列表",t.length?"":"idle"],["成功",r.ok,"终态成功",r.ok?"":"idle"],["等待人工审批",r.waiting,"链到审批队列",r.waiting?"warn":"idle"],["失败",r.failed,"错误以数据返回",r.failed?"bad":"idle"]],c=t.length?`<table><thead><tr><th>状态</th><th>商品</th><th>开始</th><th>耗时</th><th>停在</th></tr></thead><tbody>
      ${t.map(m=>{const p=n&&n.id===m.id?' style="background:var(--bg)"':"",_=m.product&&m.product.title_label||"缺数据",S=m.pending_approval?`<div>${it(m.pending_approval,"queue-link")}</div>`:"";return`<tr class="clickable" data-orch-run="${a(m.id)}"${p}>
            <td><span class="tag ${a(vt(m.display_status,m.status))}">${a(m.display_label||m.status)}</span></td>
            <td title="${a(m.product&&m.product.url||"")}">${a(_)}</td>
            <td>${a(ot(m.started_at))}</td>
            <td>${a(m.duration_label||"缺数据")}</td>
            <td>${a(m.stopped_step_label||"—")}${S}</td>
          </tr>`}).join("")}
      </tbody></table>`:k("暂无编排运行","在总览点「一键 mock 演示」，跑完后这里出现完整时间线。");let f=k("选择运行","左侧点一行查看每个 agent 步骤。本页只读。");if(n){const m=e.timeline||[];f=`
      <div class="panel-head">
        <h2>${a(n.display_label||n.status)} · ${a(n.stopped_step_label||"—")}</h2>
        <span class="muted mono">${a(n.id)}</span>
      </div>
      <div class="panel-body">
        <p class="muted" style="margin-top:0">商品 ${a(n.product&&n.product.title_label||"缺数据")} · 开始 ${a(ot(n.started_at))} · 耗时 ${a(n.duration_label||"缺数据")}</p>
        ${n.pending_approval?`<p>${it(n.pending_approval,"btn secondary pill")}</p>`:""}
        ${gt(n.error)}
        <h3>时间线</h3>
        ${m.length?`<ol class="timeline" id="orch-run-timeline">${m.map(p=>`<li data-step="${a(p.step_id)}" data-display-status="${a(p.display_status)}">
                    <span class="health-dot ${me(p.display_status)}" style="margin-top:6px"></span>
                    <div>
                      <div class="step-head">
                        <strong>${a(p.label||p.step_id)}</strong>
                        <span class="tag ${a(vt(p.display_status,p.status))}">${a(p.display_label||p.status)}</span>
                      </div>
                      <div class="muted">${a(ot(p.started_at||p.finished_at))} · ${a(p.duration_label||"缺数据")}${p.attempt?` · 尝试 ${a(p.attempt)}`:""}</div>
                      ${ft("输入",p.input_summary)}
                      ${ft("输出",p.output_summary)}
                      <p class="hint" style="margin:6px 0 0">${ve(p.llm)}</p>
                      ${gt(p.error)}
                      ${p.approval?`<p style="margin:8px 0 0">${it(p.approval,"btn secondary pill")}</p>`:""}
                    </div>
                  </li>`).join("")}</ol>`:k("无步骤","这条 run 还没有步骤记录。")}
      </div>`}return`
    <div class="banner ${o?"warn":""}" id="orch-run-banner">${M.check}<span>${a(d)}</span></div>
    <div class="page-head">
      <div>
        <h1>编排运行</h1>
        <p>选品 → 定价 → 文案图片 → 质检 → 短视频脚本 → 达人建联 → 审批 → 上架 → 订单 → 代发 → 物流 → 客服。只读，不代批、不外呼。</p>
      </div>
    </div>
    <div class="grid-4 run-kpis" id="orch-run-health" style="margin-bottom:12px">
      ${i.map(([m,p,_,S])=>`
        <section class="card health-card">
          <div class="health-top">
            <span class="health-dot ${S}"></span>
            <span class="health-name">${a(m)}</span>
          </div>
          <div class="metric">${a(p)}</div>
          <div class="health-desc">${a(_)}</div>
        </section>`).join("")}
    </div>
    <div class="split">
      <section class="panel" id="orch-run-list"><div class="panel-head"><h2>最近运行</h2></div><div class="panel-body">${c}</div></section>
      <section class="panel" id="orch-run-detail">${f}</section>
    </div>
    <footer class="page-foot">本页只读。不外呼 TikTok，不打开发货、退款或付款，真写开关保持关闭。待审单进入现有审批队列。</footer>
  `}async function ge(){await N();try{const n=await v("/api/workflow/shops");s.profitShops=n.shops||[]}catch{s.profitShops=[{shop_id:"default"}]}const t=new URLSearchParams;s.profitShop&&t.set("shop_id",s.profitShop),s.profitFrom&&t.set("from",s.profitFrom),s.profitTo&&t.set("to",s.profitTo);const e=t.toString();s.profit=await v("/api/workflow/profit"+(e?`?${e}`:""))}async function be(){const t=new URLSearchParams;s.agentFrom&&t.set("from",s.agentFrom),s.agentTo&&t.set("to",s.agentTo);const e=t.toString();s.agentStats=await v("/api/admin/agents/stats"+(e?`?${e}`:""));const n=s.agentStats&&s.agentStats.runs||[],r=s.agentRun&&s.agentRun.run&&s.agentRun.run.id,o=r&&n.some(d=>d.id===r)?r:n[0]&&n[0].id;o?s.agentRun=await v(`/api/admin/agents/runs/${encodeURIComponent(o)}`):s.agentRun=null}async function $e(){const t=await v("/api/workflow/outreach/drafts");s.outreachDrafts=t.drafts||[]}async function ye(){const[t,e]=await Promise.all([v("/api/workflow/cs/inbox"),v("/api/workflow/cs/drafts")]);s.csInbox=t.messages||[],s.csDrafts=e.drafts||[]}async function we(t){const e=await v("/api/admin/runs?limit=50");s.orchRuns=e.runs||[];const r=t||""||s.orchRuns[0]&&s.orchRuns[0].id;if(!r){s.orchRun=null;return}try{s.orchRun=await v(`/api/admin/runs/${encodeURIComponent(r)}`)}catch(o){if(o.status===404&&s.orchRuns[0]&&s.orchRuns[0].id!==r){s.orchRun=await v(`/api/admin/runs/${encodeURIComponent(s.orchRuns[0].id)}`);return}throw o}}async function Q(t){const e=await v("/api/orchestrator/runs?limit=50");s.runs=e.runs||[],t?s.selectedRun=await v(`/api/orchestrator/runs/${encodeURIComponent(t)}`):s.selectedRun&&s.selectedRun.id?s.selectedRun=await v(`/api/orchestrator/runs/${encodeURIComponent(s.selectedRun.id)}`):s.runs[0]?s.selectedRun=await v(`/api/orchestrator/runs/${encodeURIComponent(s.runs[0].id)}`):s.selectedRun=null}async function _e(){if(s.route==="overview")await N();else if(s.route==="workflows")await N(),await Q(lt()||s.selectedRun&&s.selectedRun.id);else if(s.route==="runs")await we(lt()||s.orchRun&&s.orchRun.run&&s.orchRun.run.id);else if(s.route==="agents")await be();else if(s.route==="approvals")await N(),await dt(jt()||s.selectedApproval&&s.selectedApproval.id);else if(s.route==="cs")await N(),await ye();else if(s.route==="outreach")await N(),await $e();else if(s.route==="products"){await N();const t=await v("/api/workflow/catalog");s.catalog=t.items||t.catalog||[]}else if(s.route==="orders"){await N();const t=s.activeShop||"default",e=s.shopGeneration,n=`?shop_id=${encodeURIComponent(t)}`;try{const r=await v(`/api/workflow/orders/synced${n}`);if(e!==s.shopGeneration||(s.activeShop||"default")!==t||(s.syncedOrders=r.orders||[],s.sla=r.sla||await v(`/api/workflow/orders/sla${n}`),e!==s.shopGeneration||(s.activeShop||"default")!==t))return}catch(r){if(e!==s.shopGeneration||(s.activeShop||"default")!==t)return;throw s.syncedOrders=[],s.sla=null,r}}else if(s.route==="scout"){await N();const t=await v("/api/workflow/drafts");s.drafts=t.drafts||[]}else if(s.route==="spotcheck")await N(),await yt();else if(s.route==="reports"){await N();const t=await v("/api/workflow/reports");s.reports=t.reports||[];const e=s.profitShop||"default",n=await v(`/api/workflow/profit?shop_id=${encodeURIComponent(e)}`);s.profit=n;const r=await v(`/api/workflow/ad-spend?shop_id=${encodeURIComponent(e)}`);s.adSpendRows=r.rows||[]}else if(s.route==="profit")await ge();else if(s.route==="settings"){await N();try{s.ttsQuota=await v("/api/admin/tts-quota")}catch{s.ttsQuota={available:!1,reason:"读取配额失败。"}}try{s.settings=await v("/api/admin/settings")}catch{s.settings=null}}}function ke(){var Z;const t=document.getElementById("order-workbench");if(t){V.attach(t,(Z=s.user)==null?void 0:Z.role);const h=E=>{history.replaceState({},"",`/admin/orders?order=${encodeURIComponent(E)}`),s.selectedOrderId=E,document.querySelectorAll("[data-order-work]").forEach(U=>{U.classList.toggle("is-selected",U.dataset.orderWork===E)}),V.shopId=s.activeShop||"default",V.open(E)};document.querySelectorAll("[data-order-work]").forEach(E=>{E.onclick=()=>h(E.dataset.orderWork)});const l=document.getElementById("order-search"),u=document.getElementById("order-status-filter"),b=document.getElementById("order-platform-filter"),A=()=>{const E=String((l==null?void 0:l.value)||"").trim().toLowerCase(),U=String((u==null?void 0:u.value)||""),tt=String((b==null?void 0:b.value)||"");document.querySelectorAll("[data-order-work]").forEach(q=>{const St=!E||String(q.dataset.orderSearch||"").includes(E),xt=!U||q.dataset.orderStatus===U,Et=!tt||q.dataset.orderPlatform===tt;q.hidden=!(St&&xt&&Et)})};l==null||l.addEventListener("input",A),u==null||u.addEventListener("change",A),b==null||b.addEventListener("change",()=>{s.orderPlatform=b.value,A()});const w=document.getElementById("shopee-csv");w==null||w.addEventListener("change",async()=>{const E=w.files&&w.files[0];if(!E)return;const U=s.activeShop||"default",tt=s.shopGeneration;try{const q=await E.text();if(tt!==s.shopGeneration||(s.activeShop||"default")!==U){w.value="",s.pageError="店铺已切换，已取消本次 CSV 导入，请重新选择文件。",g();return}await v("/api/workflow/orders/import/shopee",{method:"POST",body:JSON.stringify({csv_text:q,shop_id:U})}),w.value="",await g()}catch(q){s.pageError=q.message||"Shopee CSV 导入失败",g()}});const B=new URLSearchParams(location.search).get("order"),F=(s.syncedOrders||[]).find(E=>String(E.order_id||E.id)===B);F&&String(F.platform||"tiktok")==="tiktok"?h(B):(s.selectedOrderId="",V.clear())}const e=document.getElementById("btn-refresh-overview");e&&(e.onclick=()=>g()),document.querySelectorAll("[data-approval]").forEach(h=>{h.addEventListener("click",()=>{const l=h.getAttribute("data-approval");l&&K("approvals",{id:l})})}),document.querySelectorAll("[data-agent-run]").forEach(h=>{h.addEventListener("click",async()=>{const l=h.getAttribute("data-agent-run");l&&(s.agentRun=await v(`/api/admin/agents/runs/${encodeURIComponent(l)}`),g())})});const n=document.getElementById("agent-stats-form");n&&n.addEventListener("submit",h=>{var l,u;h.preventDefault(),s.agentFrom=((l=document.getElementById("agent-from"))==null?void 0:l.value)||"",s.agentTo=((u=document.getElementById("agent-to"))==null?void 0:u.value)||"",s.agentRun=null,g()});const r=document.getElementById("profit-console-form");r&&r.addEventListener("submit",h=>{var l,u,b;h.preventDefault(),s.profitShop=((l=document.getElementById("profit-shop"))==null?void 0:l.value)||"",s.profitFrom=((u=document.getElementById("profit-from"))==null?void 0:u.value)||"",s.profitTo=((b=document.getElementById("profit-to"))==null?void 0:b.value)||"",g()}),document.querySelectorAll("[data-orch-run]").forEach(h=>{h.addEventListener("click",async l=>{if(l.target&&l.target.closest&&l.target.closest("[data-orch-approval]"))return;const u=h.getAttribute("data-orch-run");if(!u)return;const b=`/admin/runs?run=${encodeURIComponent(u)}`;`${location.pathname}${location.search}`!==b&&history.pushState({},"",b),s.orchRun=await v(`/api/admin/runs/${encodeURIComponent(u)}`),g()})}),document.querySelectorAll("[data-orch-approval]").forEach(h=>{h.addEventListener("click",l=>{l.preventDefault(),l.stopPropagation();const u=h.getAttribute("data-orch-approval");u&&K("approvals",{id:u})})}),document.querySelectorAll("[data-run]").forEach(h=>{h.addEventListener("click",async()=>{const l=h.getAttribute("data-run"),u=`/admin/workflows?run=${encodeURIComponent(l)}`;`${location.pathname}${location.search}`!==u&&history.pushState({},"",u),s.selectedRun=await v(`/api/orchestrator/runs/${encodeURIComponent(l)}`),g()})});const o=document.getElementById("btn-start-run");o&&(o.onclick=async()=>{const h=await v("/api/orchestrator/runs",{method:"POST",body:JSON.stringify({url:Ct})});s.selectedRun=h,await Q(h.id),g()});const d=document.getElementById("btn-demo-orch");d&&(d.onclick=async()=>{if(!(s.user&&s.user.role==="viewer")){d.disabled=!0,d.textContent="演示运行中…";try{const h=await v("/api/admin/demo-orch",{method:"POST",body:"{}"}),l=h.run_id,u=h.href||`/admin/runs?run=${encodeURIComponent(l)}`;`${location.pathname}${location.search}`!==u&&history.pushState({},"",u),g()}catch(h){s.pageError=h.message||"演示失败",g()}}});const i=document.getElementById("btn-run-approve");i&&(i.onclick=async()=>{var u;const h=((u=document.getElementById("run-decision-note"))==null?void 0:u.value)||"",l=s.selectedRun.id;await v(`/api/orchestrator/runs/${encodeURIComponent(l)}/approve`,{method:"POST",body:JSON.stringify({note:h})}),await Q(l),g()});const c=document.getElementById("btn-run-reject");c&&(c.onclick=async()=>{var u;const h=(((u=document.getElementById("run-decision-note"))==null?void 0:u.value)||"").trim();if(!h){alert("打回必须填写原因");return}const l=s.selectedRun.id;await v(`/api/orchestrator/runs/${encodeURIComponent(l)}/reject`,{method:"POST",body:JSON.stringify({note:h})}),await Q(l),g()});const f=document.getElementById("btn-run-cancel");f&&(f.onclick=async()=>{const h=s.selectedRun.id;await v(`/api/orchestrator/runs/${encodeURIComponent(h)}/cancel`,{method:"POST",body:JSON.stringify({note:"admin cancel"})}),await Q(h),g()});const m=document.getElementById("btn-run-retry");m&&(m.onclick=async()=>{const h=s.selectedRun.id,l=s.selectedRun.current_step;if(!l){alert("没有可重试的当前步骤");return}await v(`/api/orchestrator/runs/${encodeURIComponent(h)}/steps/${encodeURIComponent(l)}/retry`,{method:"POST",body:"{}"}),await Q(h),g()});const p=document.getElementById("btn-save-inputs");p&&p.addEventListener("click",async()=>{const h=s.draftDetail||{},l=b=>(document.getElementById(b)||{}).value||"",u=document.getElementById("pi-msg");try{const b=await v(`/api/workflow/drafts/${h.id}/inputs`,{method:"POST",body:JSON.stringify({category_id:l("pi-category"),warehouse_id:l("pi-warehouse"),price_amount:l("pi-price"),image_path:l("pi-image")})});u&&(u.textContent=`已保存：${(b.saved||[]).join("、")||"无"}（草稿已回到待审）`),await g()}catch(b){u&&(u.textContent=`保存失败：${b&&b.message||b}`)}});const _=document.getElementById("btn-approve");_&&(_.onclick=async()=>{var u,b;const h=((u=document.getElementById("decision-note"))==null?void 0:u.value)||"",l=s.selectedApproval.id;await v(`/api/workflow/approvals/${encodeURIComponent(l)}/approve`,{method:"POST",body:JSON.stringify({note:h,expected_hash:((b=s.selectedApproval)==null?void 0:b.payload_hash)||""})}),await dt(l),g()});const S=document.getElementById("btn-reject");S&&(S.onclick=async()=>{var u;const h=(((u=document.getElementById("decision-note"))==null?void 0:u.value)||"").trim();if(!h){alert("打回必须填写原因");return}const l=s.selectedApproval.id;await v(`/api/workflow/approvals/${encodeURIComponent(l)}/reject`,{method:"POST",body:JSON.stringify({note:h})}),await dt(l),g()});const I=document.getElementById("btn-import-demo");I&&(I.onclick=async()=>{await v("/api/workflow/catalog/import-demo",{method:"POST",body:"{}"}),g()});const $=document.getElementById("btn-sync-orders");$&&($.onclick=async()=>{try{await v("/api/workflow/orders/sync",{method:"POST",body:JSON.stringify({shop_id:s.activeShop||"default"})})}catch(h){s.pageError=h.message||"订单同步失败"}g()});const y=document.getElementById("btn-gen-listing");y&&(y.onclick=async()=>{await v("/api/workflow/drafts/generate",{method:"POST",body:JSON.stringify({kind:"listing",items:[{name:"抽屉分隔盒",specs:"外径 20cm，材质 PP"}]})}),g()});const L=document.getElementById("btn-spot-score");L&&(L.onclick=async()=>{await Kt()});const R=document.getElementById("btn-spot-export");R&&(R.onclick=()=>{if(!s.spotCheckReport){window.alert("请先点「计算通过率」，再导出。");return}const h=new Blob([s.spotCheckReport],{type:"text/markdown;charset=utf-8"}),l=document.createElement("a");l.href=URL.createObjectURL(h),l.download="m1-spot-check.md",l.click(),URL.revokeObjectURL(l.href)});const x=document.getElementById("btn-spot-reload");x&&(x.onclick=async()=>{await yt(),g()});const P=document.getElementById("btn-run-research");P&&(P.onclick=async()=>{await v("/api/workflow/jobs/research",{method:"POST"}),g()});const H=document.getElementById("btn-job-research");H&&(H.onclick=async()=>{await v("/api/workflow/jobs/research",{method:"POST"}),g()});const W=document.getElementById("btn-job-daily");W&&(W.onclick=async()=>{await v("/api/workflow/jobs/daily",{method:"POST"}),g()});const C=document.getElementById("ad-spend-form");C&&(C.onsubmit=async h=>{h.preventDefault();const l=new FormData(C),u=String(l.get("metrics")||"").split(",").map(w=>w.trim()),b={shop_id:s.profitShop||"default",spend_date:String(l.get("spend_date")||""),amount_thb:String(l.get("amount_thb")||""),channel:String(l.get("channel")||"tiktok_ads"),sku:String(l.get("sku")||"")||null,impressions:u[0]?Number(u[0]):null,clicks:u[1]?Number(u[1]):null,conversions:u[2]?Number(u[2]):null},A=document.getElementById("ad-spend-form-error");try{await v("/api/workflow/ad-spend",{method:"POST",body:JSON.stringify(b)}),A&&(A.textContent=""),g()}catch(w){A&&(A.textContent=w.message||"记账失败")}});const J=document.getElementById("btn-ads-pull");J&&(J.onclick=async()=>{const h=new Date,l=new Date(h.getTime()-6*864e5),u=A=>A.toISOString().slice(0,10),b=document.getElementById("ad-spend-form-error");try{await v("/api/workflow/ad-spend/pull",{method:"POST",body:JSON.stringify({shop_id:s.profitShop||"default",date_from:u(l),date_to:u(h)})}),b&&(b.textContent=""),g()}catch(A){b&&(b.textContent=A.message||"拉取失败")}});const z=document.getElementById("btn-refresh-cs");z&&(z.onclick=()=>g());const X=document.getElementById("cs-inbox-form");X&&(X.onsubmit=async h=>{h.preventDefault();const l=new FormData(X),u=document.getElementById("cs-form-error");try{await v("/api/workflow/cs/inbox",{method:"POST",body:JSON.stringify({conversation_id:String(l.get("conversation_id")||"").trim(),category:String(l.get("category")||"").trim(),body:String(l.get("body")||"").trim(),lang:String(l.get("lang")||"th").trim()||"th",order_id:String(l.get("order_id")||"").trim()||null})}),s.csNote="已写入本地 mock inbox。未外呼 TikTok 客服。",s.csError="",u&&(u.textContent=""),g()}catch(b){s.csError=b.message||"写入失败",u&&(u.textContent=s.csError)}}),document.querySelectorAll("[data-cs-draft-msg]").forEach(h=>{h.addEventListener("click",async()=>{const l=h.getAttribute("data-cs-draft-msg");try{const u=await v("/api/workflow/cs/draft",{method:"POST",body:JSON.stringify({message_id:l})});s.csNote=u.note||(u.whitelist?"白名单免审。":"已建 auto_reply 审批。"),s.csError="",g()}catch(u){s.csError=u.message||"生成草稿失败",g()}})}),document.querySelectorAll("[data-cs-send]").forEach(h=>{h.addEventListener("click",async()=>{const l=h.getAttribute("data-cs-send");try{const u=await v(`/api/workflow/cs/drafts/${encodeURIComponent(l)}/send`,{method:"POST",body:"{}"});s.csNote=u.sent_stub?"已本地 stub 发送（sent_stub / dry_run）。未外呼 TikTok 客服。":u.note||"已处理。",s.csError="",g()}catch(u){s.csError=u.message||"发送失败",g()}})});const Y=document.getElementById("btn-refresh-outreach");Y&&(Y.onclick=()=>g()),document.querySelectorAll("[data-outreach-approve]").forEach(h=>{h.addEventListener("click",async()=>{var u;const l=h.getAttribute("data-outreach-approve");try{const b=await v(`/api/workflow/outreach/drafts/${encodeURIComponent(l)}/approve`,{method:"POST",body:JSON.stringify({note:((u=document.getElementById("outreach-note"))==null?void 0:u.value)||""})});s.outreachNote=b.sent?"":"已标记通过。没有发送私信，没有寄样，也没有付款。",s.outreachError="",g()}catch(b){s.outreachError=b.message||"标记失败",g()}})}),document.querySelectorAll("[data-outreach-reject]").forEach(h=>{h.addEventListener("click",async()=>{var b;const l=(((b=document.getElementById("outreach-note"))==null?void 0:b.value)||"").trim();if(!l){s.outreachError="标记拒绝必须填写原因。",g();return}const u=h.getAttribute("data-outreach-reject");try{await v(`/api/workflow/outreach/drafts/${encodeURIComponent(u)}/reject`,{method:"POST",body:JSON.stringify({note:l})}),s.outreachNote="已标记拒绝。没有发送。",s.outreachError="",g()}catch(A){s.outreachError=A.message||"标记失败",g()}})}),document.querySelectorAll("[data-cs-approval]").forEach(h=>{h.addEventListener("click",l=>{l.preventDefault();const u=h.getAttribute("data-cs-approval");u&&K("approvals",{id:u})})})}async function g(){const t=document.getElementById("app");if(s.route=Bt(),(s.route!=="orders"||!s.user)&&V.clear(),s.route==="login"||!s.user){if(!s.user&&s.route!=="login")try{const r=await Tt();s.user=r.user,window.__CSRF__=r.csrf_token||""}catch{s.route="login",history.replaceState({},"","/admin/login"),et();return}if(s.route==="login"){et();return}}try{await _e()}catch(r){if(r.status===401){s.user=null,s.route="login",history.replaceState({},"","/admin/login"),et();return}s.pageError=r.message}const n=({overview:pt,workflows:se,runs:fe,agents:oe,approvals:Dt,products:Gt,orders:Wt,scout:Yt,spotcheck:Xt,reports:te,profit:ae,cs:ie,outreach:ce,settings:he}[s.route]||pt)();t.innerHTML=Pt(n+(s.pageError?`<p class="error">${a(s.pageError)}</p>`:"")),s.pageError="",Mt(),ke()}window.addEventListener("popstate",()=>g());g();
