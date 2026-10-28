(function(){const t=document.createElement("link").relList;if(t&&t.supports&&t.supports("modulepreload"))return;for(const r of document.querySelectorAll('link[rel="modulepreload"]'))a(r);new MutationObserver(r=>{for(const o of r)if(o.type==="childList")for(const d of o.addedNodes)d.tagName==="LINK"&&d.rel==="modulepreload"&&a(d)}).observe(document,{childList:!0,subtree:!0});function e(r){const o={};return r.integrity&&(o.integrity=r.integrity),r.referrerPolicy&&(o.referrerPolicy=r.referrerPolicy),r.crossOrigin==="use-credentials"?o.credentials="include":r.crossOrigin==="anonymous"?o.credentials="omit":o.credentials="same-origin",o}function a(r){if(r.ep)return;r.ep=!0;const o=e(r);fetch(r.href,o)}})();const Y={operations:"运营总览",candidates:"选品工作台",materials:"素材中心",campaigns:"投放计划",monitoring:"数据监控",records:"调用记录"},ut={research:"研究建议",verify:"事实核验",write_copy:"文案生成",quality_check:"素材质检",prepare_campaign:"投放计划",record_metrics:"数据复盘",orchestrator:"总控建议"},Qt=s=>String(s??"").replace(/[&<>"']/g,t=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[t]),p=Qt,ot={researching:"调研中",shortlisted:"已入选",rejected:"不采用",draft:"草拟",manual_active:"平台人工启用",completed:"已结束",manual:"人工录入",csv:"平台导出 / CSV",mock:"模拟数据",text:"文案",image:"图片"};function Vt(s){if(s===""||s==null)return null;const t=Number(s);if(!Number.isFinite(t)||t<0)throw Error("请输入非负数字，未知请留空");return t}function it(s,t=!1){return s==null?"缺数据":t?`${(Number(s)*100).toFixed(2)}%`:Number(s).toLocaleString("zh-CN",{maximumFractionDigits:2})}function xt(s){return typeof s=="string"&&s.startsWith("/api/admin/operations/assets/")&&!s.includes("\\")?s:""}const F=(s,t,e="",a="text",r="")=>`<label class="ops-field">${p(t)}<input name="${s}" type="${a}" value="${p(e)}" ${r}></label>`,Z=(s,t,e="")=>`<label class="ops-field">${p(t)}<textarea name="${s}" rows="3">${p(e)}</textarea></label>`,U=(s,t,e,a="")=>`<label class="ops-field">${p(t)}<select name="${s}">${e.map(([r,o])=>`<option value="${p(r)}" ${String(r)===String(a)?"selected":""}>${p(o)}</option>`).join("")}</select></label>`,nt=s=>s.map(t=>[t,ot[t]||t]),Q=(s,t)=>`<a class="btn secondary" data-nav="${s}" href="/admin/${s}">${t}</a>`,ft=s=>`<div class="ops-empty">${s}</div>`,rt=["impressions","clicks","spend","revenue","orders"],gt={impressions:"展示次数",clicks:"点击次数",spend:"实际花费",revenue:"实际收入",orders:"订单数"};class Kt{constructor(t,e){this.api=t,this.context=e,this.token=0,this.data={},this.references={},this.edit=null,this.note="",this.error="",this.flash="",this.flashError=!1,this.candidateSuggestions={},this.copyState={},this.planState={},this.reviewState={},this.records=[],this.recordsTotal=0,this.recordsOffset=0,this.recordModule="",this.recordDetail={},this.recordDetailOpen=""}clear(){this.token++,this.data={},this.references={},this.edit=null,this.note="",this.error="",this.flash="",this.flashError=!1,this.scope="",this.offset=0,this.filters={},this.candidateSuggestions={},this.copyState={},this.planState={},this.reviewState={},this.records=[],this.recordsTotal=0,this.recordsOffset=0,this.recordModule="",this.recordDetail={},this.recordDetailOpen=""}capture(){return{...this.context()}}current(t){const e=this.context();return e.shop===t.shop&&e.generation===t.generation&&e.route===t.route&&e.user===t.user}url(t,e){if((t.startsWith("dashboard")||t.startsWith("metrics?"))&&this.filters){const a=new URLSearchParams(Object.entries(this.filters).filter(([,r])=>r)).toString();a&&(t+=`${t.includes("?")?"&":"?"}${a}`)}return`/api/admin/operations/${t}${t.includes("?")?"&":"?"}shop_id=${encodeURIComponent(e.shop)}`}async all(t,e,a){const r={candidates:"candidates",materials:"assets",campaigns:"campaigns",monitoring:"metrics"}[e.route],o=t===r&&this.offset||0,d=await this.api(this.url(`${t}?limit=50&offset=${o}`,e));return!this.current(e)||a!==this.token?[]:(t===r?this.total=d.total:this.references[t]={offset:d.items.length,total:d.total},d.items)}async loadMoreReferences(t){const e=this.capture(),a=this.token,r=this.references[t];if(!r||r.offset>=r.total||r.busy)return null;r.busy=!0;try{const o=await this.api(this.url(`${t}?limit=50&offset=${r.offset}`,e));if(!this.current(e)||a!==this.token)return null;const d=new Set((this.data[t]||[]).map(b=>b.id)),l=o.items.filter(b=>!d.has(b.id));return this.data[t]=[...this.data[t]||[],...l],r.offset+=o.items.length,r.total=o.items.length?o.total:r.offset,l}finally{r.busy=!1}}referenceControls(t){const e=this.references[t];return e?`<button class="btn secondary" type="button" data-ops-more="${t}" ${e.offset>=e.total?"disabled":""}>加载更多${{candidates:"商品",assets:"素材",campaigns:"计划",drafts:"草稿"}[t]}（已加载 ${e.offset} / ${e.total}）</button>`:""}async load(){const t=this.capture(),e=++this.token,a=`${t.shop}:${t.generation}:${t.route}`;if(this.scope!==a&&(this.data={},this.references={},this.edit=null,this.note="",this.error="",this.flash="",this.flashError=!1,this.scope=a,this.offset=0,this.filters={},this.candidateSuggestions={},this.copyState={},this.planState={},this.reviewState={},this.records=[],this.recordsTotal=0,this.recordsOffset=0,this.recordModule="",this.recordDetail={},this.recordDetailOpen=""),t.route==="records"){await this.loadRecords(t,e);return}const r=t.route==="operations"?["dashboard"]:t.route==="candidates"?["candidates"]:t.route==="materials"?["assets","candidates","drafts"]:t.route==="campaigns"?["campaigns","candidates","assets"]:["metrics","campaigns"];try{const o=await Promise.all(r.map(d=>d==="dashboard"?this.api(this.url(d,t)):this.all(d,t,e)));return!this.current(t)||e!==this.token?!1:(this.data=Object.fromEntries(r.map((d,l)=>[d,o[l]])),this.error="",!0)}catch(o){return this.current(t)&&e===this.token&&(this.data={},this.error=o.message),!1}}async loadRecords(t,e){const a=new URLSearchParams({limit:"50",offset:String(this.recordsOffset||0)});this.recordModule&&a.set("module",this.recordModule);try{const r=await this.api(this.url(`work-model-records?${a.toString()}`,t));return!this.current(t)||e!==this.token?!1:(this.records=r.items||[],this.recordsTotal=r.total||0,this.error="",!0)}catch(r){return this.current(t)&&e===this.token&&(this.records=[],this.recordsTotal=0,this.error=r.message),!1}}async openRecord(t){const e=this.capture(),a=this.token;this.recordDetailOpen=t;try{const r=await this.api(this.url(`work-model-records/${encodeURIComponent(t)}`,e));return!this.current(e)||a!==this.token?!1:(this.recordDetail={...this.recordDetail||{},[t]:r},!0)}catch(r){return this.current(e)&&a===this.token&&(this.recordDetail={...this.recordDetail||{},[t]:{error:r.message}}),!1}}render(){var o;const t=this.capture(),e=((o=t.user)==null?void 0:o.role)==="viewer",a=t.route==="records"?"研究 → 核验 → 文案 → 质检 → 计划 → 复盘：每一次模型调用都可回读":"选品 → 素材 → 人工投放计划 → 每日数据复盘",r=this.flash?`<p class="${this.flashError?"error":"ops-success"}">${p(this.flash)}</p>`:"";return`<div class="ops-workbench"><div class="page-head"><div><h1>${Y[t.route]}</h1><p>${p(a)}</p></div><button class="btn secondary" data-ops-refresh>刷新</button></div><p class="ops-boundary">小团队辅助工作台 · 店铺 ${p(t.shop)} · 记录保存在本地。投放、支付和履约由平台完成；这里不会执行广告投放。</p><div id="ops-feedback" role="status" aria-live="polite">${this.note?`<p class="ops-success">${p(this.note)}</p>`:""}${r}${this.error?`<p class="error">${p(this.error)}</p>`:""}</div>${e?'<p class="muted">当前账号只读。</p>':""}${["operations","monitoring"].includes(t.route)?this.filterForm():""}${this.error?'<button class="btn secondary" data-ops-refresh>重试加载</button>':this.content(t.route,e)}</div>`}filterForm(){var t,e,a,r,o;return`<form id="ops-filter" class="ops-filters">${F("start_date","开始日期",((t=this.filters)==null?void 0:t.start_date)||"","date")}${F("end_date","结束日期",((e=this.filters)==null?void 0:e.end_date)||"","date")}${U("source","数据来源",[["","全部来源"],...nt(["manual","csv","mock"])],((a=this.filters)==null?void 0:a.source)||"")}${this.capture().route==="monitoring"?U("campaign_id","投放计划",[["","全部计划"],...vt(this.data.campaigns||[],(r=this.filters)==null?void 0:r.campaign_id)],((o=this.filters)==null?void 0:o.campaign_id)||""):""}<button class="btn secondary">查询</button><button class="btn secondary" type="button" data-ops-reset>全部日期</button></form>`}content(t,e){if(t==="operations")return this.dashboard();if(t==="records")return this.recordsPanel();const a={candidates:"candidates",materials:"assets",campaigns:"campaigns",monitoring:"metrics"}[t],r=this.data[a]||[],o=t==="candidates"?Q("scout","AI 选品与 Listing 草稿")+Q("workflows","Scout / Stylist 工作流"):t==="materials"?Q("scout","生成 AI 文案与图片")+Q("runs","查看 Agent 产物")+Q("spotcheck","人工抽检"):"";return`<div class="ops-links">${o}</div>${o?'<p class="muted">沿用现有 AI 生成通道，mock 产物用于演示。核对商品事实后，将文案或图片保存到素材中心，填写原始来源。</p>':""}<div class="ops-layout"><section class="panel"><div class="panel-head"><h2>记录 · ${this.total??r.length}</h2></div><div class="panel-body ops-records">${r.length?r.map(d=>this.card(a,d,e)).join(""):ft("暂无记录。先建立候选商品，再整理素材、计划和每日数据。")}<div class="ops-actions"><button class="btn secondary" data-ops-prev ${(this.offset||0)===0?"disabled":""}>上一页</button><span>第 ${Math.floor((this.offset||0)/50)+1} 页 · 每页 50 条</span><button class="btn secondary" data-ops-next ${(this.offset||0)+r.length>=(this.total||0)?"disabled":""}>下一页</button></div></div></section>${e?"":`<section class="panel"><div class="panel-head"><h2>${this.edit?"编辑记录":"新增记录"}</h2></div><div class="panel-body">${this.form(a)}${t==="materials"?this.draftForm():""}${t==="monitoring"?this.importForm():""}</div></section>`}</div>`}dashboard(){var e;const t=this.data.dashboard||{};return`<div class="ops-stats">${[["candidates","候选商品"],["assets","素材"],["campaigns","投放计划"]].map(([a,r])=>{var o;return`<div class="panel ops-stat"><span>${r}</span><strong>${p(((o=t.counts)==null?void 0:o[a])??"缺数据")}</strong></div>`}).join("")}</div><div class="ops-links">${Q("candidates","1. 记录选品")}${Q("materials","2. 整理素材")}${Q("campaigns","3. 制定计划")}${Q("monitoring","4. 每日数据")}</div><section class="panel"><div class="panel-head"><h2>已记录表现 · 各币种独立汇总（含所录入的模拟数据）</h2></div><div class="panel-body">${(e=t.currencies)!=null&&e.length?`<div class="ops-table"><table><thead><tr><th>币种 / 来源</th>${["展示","点击","花费","收入","订单","CTR","CPC","ROAS"].map(a=>`<th>${a}</th>`).join("")}</tr></thead><tbody>${t.currencies.map(a=>`<tr><td>${p(a.currency)}<br><small>${p(Array.isArray(a.sources)?a.sources.map(r=>ot[r]||r).join(" / "):JSON.stringify(a.sources||{}))}</small></td>${[...rt,"ctr","cpc","roas"].map(r=>`<td>${it(a[r],r==="ctr")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`:ft("暂无数据。填写实际报表后显示指标，未知数值不按零计算。")}<p class="muted">金额为标注币种的主单位，不换汇。ROAS 是记录收入 ÷ 花费，不等于利润；计划状态不证明平台已投放。</p></div></section>`}recordsPanel(){const t=[["","全部模块"],...Object.entries(ut)],e=this.records||[],a=this.recordsTotal||0,r=this.recordsOffset||0,o=e.length,d=e.length?e.map(l=>this.recordCard(l)).join(""):ft("暂无调用记录。发起研究、核验、文案、质检、计划或复盘后，这里会留下可回读的记录。");return`<div class="ops-links">${Q("candidates","回到选品工作台")}</div><form id="ops-record-filter" class="ops-filters">${U("module","工作模块",t,this.recordModule||"")}<button class="btn secondary">筛选</button></form><div class="ops-layout"><section class="panel"><div class="panel-head"><h2>调用记录 · ${a}</h2></div><div class="panel-body ops-records">${d}<div class="ops-actions"><button class="btn secondary" data-ops-record-prev ${r===0?"disabled":""}>上一页</button><span>第 ${Math.floor(r/50)+1} 页 · 每页 50 条 · 本页 ${o} 条</span><button class="btn secondary" data-ops-record-next ${r+o>=a?"disabled":""}>下一页</button></div></div></section></div>`}recordCard(t){var o,d,l,b,f;const e=t.run||null,a=!!t.failure_reason||(e==null?void 0:e.status)==="failed";let r=`<p><span class="tag">${p(ut[t.module]||t.module)}</span> · 通道 ${p(t.model_channel||"未知")} · 提示词 ${p(t.prompt_version||"未知")}</p>`;return r+=`<p class="muted">时间 ${p(t.created_at||"")}${e!=null&&e.id?` · 运行 ${p(e.id)}`:""}${e!=null&&e.input_version_digest?` · 输入摘要 ${p(String(e.input_version_digest).slice(0,12))}`:""}</p>`,(o=t.input_object_ids)!=null&&o.length&&(r+=`<p class="muted">输入对象：${t.input_object_ids.map(u=>p(u)).join("、")}</p>`),(d=t.output_object_ids)!=null&&d.length&&(r+=`<p class="muted">关联对象：${t.output_object_ids.map(u=>p(u)).join("、")}</p>`),a?r+=`<p class="error">失败：${p((e==null?void 0:e.failure_code)||"")}${t.failure_reason?` · ${p(t.failure_reason)}`:""}</p>`:r+=`<p class="muted">状态：${p((e==null?void 0:e.status)||"已留痕")}</p>`,(t.human_review||(l=t.human_changes)!=null&&l.length)&&(r+=`<p class="muted">人工处理：${(b=t.human_changes)!=null&&b.length?t.human_changes.map(u=>p(u)).join("； "):"已复核"}</p>`),r+=`<div class="ops-actions"><button class="btn secondary" type="button" data-ops-record-open="${p(t.id)}">${this.recordDetailOpen===t.id&&((f=this.recordDetail)!=null&&f[t.id])?"收起详情":"查看详情"}</button></div>`,this.recordDetailOpen===t.id&&(r+=this.recordDetailBody(t)),`<article class="ops-record" data-record="${p(t.id)}"><h3>${p(ut[t.module]||t.module)} · ${p(t.id)}</h3>${r}</article>`}recordDetailBody(t){var o,d,l,b,f,u,O,T;const e=(o=this.recordDetail)==null?void 0:o[t.id];if(!e)return`<div class="ops-suggestion-box" data-record-detail="${p(t.id)}"><p class="muted">正在读取详情…</p></div>`;if(e.error)return`<div class="ops-suggestion-box" data-record-detail="${p(t.id)}"><p class="error">读取失败：${p(e.error)}</p></div>`;const a=e.run||null;let r=`<div class="ops-suggestion-box" data-record-detail="${p(t.id)}">`;return r+="<h4>调用详情</h4>",r+=`<p class="muted">模块 ${p(ut[(d=e.call)==null?void 0:d.module]||((l=e.call)==null?void 0:l.module)||"")} · 通道 ${p(((b=e.call)==null?void 0:b.model_channel)||"")} · 来源 ${p(((f=e.call)==null?void 0:f.source)||"")}</p>`,(O=(u=e.call)==null?void 0:u.human_changes)!=null&&O.length&&(r+=`<p><strong>人工改动：</strong>${e.call.human_changes.map(c=>p(c)).join("； ")}</p>`),a?(r+=`<p class="muted">运行 ${p(a.id)} · 状态 ${p(a.status||"")}${a.failure_code?` · 失败码 ${p(a.failure_code)}`:""}</p>`,a.failure_reason&&(r+=`<p class="error">失败原因：${p(a.failure_reason)}</p>`),(T=a.output_object_ids)!=null&&T.length&&(r+=`<p class="muted">关联对象：${a.output_object_ids.map(c=>p(c)).join("、")}</p>`),a.output&&(r+=`<h4>结构化结果</h4><pre class="ops-copy">${p(JSON.stringify(a.output,null,2))}</pre>`),a.raw_output&&(r+=`<details class="ops-import"><summary>原始输出</summary><pre class="ops-copy">${p(a.raw_output)}</pre></details>`),a.context&&Object.keys(a.context).length&&(r+=`<details class="ops-import"><summary>服务端关联上下文</summary><pre class="ops-copy">${p(JSON.stringify(a.context,null,2))}</pre></details>`),r+="</div>",r):(r+='<p class="muted">该调用没有关联的运行记录（可能来自旧的调用入口）。</p></div>',r)}card(t,e,a){var d,l,b,f,u,O,T,c,m,h,v,g,w,k,I,N;const r=e.name||((l=(d=this.data.campaigns)==null?void 0:d.find(x=>x.id===e.campaign_id))==null?void 0:l.name)||e.campaign_id;let o="";if(t==="candidates"){let x="";if((b=this.candidateSuggestions)!=null&&b[e.id]){const C=this.candidateSuggestions[e.id];x=`<div class="ops-suggestion-box" data-suggestion-cand="${p(e.id)}">`,C.module==="research"?(x+=`<h4>AI 研究建议（模型：${p(C.model_channel||"mock")}）</h4>`,(f=C.output)!=null&&f.recommendation&&(x+=`<p><strong>建议动作：</strong>${p(C.output.recommendation)}</p>`),(O=(u=C.output)==null?void 0:u.facts)!=null&&O.length&&(x+=`<p><strong>提取事实：</strong>${p(C.output.facts.join("； "))}</p>`),(c=(T=C.output)==null?void 0:T.hypotheses)!=null&&c.length&&(x+=`<p><strong>分析假设：</strong>${p(C.output.hypotheses.join("； "))}</p>`),(h=(m=C.output)==null?void 0:m.missing_info)!=null&&h.length&&(x+=`<p class="muted"><strong>缺失信息：</strong>${p(C.output.missing_info.join("； "))}</p>`),a||(x+=`<div class="ops-actions"><button type="button" class="btn" data-ops-adopt-run="${p(C.id)}" data-cand-id="${p(e.id)}" data-version="${p(C.input_version_digest)}">采纳研究建议</button></div>`)):C.module==="verify"&&(x+=`<h4>AI 事实核验（结论：${((v=C.output)==null?void 0:v.decision)==="pass"?"通过":"不通过"}）</h4>`,(w=(g=C.output)==null?void 0:g.claims)!=null&&w.length&&(x+=`<ul class="ops-claims-list">${C.output.claims.map(G=>`<li><strong>${p(G.claim)}</strong> [${p(G.status)}] ${G.evidence?`证据: ${p(G.evidence)}`:'<span class="warn">缺证据</span>'}</li>`).join("")}</ul>`),(I=(k=C.output)==null?void 0:k.missing_evidence)!=null&&I.length&&(x+=`<p class="muted"><strong>缺失证据：</strong>${p(C.output.missing_evidence.join("； "))}</p>`),!a&&((N=C.output)==null?void 0:N.decision)==="pass"&&(x+=`<div class="ops-actions"><button type="button" class="btn" data-ops-adopt-run="${p(C.id)}" data-cand-id="${p(e.id)}" data-version="${p(C.input_version_digest)}">采纳已核验事实</button></div>`)),x+="</div>"}x+=this.copyPanel(e,a),x+=this.planPanel(e,a),o=`<span class="tag">${p(ot[e.status])}</span><p>${p(e.facts)}</p><p>来源 ${p(e.source_url||"未填写")} · 成本 ${it(e.cost_amount)} ${p(e.currency)}</p>${x}`}return t==="assets"&&(o=(e.kind==="image"&&xt(e.content_url)?`<img class="ops-preview" src="${p(xt(e.content_url))}" alt="${p(e.name)}" loading="lazy">`:`<pre class="ops-copy">${p(e.text)}</pre>`)+`<p>语言 ${p(e.language||"未填写")} · 来源 ${p([e.source,e.draft_source].filter(Boolean).join(" · ")||"未填写")}</p><p>状态：${p(e.status==="usable"?"可用素材（已通过质检）":"草稿（未通过质检）")}</p>`),t==="campaigns"&&(o=`<p>${p(e.platform)} · ${p(ot[e.status])}</p><p>${p(e.notes)}</p><p>人工执行记录：${p(e.execution_notes||"未填写")}</p>`+this.reviewPanel(e,a)),t==="metrics"&&(o=`<p>${p(e.day)} · ${p(e.currency)} · ${p(ot[e.source]||e.source||"人工录入")}</p><p>${rt.map(x=>`${gt[x]} ${it(e[x])}`).join(" · ")}</p>`),`<article class="ops-record"><h3>${p(r)}</h3>${o}${a?"":`<div class="ops-actions">${t==="candidates"?`<button class="btn" type="button" data-ops-quick-list="${p(e.id)}" data-name="${p(e.name)}">生成 Listing 草稿</button><button class="btn secondary" type="button" data-ops-research="${p(e.id)}" data-name="${p(e.name)}">研究建议</button><button class="btn secondary" type="button" data-ops-verify="${p(e.id)}" data-name="${p(e.name)}">核验事实</button><button class="btn secondary" type="button" data-ops-write-copy="${p(e.id)}" data-name="${p(e.name)}">生成泰英文案</button><button class="btn secondary" type="button" data-ops-plan="${p(e.id)}" data-name="${p(e.name)}">生成投放计划建议</button>`:""}${t==="campaigns"?`<button class="btn secondary" type="button" data-ops-review="${p(e.id)}" data-name="${p(e.name)}">复盘这份数据</button>`:""}${t!=="assets"?`<button class="btn secondary" data-ops-edit="${p(e.id)}" data-kind="${t}">编辑</button>`:'<span class="muted">素材不可覆盖，修改请新增版本</span>'}<button class="btn secondary" data-ops-delete="${p(e.id)}" data-kind="${t}">删除</button></div>`}</article>`}planPanel(t,e){var l,b,f,u,O;const a=(l=this.planState)==null?void 0:l[t.id];if(!a||!a.run)return"";const r=a.run,o=r.output||{};let d=`<div class="ops-suggestion-box" data-plan-panel="${p(t.id)}">`;return d+="<h4>投放计划建议</h4>",r.status!=="succeeded"?(d+=`<p class="error">计划建议未通过：${p(r.failure_code||"未知原因")}</p>`,d+="</div>",d):(d+=`<p><strong>计划名称：</strong>${p(o.name||"未命名")}</p>`,o.objective&&(d+=`<p><strong>目标：</strong>${p(o.objective)}</p>`),(b=o.audience_hypotheses)!=null&&b.length&&(d+=`<p><strong>受众假设：</strong>${p(o.audience_hypotheses.join("； "))}</p>`),o.budget_note&&(d+=`<p><strong>预算说明：</strong>${p(o.budget_note)}</p>`),(f=o.execution_checklist)!=null&&f.length&&(d+=`<ul class="ops-claims-list">${o.execution_checklist.map(T=>`<li>${p(T)}</li>`).join("")}</ul>`),(u=o.tracking_fields)!=null&&u.length&&(d+=`<p class="muted"><strong>跟踪字段：</strong>${p(o.tracking_fields.join("、"))}</p>`),(O=o.missing)!=null&&O.length&&(d+=`<p class="muted"><strong>待补信息：</strong>${p(o.missing.join("； "))}</p>`),d+='<p class="muted">这是本地人工执行计划建议，不是平台回执；采纳后状态为草稿。</p>',e||(d+=`<div class="ops-actions"><button type="button" class="btn" data-ops-adopt-campaign="${p(r.id)}" data-cand-id="${p(t.id)}">采纳为本地计划（草稿）</button></div>`),d+="</div>",d)}reviewPanel(t,e){var f,u,O,T;const a=(f=this.reviewState)==null?void 0:f[t.id];if(!a||!a.run)return"";const r=a.run,o=r.output||{};let d=`<div class="ops-suggestion-box" data-review-panel="${p(t.id)}">`;if(d+="<h4>数据复盘</h4>",r.status!=="succeeded")return d+=`<p class="error">复盘未通过：${p(r.failure_code||"未知原因")}</p>`,d+="</div>",d;const l=o.scope||{};l.currency&&(d+=`<p class="muted">范围：${p(l.campaign_id||t.id)} · ${p(l.currency)}${l.from?` · ${p(l.from)} 起`:""}${l.to?` · 至 ${p(l.to)}`:""}</p>`);const b=o.known_metrics||{};return d+=`<ul class="ops-claims-list">${rt.map(c=>`<li>${p(gt[c])} ${it(b[c])}</li>`).join("")}${["ctr","cpc","roas"].map(c=>`<li>${c.toUpperCase()} ${it(b[c],c==="ctr")}</li>`).join("")}</ul>`,(u=o.observations)!=null&&u.length&&(d+=`<p><strong>观察：</strong>${p(o.observations.join("； "))}</p>`),(O=o.data_gaps)!=null&&O.length&&(d+=`<p class="muted"><strong>数据缺口：</strong>${p(o.data_gaps.join("； "))}</p>`),(T=o.next_actions)!=null&&T.length&&(d+=`<p class="muted"><strong>建议下一步：</strong>${p(o.next_actions.join("； "))}</p>`),d+='<p class="muted">比率由服务端按原始指标重算；原始指标不被复盘结果改写。</p>',d+="</div>",d}copyPanel(t,e){var b,f,u,O,T,c,m,h;const a=(b=this.copyState)==null?void 0:b[t.id];if(!a)return"";const r=a.copy,o=a.quality,d=(r==null?void 0:r.output)||{};let l=`<div class="ops-suggestion-box" data-copy-panel="${p(t.id)}">`;if(l+="<h4>泰英文案</h4>",r?(l+=`<pre class="ops-copy">${p(d.title_th||"")}
${p(d.body_th||"")}
${p(d.title_en||"")}
${p(d.body_en||"")}</pre>`,l+=`<p class="muted">模型：${p(r.model_channel||"mock")}</p>`):l+='<p class="muted">尚未生成文案。生成需先有当前版本的有效核验作为事实背书。</p>',o){const v=((f=o.output)==null?void 0:f.checks)||{},g={facts_supported:"事实有据",language_ok:"语言可用",sensitive_claims_checked:"敏感声明已查",copyright_source_present:"版权来源齐备"};l+=`<h4>素材质检（结论：${((u=o.output)==null?void 0:u.decision)==="pass"?"通过":o.status==="failed"?"未通过":"待人工复核"}）</h4>`,l+=`<ul class="ops-claims-list">${Object.entries(g).map(([w,k])=>`<li>${p(k)}：${v[w]===!0?"是":"否"}</li>`).join("")}</ul>`,(T=(O=o.output)==null?void 0:O.issues)!=null&&T.length&&(l+=`<p class="muted"><strong>问题：</strong>${p(o.output.issues.join("； "))}</p>`),(m=(c=o.output)==null?void 0:c.required_edits)!=null&&m.length&&(l+=`<p class="muted"><strong>必改项：</strong>${p(o.output.required_edits.join("； "))}</p>`),o.status==="failed"&&(l+=`<p class="error">质检未通过：${p(o.failure_code||"")}</p>`)}return!e&&(o==null?void 0:o.status)==="succeeded"&&((h=o.output)==null?void 0:h.decision)==="pass"&&(l+=`<div class="ops-actions"><button type="button" class="btn" data-ops-adopt-asset="${p(o.id)}" data-cand-id="${p(t.id)}">采纳为可用素材</button></div>`),!e&&(r==null?void 0:r.status)==="succeeded"&&!o&&(l+=`<div class="ops-actions"><button type="button" class="btn secondary" data-ops-quality-check="${p(r.id)}" data-cand-id="${p(t.id)}" data-name="${p(t.name)}">质检这份文案</button></div>`),l+="</div>",l}form(t){const e=this.edit||{},a=[["","不关联"],...vt(this.data.candidates||[],e.candidate_id)];let r="";return t==="candidates"&&(r=F("name","商品名称",e.name,"text",'required maxlength="200"')+`<div class="ops-url-row">${F("source_url","货源链接",e.source_url,"url")}<button type="button" class="btn secondary" data-ops-extract-url>自动提取</button></div>`+Z("facts","已确认商品事实",e.facts)+Z("notes","调研备注",e.notes)+U("status","选品判断",nt(["researching","shortlisted","rejected"]),e.status||"researching")+F("cost_amount","成本金额（未知留空）",e.cost_amount,"number",'min="0" step="0.01"')+F("currency","成本币种",e.currency||"THB","text",'required pattern="[A-Z]{3}" maxlength="3"')),t==="assets"&&(r=F("name","素材名称",e.name,"text",'required maxlength="200"')+U("candidate_id","关联商品",a,e.candidate_id)+U("kind","素材类型",nt(["text","image"]),e.kind||"text")+F("language","语言（th / en / vi 等）",e.language)+F("source","来源（人工 / mock / AI 通道 / 原始链接）",e.source,"text","required")+Z("text","文案正文",e.text)+'<label class="ops-field">上传图片（PNG / JPEG / WebP，最大 5 MiB）<input name="file" type="file" accept="image/png,image/jpeg,image/webp"></label><div id="ops-file-preview"></div><p class="muted">请确认素材使用权。已保存素材不可覆盖，修改请新增版本。</p>'),t==="campaigns"&&(r=F("name","计划名称",e.name,"text",'required maxlength="200"')+U("candidate_id","关联商品",a,e.candidate_id)+U("platform","计划平台",[["tiktok","TikTok"],["shopee","Shopee"],["other","其他"]],e.platform||"tiktok")+`<fieldset class="ops-assets"><legend>选用素材</legend>${(this.data.assets||[]).map(o=>`<label><input type="checkbox" name="asset_ids" value="${p(o.id)}" ${(e.asset_ids||[]).includes(o.id)?"checked":""}>${p(o.name)} · ${p(o.language)} · ${p(o.source)}</label>`).join("")||"请先添加素材"}</fieldset>`+Z("notes","目标 / 预算说明（仅计划）",e.notes)+U("status","人工计划状态",nt(["draft","manual_active","completed"]),e.status||"draft")+Z("execution_notes","平台人工操作记录 / 计划编号",e.execution_notes)+'<p class="muted">人工标记启用不会开通账户、扣费或向平台发送投放请求。</p>'),t==="metrics"&&(r=U("campaign_id","关联投放计划",vt(this.data.campaigns||[],e.campaign_id),e.campaign_id)+F("day","平台报表日期",e.day||new Date().toLocaleDateString("en-CA"),"date","required")+F("currency","金额币种",e.currency||"THB","text",'required pattern="[A-Z]{3}" maxlength="3"')+U("source","数据来源",nt(["manual","csv","mock"]),e.source||"manual")+rt.map(o=>F(o,gt[o]+(["spend","revenue"].includes(o)?"（主单位）":""),e[o],"number",`min="0" step="${["spend","revenue"].includes(o)?"0.01":"1"}"`)).join("")+'<p class="muted">未知留空，实际为零填 0。同计划、日期、币种再次保存会覆盖该日记录，修改日期会新增。</p>'),`<form id="ops-form" class="ops-form" data-kind="${t}">${r}<div class="ops-actions">${t==="assets"?this.referenceControls("candidates"):t==="campaigns"?this.referenceControls("candidates")+this.referenceControls("assets"):t==="metrics"?this.referenceControls("campaigns"):""}</div><div class="ops-actions"><button class="btn" type="submit">保存${t==="campaigns"?"本地计划":""}</button>${this.edit?'<button class="btn secondary" type="button" data-ops-cancel>取消编辑</button>':""}</div></form>`}draftForm(){return`<details class="ops-import"><summary>从 AI 草稿导入文案</summary><p>保留草稿来源，不修改审批、不执行发布。图片请从产物下载并上传。</p><form id="ops-draft-import">${U("draft_id","现有草稿",(this.data.drafts||[]).map(t=>[t.id,`${t.title} · ${t.source||t.kind}`]))}${U("candidate_id","关联商品",[["","不关联"],...(this.data.candidates||[]).map(t=>[t.id,t.name])])}${U("language","语言",[["en","英文"],["th","泰文"]])}${this.referenceControls("drafts")}<button type="submit" class="btn secondary" ${(this.data.drafts||[]).length?"":"disabled"}>导入为素材</button></form></details>`}importForm(){return`<details class="ops-import"><summary>批量导入每日数据（JSON）</summary><p>粘贴行数组。逐行保存，重复键覆盖；失败时显示已保存行数。</p><form id="ops-import">${U("campaign_id","计划",(this.data.campaigns||[]).map(t=>[t.id,t.name]))}${Z("rows","数据数组",'[{"day":"2026-10-22","currency":"THB","impressions":100,"clicks":5,"spend":20,"revenue":60,"orders":1,"source":"manual"}]')}<button class="btn secondary">导入记录</button></form></details>`}feedback(t,e=!1){this.flash=t,this.flashError=!!e;const a=document.getElementById("ops-feedback");a&&(a.innerHTML=`<p class="${e?"error":"ops-success"}">${p(t)}</p>`)}bind(t,e){t.querySelectorAll("[data-ops-more]").forEach(c=>c.onclick=async()=>{var v;const m=this.capture(),h=c.dataset.opsMore;c.disabled=!0;try{const g=await this.loadMoreReferences(h);if(!g||!this.current(m)||!c.isConnected)return;const w={candidates:"candidate_id",campaigns:"campaign_id",drafts:"draft_id"}[h];if(w&&t.querySelectorAll(`select[name="${w}"]`).forEach(I=>{const N=new Set([...I.options].map(x=>x.value));for(const x of g){if(N.has(x.id))continue;const C=document.createElement("option");C.value=x.id,C.textContent=x.name||x.title||x.id,I.append(C)}}),h==="assets"){const I=t.querySelector(".ops-assets");if(I)for(const N of g){const x=document.createElement("label"),C=document.createElement("input");C.type="checkbox",C.name="asset_ids",C.value=N.id,C.checked=(((v=this.edit)==null?void 0:v.asset_ids)||[]).includes(N.id),x.append(C,document.createTextNode(`${N.name} · ${N.language||""} · ${N.source||""}`)),I.append(x)}}const k=this.references[h];c.textContent=`加载更多（已加载 ${k.offset} / ${k.total}）`,this.feedback("已加载更多选项，表单输入和已选内容保留。")}catch(g){this.current(m)&&this.feedback(g.message,!0)}finally{if(this.current(m)&&c.isConnected){const g=this.references[h];c.disabled=!g||g.offset>=g.total}}});const a=t.querySelector("#ops-filter");a&&(a.onsubmit=c=>{c.preventDefault(),this.filters=Object.fromEntries(new FormData(a)),this.offset=0,e()});const r=t.querySelector("[data-ops-reset]");r&&(r.onclick=()=>{this.filters={},this.offset=0,e()});const o=t.querySelector("[data-ops-prev]"),d=t.querySelector("[data-ops-next]");o&&(o.onclick=()=>{this.offset=Math.max(0,(this.offset||0)-50),this.edit=null,e()}),d&&(d.onclick=()=>{this.offset=(this.offset||0)+50,this.edit=null,e()}),t.querySelectorAll("[data-ops-refresh]").forEach(c=>c.onclick=()=>e());const l=t.querySelector("#ops-record-filter");l&&(l.onsubmit=c=>{c.preventDefault(),this.recordModule=new FormData(l).get("module")||"",this.recordsOffset=0,this.recordDetail={},this.recordDetailOpen="",e()}),t.querySelectorAll("[data-ops-record-prev]").forEach(c=>c.onclick=()=>{this.recordsOffset=Math.max(0,(this.recordsOffset||0)-50),this.recordDetail={},this.recordDetailOpen="",e()}),t.querySelectorAll("[data-ops-record-next]").forEach(c=>c.onclick=()=>{this.recordsOffset=(this.recordsOffset||0)+50,this.recordDetail={},this.recordDetailOpen="",e()}),t.querySelectorAll("[data-ops-record-open]").forEach(c=>c.onclick=async()=>{var v;const m=c.dataset.opsRecordOpen,h=this.capture();if(this.recordDetailOpen===m&&((v=this.recordDetail)!=null&&v[m])){this.recordDetailOpen="",e(!1);return}c.disabled=!0;try{if(await this.openRecord(m),!this.current(h))return;e(!1)}finally{this.current(h)&&c.isConnected&&(c.disabled=!1)}}),t.querySelectorAll("[data-ops-edit]").forEach(c=>c.onclick=()=>{this.edit=this.data[c.dataset.kind].find(m=>String(m.id)===c.dataset.opsEdit),e(!1)});const b=t.querySelector("[data-ops-cancel]");b&&(b.onclick=()=>{this.edit=null,e(!1)}),t.querySelectorAll("[data-ops-delete]").forEach(c=>c.onclick=async()=>{if(!window.confirm("确定删除这条本地记录？被引用的记录无法删除。"))return;const m=this.capture();c.disabled=!0;try{await this.api(this.url(`${c.dataset.kind}/${encodeURIComponent(c.dataset.opsDelete)}`,m),{method:"DELETE"}),this.current(m)&&(this.edit=null,this.note="已删除本地记录。",await e())}catch(h){this.current(m)&&this.feedback(h.message,!0)}finally{this.current(m)&&c.isConnected&&(c.disabled=!1)}});const f=t.querySelector("#ops-form");if(f){const c=f.querySelector("[name=file]");c&&(c.onchange=async()=>{const m=this.capture(),h=c.files[0];if(h)try{const v=await Ot(h);if(!this.current(m)||!c.isConnected||c.files[0]!==h)return;t.querySelector("#ops-file-preview").innerHTML=`<img class="ops-preview" alt="待保存图片预览" src="data:${p(h.type)};base64,${v}">`,f.elements.kind.value="image"}catch(v){this.current(m)&&this.feedback(v.message,!0)}}),f.onsubmit=async m=>{var N,x;m.preventDefault();const h=this.capture(),v=new FormData(f),g=f.dataset.kind,w=(N=this.edit)==null?void 0:N.id,k=Object.fromEntries(v);delete k.file,"candidate_id"in k&&(k.candidate_id||(k.candidate_id=null)),g==="campaigns"&&(k.asset_ids=Xt(((x=this.edit)==null?void 0:x.asset_ids)||[],this.data.assets||[],v.getAll("asset_ids")));const I=f.querySelector("[type=submit]");I.disabled=!0,this.feedback("正在保存…");try{for(const C of g==="metrics"?rt:g==="candidates"?["cost_amount"]:[])k[C]=Vt(k[C]);if(g==="assets"&&c.files[0]&&(k.mime_type=c.files[0].type,k.data_base64=await Ot(c.files[0])),!this.current(h)||(await this.api(this.url(`${g}${w&&g!=="metrics"?`/${encodeURIComponent(w)}`:""}`,h),{method:w&&g!=="metrics"?"PATCH":"POST",body:JSON.stringify(k)}),!this.current(h)))return;this.edit=null,this.note="已保存到当前店铺。未执行平台投放或付款。",await e()}catch(C){this.current(h)&&this.feedback(C.message,!0)}finally{this.current(h)&&I.isConnected&&(I.disabled=!1)}}}const u=t.querySelector("[data-ops-extract-url]");u&&(u.onclick=async()=>{const c=t.querySelector('input[name="source_url"]'),m=((c==null?void 0:c.value)||"").trim();if(!m){this.feedback("请先填写货源链接（须以 http:// 或 https:// 开头）。",!0),c==null||c.focus();return}if(!m.startsWith("http://")&&!m.startsWith("https://")){this.feedback("货源链接须包含 http:// 或 https:// 协议头。",!0),c==null||c.focus();return}const h=this.capture();u.disabled=!0,u.textContent="提取中…",this.feedback("正在从链接解析商品信息…");try{const v=await this.api(this.url("candidates/extract-url",h),{method:"POST",body:JSON.stringify({url:m})});if(!this.current(h))return;const g=t.querySelector('input[name="name"]'),w=t.querySelector('textarea[name="facts"]'),k=t.querySelector('textarea[name="notes"]'),I=t.querySelector('input[name="cost_amount"]'),N=t.querySelector('input[name="currency"]');g&&v.name&&(g.value=v.name),w&&v.facts&&(w.value=v.facts),k&&v.notes&&(k.value=v.notes),I&&v.cost_amount!=null&&(I.value=v.cost_amount),N&&v.currency&&(N.value=v.currency),v.is_mock?this.feedback("已填入离线 Mock 演示数据，不是真实商品信息，请核对修改。",!0):this.feedback("已自动提取商品信息，请核对后保存。")}catch(v){this.current(h)&&this.feedback(`提取失败：${v.message||"未能解析"}，请手动填写。`,!0)}finally{u.isConnected&&(u.disabled=!1,u.textContent="自动提取")}}),t.querySelectorAll("[data-ops-quick-list]").forEach(c=>c.onclick=async()=>{const m=c.dataset.opsQuickList,h=c.dataset.name||"商品",v=this.capture();c.disabled=!0,c.textContent="草稿生成中…",this.feedback(`正在为【${h}】生成 Listing 草稿…`);try{const g=await this.api(this.url(`candidates/${m}/quick-list`,v),{method:"POST",body:"{}"});if(!this.current(v))return;const w=g.degraded?"降级模板":g.source==="mock"?"Mock 模板":`模型 ${g.source||"未知"}`,k=g.status==="needs_revision";this.feedback(`【${g.title}】草稿已生成（来源：${w}）。${k?"质检未通过，请修改候选商品事实后重新生成。":"请前往审批页查看内容后，由 owner 人工审批。"}`,k),c.textContent=k?"需修改":"待审批"}catch(g){this.current(v)&&this.feedback(`生成失败：${g.message||"未能处理"}`,!0),c.disabled=!1,c.textContent="生成 Listing 草稿"}}),t.querySelectorAll("[data-ops-research]").forEach(c=>c.onclick=async()=>{const m=c.dataset.opsResearch,h=c.dataset.name||"商品",v=this.capture();c.disabled=!0,c.textContent="研究建议中…",this.feedback(`正在为【${h}】生成 AI 研究建议…`);try{const g=await this.api(this.url("work-model-runs",v),{method:"POST",body:JSON.stringify({module:"research",object_id:m,idempotency_key:`research-${m}-${Date.now()}`})});if(!this.current(v))return;this.candidateSuggestions||(this.candidateSuggestions={}),this.candidateSuggestions[m]=g,this.feedback(`【${h}】研究建议生成完成，请预览并决定是否采纳。`),e(!1)}catch(g){this.current(v)&&this.feedback(`生成失败：${g.message||"未能处理"}`,!0),c.disabled=!1,c.textContent="研究建议"}}),t.querySelectorAll("[data-ops-verify]").forEach(c=>c.onclick=async()=>{var g;const m=c.dataset.opsVerify,h=c.dataset.name||"商品",v=this.capture();c.disabled=!0,c.textContent="事实核验中…",this.feedback(`正在核验【${h}】的商品事实…`);try{const w=await this.api(this.url("work-model-runs",v),{method:"POST",body:JSON.stringify({module:"verify",object_id:m,idempotency_key:`verify-${m}-${Date.now()}`})});if(!this.current(v))return;this.candidateSuggestions||(this.candidateSuggestions={}),this.candidateSuggestions[m]=w;const k=((g=w.output)==null?void 0:g.decision)==="pass"?"通过":"不通过";this.feedback(`【${h}】事实核验完成（${k}），请核对声明与证据来源。`),e(!1)}catch(w){this.current(v)&&this.feedback(`核验失败：${w.message||"未能处理"}`,!0),c.disabled=!1,c.textContent="核验事实"}}),t.querySelectorAll("[data-ops-adopt-run]").forEach(c=>c.onclick=async()=>{const m=c.dataset.opsAdoptRun,h=c.dataset.candId,v=c.dataset.version,g=this.capture();c.disabled=!0,c.textContent="采纳中…",this.feedback("正在采纳分析结果并更新商品事实…");try{const w=await this.api(this.url(`candidates/${h}/adopt-run`,g),{method:"POST",body:JSON.stringify({run_id:m,expected_version_digest:v})});if(!this.current(g))return;if(this.candidateSuggestions&&delete this.candidateSuggestions[h],this.data.candidates){const k=this.data.candidates.findIndex(I=>I.id===h);k>=0&&(this.data.candidates[k]=w)}this.feedback("分析结果已成功采纳至商品候选事实。"),e(!1)}catch(w){this.current(g)&&this.feedback(`采纳失败：${w.message||"未能处理"}`,!0),c.disabled=!1,c.textContent="采纳分析结果"}}),t.querySelectorAll("[data-ops-write-copy]").forEach(c=>c.onclick=async()=>{const m=c.dataset.opsWriteCopy,h=c.dataset.name||"商品",v=this.capture();c.disabled=!0,c.textContent="文案生成中…",this.feedback(`正在为【${h}】生成泰英文案（需先有当前版本的有效核验）…`);try{const g=await this.api(this.url("work-model-runs",v),{method:"POST",body:JSON.stringify({module:"write_copy",object_id:m,idempotency_key:`copy-${m}-${Date.now()}`})});if(!this.current(v))return;this.copyState||(this.copyState={}),this.copyState[m]={copy:g},g.status!=="succeeded"?this.feedback(`文案生成未通过：${g.failure_code||"未知原因"}`,!0):this.feedback(`【${h}】文案已生成，请预览后发起质检。`),e(!1)}catch(g){this.current(v)&&this.feedback(`文案生成失败：${g.message||"未能处理"}`,!0),c.disabled=!1,c.textContent="生成泰英文案"}}),t.querySelectorAll("[data-ops-quality-check]").forEach(c=>c.onclick=async()=>{var w,k;const m=c.dataset.opsQualityCheck,h=c.dataset.candId,v=c.dataset.name||"商品",g=this.capture();c.disabled=!0,c.textContent="质检中…",this.feedback(`正在质检【${v}】的文案…`);try{const I=await this.api(this.url("work-model-runs",g),{method:"POST",body:JSON.stringify({module:"quality_check",object_id:h,idempotency_key:`quality-${m}-${Date.now()}`,scope:{copy_run_id:m}})});if(!this.current(g))return;this.copyState||(this.copyState={});const N=this.copyState[h]||{};this.copyState[h]={...N,quality:I},I.status!=="succeeded"?this.feedback(`质检未通过：${I.failure_code||"未知原因"}`,!0):((w=I.output)==null?void 0:w.decision)!=="pass"?this.feedback(`质检结论为 ${((k=I.output)==null?void 0:k.decision)||"未知"}，请按问题清单修改后重新质检。`,!0):this.feedback(`【${v}】质检通过，可由人工预览后采纳为可用素材。`),e(!1)}catch(I){this.current(g)&&this.feedback(`质检失败：${I.message||"未能处理"}`,!0),c.disabled=!1,c.textContent="质检这份文案"}}),t.querySelectorAll("[data-ops-adopt-asset]").forEach(c=>c.onclick=async()=>{var g,w;const m=c.dataset.opsAdoptAsset,h=c.dataset.candId,v=this.capture();c.disabled=!0,c.textContent="采纳中…",this.feedback("正在把质检通过的文案采纳为可用素材…");try{const k=await this.api(this.url(`candidates/${h}/adopt-asset-run`,v),{method:"POST",body:JSON.stringify({quality_run_id:m})});if(!this.current(v))return;this.feedback(k.replayed?"该质检结果此前已采纳，已回放原素材。":"文案已采纳为可用素材，素材中心可预览。"),(g=this.copyState)!=null&&g[h]&&delete this.copyState[h].quality,(w=this.copyState)!=null&&w[h]&&!this.copyState[h].quality&&!this.copyState[h].copy&&delete this.copyState[h],await e()}catch(k){this.current(v)&&this.feedback(`采纳失败：${k.message||"未能处理"}`,!0),c.disabled=!1,c.textContent="采纳为可用素材"}}),t.querySelectorAll("[data-ops-plan]").forEach(c=>c.onclick=async()=>{const m=c.dataset.opsPlan,h=c.dataset.name||"商品",v=this.capture();c.disabled=!0,c.textContent="计划建议中…",this.feedback(`正在为【${h}】生成本地投放计划建议…`);try{let g=this.data.assets||[];if(!g.length){const I=await this.api(this.url("assets?limit=50&offset=0",v));if(!this.current(v))return;g=I.items||[]}const w=g.filter(I=>I.candidate_id===m).map(I=>I.id),k=await this.api(this.url("work-model-runs",v),{method:"POST",body:JSON.stringify({module:"prepare_campaign",object_id:m,idempotency_key:`plan-${m}-${Date.now()}`,scope:{asset_ids:w}})});if(!this.current(v))return;this.planState||(this.planState={}),this.planState[m]={run:k},k.status!=="succeeded"?this.feedback(`计划建议未通过：${k.failure_code||"未知原因"}`,!0):this.feedback(`【${h}】计划建议已生成，请预览后决定是否采纳。`),e(!1)}catch(g){this.current(v)&&this.feedback(`计划建议失败：${g.message||"未能处理"}`,!0),c.disabled=!1,c.textContent="生成投放计划建议"}}),t.querySelectorAll("[data-ops-adopt-campaign]").forEach(c=>c.onclick=async()=>{var g,w;const m=c.dataset.opsAdoptCampaign,h=c.dataset.candId,v=this.capture();c.disabled=!0,c.textContent="采纳中…",this.feedback("正在把计划建议采纳为本地草稿计划…");try{const k=await this.api(this.url(`candidates/${h}/adopt-campaign-run`,v),{method:"POST",body:JSON.stringify({run_id:m})});if(!this.current(v))return;this.feedback(k.replayed?"该计划建议此前已采纳，已回放原计划。":"已保存为本地草稿计划；执行说明需人工在平台操作后回填。"),(g=this.planState)!=null&&g[h]&&delete this.planState[h].run,(w=this.planState)!=null&&w[h]&&!this.planState[h].run&&delete this.planState[h],await e()}catch(k){this.current(v)&&this.feedback(`采纳失败：${k.message||"未能处理"}`,!0),c.disabled=!1,c.textContent="采纳为本地计划（草稿）"}}),t.querySelectorAll("[data-ops-review]").forEach(c=>c.onclick=async()=>{var g,w,k;const m=c.dataset.opsReview,h=c.dataset.name||"计划",v=this.capture();c.disabled=!0,c.textContent="复盘中…",this.feedback(`正在按数据库原始指标复盘【${h}】…`);try{const I=await this.api(this.url("work-model-runs",v),{method:"POST",body:JSON.stringify({module:"record_metrics",object_id:m,idempotency_key:`review-${m}-${Date.now()}`,scope:{from:((g=this.filters)==null?void 0:g.start_date)||void 0,to:((w=this.filters)==null?void 0:w.end_date)||void 0,source:((k=this.filters)==null?void 0:k.source)||void 0}})});if(!this.current(v))return;this.reviewState||(this.reviewState={}),this.reviewState[m]={run:I},I.status!=="succeeded"?this.feedback(`复盘未通过：${I.failure_code||"未知原因"}`,!0):this.feedback(`【${h}】复盘完成，比率由服务端按原始指标重算。`),e(!1)}catch(I){this.current(v)&&this.feedback(`复盘失败：${I.message||"未能处理"}`,!0),c.disabled=!1,c.textContent="复盘这份数据"}});const O=t.querySelector("#ops-draft-import");O&&(O.onsubmit=async c=>{c.preventDefault();const m=this.capture(),h=Object.fromEntries(new FormData(O));h.candidate_id||(h.candidate_id=null);const v=O.querySelector("[type=submit]");v.disabled=!0;try{await this.api(this.url("assets/from-draft",m),{method:"POST",body:JSON.stringify(h)}),this.current(m)&&(this.note="AI 草稿已导入素材，保留原始来源。",await e())}catch(g){this.current(m)&&this.feedback(g.message,!0)}finally{this.current(m)&&v.isConnected&&(v.disabled=!1)}});const T=t.querySelector("#ops-import");T&&(T.onsubmit=async c=>{c.preventDefault();const m=this.capture(),h=new FormData(T),v=T.querySelector("button");v.disabled=!0;let g=0;try{const w=JSON.parse(h.get("rows"));if(!Array.isArray(w)||!w.length||w.length>100)throw Error("请提供 1–100 行 JSON 数组");for(const k of w){if(!this.current(m))return;await this.api(this.url("metrics",m),{method:"POST",body:JSON.stringify({...k,campaign_id:h.get("campaign_id")})}),g++}this.current(m)&&(this.note=`已导入 ${g} 行。`,await e())}catch(w){this.current(m)&&this.feedback(`已保存 ${g} 行，第 ${g+1} 行失败：${w.message}。修正后可重新导入。`,!0)}finally{this.current(m)&&v.isConnected&&(v.disabled=!1)}})}}async function Ot(s){if(!["image/png","image/jpeg","image/webp"].includes(s.type))throw Error("仅接受 PNG、JPEG、WebP 图片");if(s.size>5*1024*1024)throw Error("图片不能超过 5 MiB");const t=new Uint8Array(await s.arrayBuffer());let e="";for(let a=0;a<t.length;a+=8192)e+=String.fromCharCode(...t.subarray(a,a+8192));return btoa(e)}function vt(s,t){const e=s.map(a=>[a.id,a.name]);return t&&!s.some(a=>a.id===t)&&e.unshift([t,`当前关联（列表外） ${t}`]),e}function Xt(s,t,e){const a=new Set(t.map(r=>r.id));return[...new Set([...s.filter(r=>!a.has(r)),...e])]}function Yt(){const s=document.cookie.match(/(?:^|;\s*)sk_csrf=([^;]+)/);return s?decodeURIComponent(s[1]):""}async function S(s,t={}){const e=Object.assign({Accept:"application/json"},t.headers||{}),a=(t.method||"GET").toUpperCase();a!=="GET"&&a!=="HEAD"&&(e["X-CSRF-Token"]=Yt()||window.__CSRF__||"",t.body&&!e["Content-Type"]&&(e["Content-Type"]="application/json"));const r=await fetch(s,{credentials:"same-origin",...t,method:a,headers:e}),o=await r.text();let d=null;try{d=o?JSON.parse(o):null}catch{d={detail:o}}if(!r.ok){const l=d&&d.detail?d.detail:r.statusText,b=new Error(typeof l=="string"?l:JSON.stringify(l));throw b.status=r.status,b.data=d,b}return d}function Zt(s,t){return S("/api/auth/login",{method:"POST",body:JSON.stringify({username:s,password:t})})}function te(){return S("/api/auth/logout",{method:"POST",body:"{}"})}function ee(){return S("/api/auth/me")}const B=s=>String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");function bt(s){var a,r,o,d,l,b,f;const t=s==null?void 0:s.dropship,e=s==null?void 0:s.tracking;if(((a=e==null?void 0:e.draft)==null?void 0:a.status)==="executed")return"complete";if(!(t!=null&&t.draft))return"prepare";if(t.draft.status==="awaiting_manual_po")return"manual_po";if(t.draft.status!=="executed")return((r=t.approval)==null?void 0:r.status)==="executing"?"executing":((o=t.approval)==null?void 0:o.status)==="pending"?"approve_dropship":((d=t.approval)==null?void 0:d.status)==="approved"?"execute_dropship":"prepare";if(!(e!=null&&e.draft)){const u=t.draft.result||{};return u.carrier&&u.tracking_no?"prepare_tracking":"tracking_details"}return((l=e.approval)==null?void 0:l.status)==="executing"?"executing":((b=e.approval)==null?void 0:b.status)==="pending"?"approve_tracking":((f=e.approval)==null?void 0:f.status)==="approved"?"push_tracking":"tracking_details"}class se{constructor(t){this.request=t,this.orderId="",this.generation=0,this.role="viewer",this.busy=!1,this.data=null,this.error="",this.form={},this.sheet="",this.shopId="default"}clear(){this.generation+=1,this.orderId="",this.data=null,this.sheet="",this.form={},this.busy=!1,this.error="",this.paint()}attach(t,e){this.host=t,this.role=e||"viewer",this.paint()}current(t,e){return this.orderId===t&&this.generation===e}path(t,e){const a=encodeURIComponent(this.shopId||"default");return`/api/workflow/orders/${encodeURIComponent(t)}/${e}?shop_id=${a}`}async read(t,e){const a=async d=>{try{return await this.request(this.path(t,d))}catch(l){if(l.status===404)return null;throw l}},[r,o]=await Promise.all([a("dropship"),a("tracking")]);return this.current(t,e)?(this.data={dropship:r,tracking:o},!0):!1}async open(t){this.clear(),this.orderId=t;const e=this.generation;this.busy=!0,this.error="",this.paint();try{await this.read(t,e)}catch(a){this.current(t,e)&&(this.error=a.message)}finally{this.current(t,e)&&(this.busy=!1,this.paint())}}async post(t,e,a={}){return this.request(this.path(t,e),{method:"POST",body:JSON.stringify(a)})}async advance(t,e){for(let a=0;a<4;a+=1){if(!await this.read(t,e))return;const r=bt(this.data),o={prepare:"dropship/draft",execute_dropship:"dropship/execute",prepare_tracking:"tracking/draft",push_tracking:"tracking/push"}[r];if(!o)return;await this.post(t,o)}throw new Error("流程状态未推进，请刷新后核对执行结果。")}async act(t,e={}){var o;if(this.busy||!this.orderId||t!=="refresh"&&this.role==="viewer"||(t==="approve"||t==="reject")&&this.role!=="owner")return;const a=this.orderId,r=this.generation;this.busy=!0,this.error="",this.form={...this.form,...e},this.paint();try{if(!await this.read(a,r))return;if(t==="approve"||t==="reject"){const d=bt(this.data),l=d==="approve_dropship"?this.data.dropship.approval:d==="approve_tracking"?this.data.tracking.approval:null;if(!l||l.id!==e.approvalId)throw new Error("审批内容或状态已变化，请重新核对后操作。");if(t==="reject"&&!((o=e.note)!=null&&o.trim()))throw new Error("请填写驳回原因。");await this.request(`/api/workflow/approvals/${encodeURIComponent(l.id)}/${t}`,{method:"POST",body:JSON.stringify({note:e.note||"订单履约工作台批准并继续",expected_hash:l.payload_hash||""})}),t==="approve"&&this.current(a,r)&&await this.advance(a,r)}else if(t==="continue")await this.advance(a,r);else if(t==="record_po")await this.post(a,"dropship/record-po",{alibaba_order_id:e.alibaba_order_id}),this.current(a,r)&&await this.advance(a,r);else if(t==="tracking")await this.post(a,"tracking/draft",{carrier:e.carrier,tracking_no:e.tracking_no});else if(t==="sheet"){const d=await this.request(this.path(a,"dropship/manual-sheet"));this.current(a,r)&&(this.sheet=d.copy_text||"")}await this.read(a,r)}catch(d){if(this.current(a,r)){this.error=d.message||"处理失败，请核对服务器执行结果。";try{await this.read(a,r)}catch{}}}finally{this.current(a,r)&&(this.busy=!1,this.paint())}}paint(){var t,e,a;(t=this.host)!=null&&t.isConnected&&(this.host.innerHTML=this.html(),this.host.querySelectorAll("[data-order-action]").forEach(r=>{r.onclick=()=>{var o;return this.act(r.dataset.orderAction,{approvalId:r.dataset.approvalId,note:((o=this.host.querySelector("[name=order_note]"))==null?void 0:o.value)||""})}}),(e=this.host.querySelector("#order-po-form"))==null||e.addEventListener("submit",r=>{r.preventDefault(),this.act("record_po",Object.fromEntries(new FormData(r.currentTarget)))}),(a=this.host.querySelector("#order-tracking-form"))==null||a.addEventListener("submit",r=>{r.preventDefault(),this.act("tracking",Object.fromEntries(new FormData(r.currentTarget)))}))}html(){var _,$,y,A,q;if(!this.orderId)return'<p class="muted">选择订单后可查看代发与物流处理进度。</p>';const t=bt(this.data),e=(_=this.data)==null?void 0:_.dropship,a=($=this.data)==null?void 0:$.tracking,r=t==="approve_dropship"?e.approval:t==="approve_tracking"?a.approval:null,o={prepare:"准备代发草稿",approve_dropship:"等待代发审批",execute_dropship:"代发已批准，可继续",manual_po:"等待人工回填采购单",prepare_tracking:"准备物流草稿",tracking_details:"等待填写物流信息",approve_tracking:"等待物流审批",push_tracking:"物流已批准，可继续",executing:"服务器正在执行，请刷新核对",complete:"物流信息处理完成"},d=this.busy||this.role==="viewer"?"disabled":"",l=(R,M,z="",P="secondary")=>`<button type="button" class="btn ${P}" data-order-action="${R}" ${d} ${z}>${M}</button>`,b=R=>{var z,P,W;const M=(z=R==null?void 0:R.draft)==null?void 0:z.result;return M&&(M.code||M.message)?[M.code,M.message].filter(Boolean).join(" "):(P=R==null?void 0:R.approval)!=null&&P.execution_error?`执行失败：${R.approval.execution_error}`:((W=R==null?void 0:R.approval)==null?void 0:W.status)==="rejected"?`已驳回：${R.approval.decision_note||"未填写原因"}`:""},f=b(a)||b(e),u=[this.error,f].filter(Boolean).filter((R,M,z)=>z.indexOf(R)===M).join("；"),O={prepare:"从已同步订单生成代发草稿，生成后仍需店主审批。",approve_dropship:"代发草稿已生成，请核对供应商、数量与收货信息后审批。",execute_dropship:"代发已批准，可执行本地写入或打开人工下单信息。",manual_po:"在 1688 人工下单后，把采购单号回填到这里；系统不代付。",prepare_tracking:"代发结果已就绪，请核对承运商与物流单号后生成物流草稿。",tracking_details:"请填写真实承运商与物流单号；不得使用演示数据冒充平台事实。",approve_tracking:"物流草稿已生成，请核对后审批；批准后才会写平台。",push_tracking:"物流已批准，可执行平台写入；失败原因会保留在服务器。",executing:"服务器正在执行外部写，请刷新核对结果，不要重复点击。",complete:"服务器已有物流处理记录；这不表示平台已发货或已签收。"},T=["prepare","approve_dropship","execute_dropship"].includes(t)?1:["manual_po","prepare_tracking","tracking_details","approve_tracking","push_tracking"].includes(t)?2:t==="complete"?3:1,m=[{label:"订单同步",hint:"已同步"},{label:"代发审批",hint:((y=e==null?void 0:e.draft)==null?void 0:y.status)==="executed"?"已完成":"当前"},{label:"物流审批",hint:t==="complete"?"已完成":T===2?"当前":"待处理"},{label:"完成",hint:t==="complete"?"已完成":"待处理"}].map((R,M)=>`<li class="wb-step ${M<T||t==="complete"?"done":M===T?"current":""}"><span class="wb-step-index">${M+1}</span><span><strong>${R.label}</strong><small>${R.hint}</small></span></li>`).join(""),h=this.busy?"正在读取或处理…":this.data?o[t]:"未能读取处理进度",v=t==="complete"?"ok":t==="executing"?"warn":"pending",g=e==null?void 0:e.draft,w=a==null?void 0:a.draft,k=(w==null?void 0:w.payload)||{},I=(a==null?void 0:a.shipment)||{},N=r?`<section class="wb-card wb-approval">
          <div class="wb-card-head"><div><span class="wb-kicker">待人工审批</span><h3>${t==="approve_tracking"?"物流信息":"代发草稿"}</h3></div><span class="status-pill ${v}">${B(r.status||"pending")}</span></div>
          <p class="wb-approval-summary">请核对本次${t==="approve_tracking"?`物流：${B((A=r.payload)==null?void 0:A.carrier)} / ${B((q=r.payload)==null?void 0:q.tracking_no)}`:"代发草稿"}。</p>
          <details class="wb-details"><summary>查看审批内容</summary><pre>${B(JSON.stringify(r.payload||{},null,2))}</pre></details>
          ${this.role==="owner"?`<label class="wb-field">审批说明 / 驳回原因<input name="order_note" placeholder="驳回时必填" /></label>
               <div class="wb-action-row">${l("approve","批准并继续",`data-approval-id="${B(r.id)}"`,"")} ${l("reject","驳回",`data-approval-id="${B(r.id)}"`,"danger")}</div>`:'<p class="muted">等待店主审批；批准后可继续处理。</p>'}
        </section>`:"",x=t==="manual_po"?`<section class="wb-card">
          <div class="wb-card-head"><div><span class="wb-kicker">人工步骤</span><h3>回填 1688 采购单</h3></div>${l("sheet","查看人工下单信息","","ghost")}</div>
          <p class="muted">先在 1688 由人完成下单，再回填采购单号或订单链接。本后台不会自动付款。</p>
          <form id="order-po-form" class="wb-form"><label class="wb-field">1688 采购单号或订单链接<input name="alibaba_order_id" required placeholder="例如 1234567890" value="${B(this.form.alibaba_order_id||"")}" /></label><button class="btn" ${d}>保存采购单并继续</button></form>
        </section>`:"",C=(g==null?void 0:g.status)==="executed"&&!["complete","executing"].includes(t)?`<section class="wb-card">
          <div class="wb-card-head"><div><span class="wb-kicker">人工填写</span><h3>物流信息</h3></div></div>
          <p class="muted">请填写真实承运商与物流单号。保存后生成待审批草稿，批准后才会写平台。</p>
          <form id="order-tracking-form" class="wb-form wb-form-grid">
            <label class="wb-field">承运商<input name="carrier" required placeholder="例如 JNT" value="${B(this.form.carrier??k.carrier??"")}" /></label>
            <label class="wb-field">物流单号<input name="tracking_no" required placeholder="例如 JT123456789012" value="${B(this.form.tracking_no??k.tracking_no??"")}" /></label>
            <button class="btn secondary" ${d}>保存物流草稿，核对后审批</button>
          </form>
        </section>`:"",G=["prepare","execute_dropship","prepare_tracking","push_tracking"].includes(t)&&this.role!=="viewer"?`<div class="wb-action-row wb-primary-action">${l("continue","推进到下一审批或人工步骤","","")}</div>`:"",at=this.sheet?`<section class="wb-card"><div class="wb-card-head"><div><span class="wb-kicker">本次查看</span><h3>人工下单信息</h3></div></div><label class="wb-field">复制以下信息到 1688 下单<textarea readonly rows="8">${B(this.sheet)}</textarea></label></section>`:"",ct=t==="complete"?`<section class="wb-card wb-complete"><div class="wb-card-head"><div><span class="wb-kicker">已完成</span><h3>物流信息处理完成</h3></div><span class="status-pill ok">completed</span></div><dl class="wb-facts"><div><dt>承运商</dt><dd>${B(I.carrier||k.carrier||"-")}</dd></div><div><dt>运单号</dt><dd class="mono">${B(I.tracking_no||k.tracking_no||"-")}</dd></div></dl><p class="muted">此进度表示服务器已有物流处理记录，不表示已发货或已签收。发货与退款请在 TikTok Shop 卖家中心由人操作。</p></section>`:"",lt=`<aside class="wb-rail">
      <section class="wb-rail-card"><span class="wb-kicker">当前记录</span><dl class="wb-facts">
        <div><dt>代发</dt><dd><span class="status-dot ${(g==null?void 0:g.status)==="executed"?"ok":""}"></span>${B((g==null?void 0:g.status)||"未开始")}</dd></div>
        <div><dt>物流</dt><dd><span class="status-dot ${(w==null?void 0:w.status)==="executed"?"ok":""}"></span>${B((w==null?void 0:w.status)||"未开始")}</dd></div>
        <div><dt>承运商</dt><dd>${B(I.carrier||k.carrier||"-")}</dd></div>
        <div><dt>运单号</dt><dd class="mono">${B(I.tracking_no||k.tracking_no||"-")}</dd></div>
      </dl></section>
      <section class="wb-rail-card wb-guard"><span class="wb-kicker">边界</span><p>代发与物流都必须先过审批。本后台不自动下单、不付款、不发货、不退款。</p><p class="muted">外部写失败不会被静默重试；失败原因以服务器记录为准。</p></section>
      ${this.role==="viewer"?'<p class="muted">当前为只读权限。</p>':""}
    </aside>`;return`<section class="panel wb-panel" aria-label="订单履约工作台">
      <header class="wb-head"><div><span class="wb-kicker">订单详情</span><h2>${B(this.orderId)}</h2><p class="wb-sub">审批驱动的代发与物流处理</p></div><div class="wb-head-right"><span class="status-pill ${v}">${h}</span><button type="button" class="btn secondary" data-order-action="refresh" ${this.busy?"disabled":""}>刷新进度</button></div></header>
      <ol class="wb-steps">${m}</ol>
      <div class="wb-body">
        <div class="wb-main">
          <div class="wb-status ${v}"><span class="wb-status-mark"></span><div><strong role="status">${h}</strong><p>${O[t]||""}</p></div></div>
          ${u?`<p class="error" role="alert">${B(u)}</p>`:""}
          ${G}
          ${N}
          ${x}
          ${C}
          ${at}
          ${ct}
        </div>
        ${lt}
      </div>
    </section>`}}const et=new se(S),J={overview:'<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',workflows:'<svg viewBox="0 0 24 24"><path d="M4 6h10"/><path d="M4 12h16"/><path d="M4 18h8"/><circle cx="18" cy="6" r="2"/><circle cx="10" cy="18" r="2"/></svg>',runs:'<svg viewBox="0 0 24 24"><path d="M8 6h12"/><path d="M8 12h12"/><path d="M8 18h12"/><circle cx="4.2" cy="6" r="1.3"/><circle cx="4.2" cy="12" r="1.3"/><circle cx="4.2" cy="18" r="1.3"/></svg>',agents:'<svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="2.4"/><circle cx="16" cy="8" r="2.4"/><path d="M4.2 18.2c.7-2.2 2.2-3.3 3.8-3.3s3.1 1.1 3.8 3.3"/><path d="M12.2 18.2c.7-2.2 2.2-3.3 3.8-3.3s3.1 1.1 3.8 3.3"/></svg>',scout:'<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><path d="M20 20l-3.5-3.5"/></svg>',products:'<svg viewBox="0 0 24 24"><path d="M4 8l8-4 8 4-8 4-8-4z"/><path d="M4 12l8 4 8-4"/><path d="M4 16l8 4 8-4"/></svg>',approvals:'<svg viewBox="0 0 24 24"><path d="M8 6h11"/><path d="M8 12h11"/><path d="M8 18h11"/><path d="M4 6h.01"/><path d="M4 12h.01"/><path d="M4 18h.01"/></svg>',orders:'<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h8"/><path d="M8 13h5"/></svg>',reports:'<svg viewBox="0 0 24 24"><path d="M5 19V9"/><path d="M10 19V5"/><path d="M15 19v-7"/><path d="M20 19V8"/></svg>',profit:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 7v10"/><path d="M9.4 9.3c.7-1 1.7-1.5 2.6-1.5 1.4 0 2.4.8 2.4 2 0 2.4-4 1.6-4 3.8 0 1.1.9 2 2.4 2 .9 0 1.9-.4 2.6-1.3"/></svg>',settings:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4"/></svg>',cs:'<svg viewBox="0 0 24 24"><path d="M5 6.5A2.5 2.5 0 0 1 7.5 4h9A2.5 2.5 0 0 1 19 6.5v7A2.5 2.5 0 0 1 16.5 16H9l-4 3.2V6.5z"/></svg>',outreach:'<svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="2.2"/><path d="M4.2 16.8c.6-2 2-3 3.8-3s3.2 1 3.8 3"/><path d="M14 6.5h6.2v5.2H16L14 14.2V6.5z"/></svg>',moon:'<svg viewBox="0 0 24 24"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 7 7 0 1 0 20 14.5z"/></svg>',sun:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M17.7 6.3l1.4-1.4M4.9 19.1l1.4-1.4"/></svg>',user:'<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.5"/><path d="M5 19c1.5-3 4-4.5 7-4.5s5.5 1.5 7 4.5"/></svg>',check:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9"/></svg>',logo:'<svg viewBox="0 0 24 24"><path d="M5 16l7-12 7 12H5z"/><path d="M9 16l3 5 3-5"/></svg>'},qt=[{label:"日常运营",items:Object.entries(Y).map(([s,t])=>({id:s,label:t}))},{label:"AI 与系统工具",items:[{id:"overview",label:"系统诊断"},{id:"workflows",label:"AI 工作流"},{id:"runs",label:"编排运行"},{id:"agents",label:"AI 代理"},{id:"scout",label:"选品"},{id:"spotcheck",label:"人工抽检"},{id:"products",label:"商品"},{id:"profit",label:"利润"}]},{label:"治理",items:[{id:"approvals",label:"审批"},{id:"cs",label:"客服"},{id:"outreach",label:"达人建联"},{id:"orders",label:"订单"},{id:"reports",label:"报表"},{id:"settings",label:"设置"}]}],Mt=qt.flatMap(s=>s.items),i={user:null,status:null,shop:null,route:"operations",approvals:[],selectedApproval:null,publishPreview:null,runs:[],selectedRun:null,orchRuns:[],orchRun:null,agentStats:null,agentRun:null,agentFrom:"",agentTo:"",error:"",theme:localStorage.getItem("admin-theme")||"light",lang:localStorage.getItem("admin-lang")||"zh",profitShop:"",shops:[],activeShop:"",shopGeneration:0,selectedOrderId:"",orderPlatform:"",profitFrom:"",profitTo:"",profitShops:[],profit:null,adSpendRows:[],csInbox:[],csDrafts:[],csNote:"",csError:"",outreachDrafts:[],outreachNote:"",outreachError:""},ae="https://detail.1688.com/offer/600123456789.html";function n(s){return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}function St(){document.documentElement.setAttribute("data-theme",i.theme==="dark"?"dark":"light")}function ie(){if(location.pathname==="/dashboard/profit")return"profit";const t=(location.pathname.replace(/^\/admin\/?/,"").split("/")[0]||"operations").toLowerCase();return t==="login"?"login":Mt.some(e=>e.id===t)?t:"operations"}function Et(){try{return new URLSearchParams(location.search).get("run")||""}catch{return""}}function ne(){try{return new URLSearchParams(location.search).get("id")||""}catch{return""}}function st(s,t){const e=t&&Object.keys(t).length?`?${new URLSearchParams(t).toString()}`:"",a=s==="login"?"/admin/login":`/admin/${s}${e}`;`${location.pathname}${location.search}`!==a&&history.pushState({},"",a),i.route=s,E()}function re(){const s=Mt.find(t=>t.id===i.route);return s?s.label:"总览"}function j(s,t){return`<div class="empty"><strong>${n(s)}</strong>${n(t)}</div>`}function ht(){St();const s=document.getElementById("app");s.innerHTML=`
    <div class="login-page">
      <form class="login-card" id="login-form">
        <div class="brand">
          <div class="brand-logo">${J.logo}</div>
          <div class="brand-text">
            <div class="brand-name">AI 店长</div>
            <p class="brand-sub">小团队运营助手</p>
          </div>
        </div>
        <h1>登录</h1>
        <p class="lead">小团队运营助手 · 辅助选品、素材整理、投放计划与数据复盘。交易与履约在平台完成。</p>
        ${i.error?`<p class="error">${n(i.error)}</p>`:""}
        <div class="field"><label>用户名</label><input name="username" autocomplete="username" required /></div>
        <div class="field"><label>密码</label><input name="password" type="password" autocomplete="current-password" required /></div>
        <button class="btn" type="submit" style="width:100%">登录</button>
        <p class="muted" style="margin:14px 0 0;font-size:11px">开发自测页仍在 <a href="/workflow">/workflow</a>。</p>
      </form>
    </div>
  `,document.getElementById("login-form").onsubmit=async t=>{t.preventDefault();const e=new FormData(t.target);i.error="";try{const a=await Zt(String(e.get("username")||""),String(e.get("password")||""));i.user=a.user,window.__CSRF__=a.csrf_token||"",st("operations"),await H(),E()}catch(a){i.error=a.message||"登录失败",ht()}}}function oe(){var e;const s=i.shops||[];if(s.length<=1){const a=s[0]||{};return`<span class="capsule" title="当前店铺">${n(a.name||((e=i.shop)==null?void 0:e.name)||"默认店铺")}</span>`}const t=i.activeShop||"";return`<span class="capsule shop-switch">
    <select id="shop-switch" title="切换店铺">${s.map(a=>`<option value="${n(a.shop_id)}" ${a.shop_id===t?"selected":""}>${n(a.name||a.shop_id)}</option>`).join("")}</select>
  </span>`}function It(s){St();const t=i.user||{},e=i.shop||{name:"泰国站 · 家居收纳"},a=i.overview?i.overview.system_ok!==!1:!0,r=qt.map(o=>`
      <div class="nav-group">
        <div class="nav-label">${n(o.label)}</div>
        ${o.items.map(d=>`<a href="/admin/${d.id}" data-nav="${d.id}" class="${i.route===d.id?"active":""}">${J[d.id]||""}<span>${n(d.label)}</span></a>`).join("")}
      </div>`).join("");return`
    <div class="app-shell">
      <aside class="sidebar" id="sidebar">
        <div class="brand">
          <div class="brand-logo">${J.logo}</div>
          <div class="brand-text">
            <div class="brand-name">AI 店长</div>
            <p class="brand-sub">小团队运营助手</p>
          </div>
        </div>
        <nav class="nav">${r}</nav>
        <div class="sidebar-foot">店铺 ${n(e.name||e.id||"-")}<br/>发货 / 退款请到卖家中心</div>
      </aside>
      <div class="nav-scrim" id="nav-scrim"></div>
      <div class="main">
        <header class="topbar">
          <div class="topbar-left">
            <button class="capsule nav-toggle" type="button" id="btn-menu" aria-label="打开导航" aria-controls="sidebar" aria-expanded="false">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
            </button>
            <div class="top-title">${n(re())}</div>
          </div>
          <div class="top-right">
            ${oe()}
            <button class="capsule" type="button" id="btn-theme" title="主题">${i.theme==="dark"?J.sun:J.moon}</button>
            <button class="capsule" type="button" id="btn-lang">${n(i.lang==="en"?"EN":"中文")}</button>
            <span class="capsule"><span class="dot" style="${a?"":"background:var(--bad)"}"></span>${a?"系统正常":"需关注"}</span>
            <button class="capsule user" type="button" id="btn-logout">${J.user}<span>${n(t.username||"-")}</span></button>
          </div>
        </header>
        <main class="content">${s}</main>
      </div>
    </div>
  `}function Ct(){const s=document.querySelector(".app-shell"),t=document.getElementById("btn-menu"),e=document.getElementById("nav-scrim"),a=f=>{s==null||s.classList.toggle("nav-open",f),t==null||t.setAttribute("aria-expanded",f?"true":"false")};t==null||t.addEventListener("click",()=>a(!(s!=null&&s.classList.contains("nav-open")))),e==null||e.addEventListener("click",()=>a(!1)),document.onkeydown=f=>{f.key==="Escape"&&a(!1)};const r={overview:"OPERATIONS",workflows:"ORCHESTRATION",runs:"RUN QUEUE",agents:"AI AGENTS",scout:"SOURCING",spotcheck:"QUALITY",products:"CATALOG",profit:"PROFIT",approvals:"GOVERNANCE",cs:"SUPPORT",outreach:"CREATOR OPS",orders:"FULFILLMENT",reports:"REPORTS",settings:"SETTINGS",records:"MODEL CALLS"};document.querySelectorAll(".page-head").forEach(f=>{const u=f.querySelector(":scope > div:first-child");if(!u||u.querySelector(".eyebrow"))return;const O=document.createElement("div");O.className="eyebrow",O.textContent=r[i.route]||"WORKSPACE",u.prepend(O)}),document.querySelectorAll("[data-nav]").forEach(f=>{f.addEventListener("click",u=>{u.preventDefault(),st(f.getAttribute("data-nav"))})});const o=document.getElementById("shop-switch");o&&(o.onchange=()=>{i.activeShop=o.value,i.shopGeneration+=1,dt.clear(),i.syncedOrders=[],i.sla=null,i.selectedOrderId="",et.clear(),i.route==="orders"&&history.replaceState({},"","/admin/orders");try{window.localStorage.setItem("shopkeeper.activeShop",i.activeShop)}catch{}E()});const d=document.getElementById("btn-theme");d&&(d.onclick=()=>{i.theme=i.theme==="dark"?"light":"dark",localStorage.setItem("admin-theme",i.theme),St(),E()});const l=document.getElementById("btn-lang");l&&(l.onclick=()=>{i.lang=i.lang==="en"?"zh":"en",localStorage.setItem("admin-lang",i.lang),E()});const b=document.getElementById("btn-logout");b&&(b.onclick=async()=>{try{await te()}catch{}i.user=null,st("login")})}async function H(){const[s,t]=await Promise.all([S("/api/admin/overview"),S("/api/admin/status")]);i.overview=s,i.status=t,i.shop=s.shop,await Dt()}async function Dt(){var s;try{const t=await S("/api/workflow/shops");i.shops=t.shops||[]}catch{i.shops=[{shop_id:"default",name:((s=i.shop)==null?void 0:s.name)||"默认店铺"}];return}if(!i.activeShop){try{i.activeShop=window.localStorage.getItem("shopkeeper.activeShop")||""}catch{i.activeShop=""}(i.shops||[]).some(e=>e.shop_id===i.activeShop)||(i.activeShop=(i.shops[0]||{}).shop_id||"")}}function de(s){return s&&s.tone==="bad"?"bad":s&&s.tone==="warn"?"warn":s&&s.tone==="idle"?"idle":s&&s.ok===!1?"bad":""}function At(){const s=i.overview||{},t=s.banner||"系统正常 · 全部组件运行中",e=s.system_ok!==!1,a=s.components||[],r=s.facts||[],o=s.switches||[],d=i.user&&i.user.role!=="viewer",l=[["待审批",s.pending_approvals??0,"八态审批收件箱"],["运行中工作流",s.running_workflows??0,"编排器未终态 run"],["临期订单",s.orders_due_soon??0,"本地 SLA 提醒"],["超时订单",s.orders_overdue??0,"请去卖家中心打单"]],b=s.alerts||[],f=r.length?`<div class="banner-facts">${r.map(u=>`<span class="pill ${u.ok===!1?"bad":""}" title="${n(u.hint||"")}">${n(u.label)} ${n(u.value)}</span>`).join("")}</div>`:"";return`
    <div class="banner ${e?"":"warn"}">${J.check}<div class="banner-body"><span>${n(t)}</span>${f}</div></div>
    <div class="page-head">
      <div>
        <h1>总览</h1>
        <p>组件健康只读自 /healthz、工作流 status 与编排 flows。无数据标缺数据，不编造。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" id="btn-demo-orch" ${d?"":"disabled"} title="${d?"强制 mock，写入本服务工作台库":"只读账号不能触发演示"}">一键 mock 演示</button>
        <button class="btn secondary" type="button" id="btn-refresh-overview">刷新</button>
      </div>
    </div>
    <div class="grid-4" style="margin-bottom:12px">
      ${a.map(u=>`
        <section class="card health-card">
          <div class="health-top">
            <span class="health-dot ${de(u)}"></span>
            <span class="health-name">${n(u.name)}</span>
            <span class="tag-mono">${n(u.tag)}</span>
          </div>
          <div class="health-desc">${n(u.desc||"")}</div>
        </section>`).join("")}
    </div>
    <div class="grid-4">
      ${l.map(([u,O,T])=>`
        <section class="card">
          <h3>${n(u)}</h3>
          <div class="metric">${n(O)}</div>
          <div class="hint">${n(T)}</div>
        </section>`).join("")}
    </div>
    <div class="split" style="margin-top:12px">
      <section class="panel">
        <div class="panel-head"><h2>快捷开关</h2><span class="muted">只读 · 危险项不可在线打开</span></div>
        <div class="panel-body">
          ${o.map(u=>`
            <div class="switch-row">
              <div class="switch-meta">
                <strong>${n(u.name)}</strong>
                <span>${n(u.hint||"")}</span>
              </div>
              <div class="toggle-wrap">
                <span class="tag">${u.on?"已开启":"已关闭"}</span>
                <div class="toggle ${u.on?"on":""}" title="只读"></div>
              </div>
            </div>`).join("")||j("无开关","环境开关会显示在这里。")}
        </div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>最近告警 / 任务失败</h2></div>
        <div class="panel-body">
          ${b.length?`<table><thead><tr><th>类型</th><th>标题</th><th>详情</th><th>时间</th></tr></thead><tbody>
              ${b.map(u=>`<tr>
                  <td>${n(u.kind)}</td><td>${n(u.title)}</td>
                  <td>${n(u.detail)}</td><td>${n(u.at)}</td></tr>`).join("")}
              </tbody></table>`:j("暂无告警","任务失败或审批执行失败会出现在这里。")}
        </div>
      </section>
    </div>
  `}async function kt(s){const t=await S("/api/admin/approvals");i.approvals=t.approvals||[],s?(i.selectedApproval=await S(`/api/admin/approvals/${encodeURIComponent(s)}`),i.publishPreview=await Tt(s)):i.approvals[0]?(i.selectedApproval=await S(`/api/admin/approvals/${encodeURIComponent(i.approvals[0].id)}`),i.publishPreview=await Tt(i.approvals[0].id)):i.selectedApproval=null}async function Tt(s){try{return await S(`/api/admin/approvals/${encodeURIComponent(s)}/publish-preview`)}catch(t){return{error:String(t&&t.message?t.message:t)}}}function ce(s){const t=s&&s.payload&&s.payload.input||{},e=!!(s&&s.platform_product_id);return`
    <h3 style="margin-top:14px">上架必需字段</h3>
    <p class="muted" style="margin:0 0 8px">
      真写上架需要类目、仓库与主图。补齐后草稿会回到「待审」，需重新通过才能上架。
    </p>
    <div class="field"><label>类目 ID（category_id）</label>
      <input id="pi-category" value="${n(t.category_id||"")}" placeholder="例如 600001" /></div>
    <div class="field"><label>仓库 ID（warehouse_id）</label>
      <input id="pi-warehouse" value="${n(t.warehouse_id||"")}" placeholder="例如 1234567890" /></div>
    <div class="field"><label>价格（THB）</label>
      <input id="pi-price" value="${n(t.price_amount||"")}" placeholder="例如 99" /></div>
    <div class="field"><label>本地主图路径（工作目录内）</label>
      <input id="pi-image" value="${n(t.image_path||"")}" placeholder="例如 artifacts/demo/images/x_EN.png" /></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
      <button class="btn" type="button" id="btn-save-inputs">保存上架字段</button>
      ${e?'<span class="muted">该草稿已有平台商品 ID，改动不影响已上架记录</span>':""}
    </div>
    <div id="pi-msg" class="muted" style="margin-top:6px"></div>
  `}function le(s){if(!s)return"";if(s.error)return`<h3 style="margin-top:14px">上架前预览</h3><p class="muted">取不到预览：${n(s.error)}</p>`;const t=(s.warnings||[]).length?`<ul class="compliance">${s.warnings.map(r=>`<li>${n(r)}</li>`).join("")}</ul>`:'<p class="muted">无阻塞项。</p>',e=s.copy||{};return`
    <h3 style="margin-top:14px">上架前预览</h3>
    <p class="muted">写通道 ${s.write_armed?'<span class="tag approved">已武装</span>':'<span class="tag pending">未武装</span>'} · 载荷哈希 ${n(String(s.payload_hash||"").slice(0,12))}…${s.preview_consistent?"":" · <strong>草稿在审批后被改过</strong>"}</p>
    ${t}
    <h3 style="margin-top:12px">将发给平台的文案</h3>
    <div class="diff">
      <div><div class="muted" style="margin-bottom:6px">泰语标题</div><pre>${n(e.title_th||"—")}</pre></div>
      <div><div class="muted" style="margin-bottom:6px">英语标题</div><pre>${n(e.title_en||"—")}</pre></div>
    </div>
    <div class="diff" style="margin-top:8px">
      <div><div class="muted" style="margin-bottom:6px">泰语正文</div><pre>${n(e.body_th||"—")}</pre></div>
      <div><div class="muted" style="margin-bottom:6px">英语正文</div><pre>${n(e.body_en||"—")}</pre></div>
    </div>
    <h3 style="margin-top:12px">图片</h3>
    <div class="images">${(s.images||[]).length?s.images.map(r=>`<img src="${n(r)}" alt="主图" />`).join(""):'<span class="muted">无主图</span>'}</div>
  `}function pe(){const s=i.approvals||[],t=i.selectedApproval,e=i.user&&i.user.role==="owner",a=s.length?`<table><thead><tr><th>动作</th><th>状态</th><th>实体</th><th>创建</th></tr></thead><tbody>
      ${s.map(o=>{const d=t&&t.id===o.id?' style="background:var(--bg)"':"";return`<tr class="clickable" data-approval="${n(o.id)}"${d}>
            <td>${n(o.action_type)}</td>
            <td><span class="tag ${n(o.status)}">${n(o.status)}</span></td>
            <td>${n(o.entity_type)} / ${n(o.entity_id)}</td>
            <td>${n(o.created_at)}</td>
          </tr>`}).join("")}
      </tbody></table>`:j("暂无审批单","生成 listing 草稿后会出现 create_listing 待审单。");let r=j("选择审批单","左侧点一行查看详情。");if(t){const o=t.detail||{},d=(o.images||[]).map(l=>`<img src="${n(l)}" alt="商品图" />`).join("");r=`
      <div class="panel-head">
        <h2>${n(t.action_type)} · ${n(t.status)}</h2>
        <span class="muted">审批人 ${n(t.decided_by||"—")}</span>
      </div>
      <div class="panel-body">
        <p class="muted">实体 ${n(t.entity_type)} / ${n(t.entity_id)} · 载荷哈希 ${n(String(t.payload_hash||"").slice(0,12))}…</p>
        <h3>图片</h3>
        <div class="images">${d||'<span class="muted">无图片</span>'}</div>
        <h3 style="margin-top:14px">改动前后</h3>
        <div class="diff">
          <div><div class="muted" style="margin-bottom:6px">改前</div><pre>${n(JSON.stringify(o.before||{},null,2))}</pre></div>
          <div><div class="muted" style="margin-bottom:6px">改后</div><pre>${n(JSON.stringify(o.after||{},null,2))}</pre></div>
        </div>
        <h3 style="margin-top:14px">合规提示</h3>
        <ul class="compliance">${(o.compliance||[]).map(l=>`<li>${n(l)}</li>`).join("")}</ul>
        ${ce(t)}
        ${le(i.publishPreview)}
        ${t.status==="pending"?`<div style="margin-top:16px">
                <div class="field"><label>决定说明（打回必填）</label>
                <textarea id="decision-note" placeholder="通过可写简短说明；打回必须写原因"></textarea></div>
                <div style="display:flex;gap:8px;flex-wrap:wrap">
                  <button class="btn" type="button" id="btn-approve" ${e?"":"disabled"}>通过</button>
                  <button class="btn danger" type="button" id="btn-reject" ${e?"":"disabled"}>打回</button>
                  ${e?"":'<span class="muted">仅 owner 可审批</span>'}
                </div>
              </div>`:`<p class="muted" style="margin-top:12px">决定说明：${n(t.decision_note||"—")}</p>`}
      </div>
    `}return`
    <div class="page-head">
      <div><h1>审批</h1><p>八态审批收件箱。通过/打回写入审批人账号。</p></div>
    </div>
    <div class="split">
      <section class="panel"><div class="panel-head"><h2>列表</h2></div><div class="panel-body">${a}</div></section>
      <section class="panel" id="approval-detail">${r}</section>
    </div>
  `}function ue(s){const t=String(s.platform_audit_status||"").toUpperCase();if(!t)return s.platform_product_id?'<span class="muted">未查询</span>':'<span class="muted">—</span>';const e=t==="APPROVED"?"approved":t==="FAILED"?"failed":"pending",a=s.platform_audit_reason?` title="${n(s.platform_audit_reason)}"`:"";return`<span class="tag ${e}"${a}>${n(t)}</span>`}function he(){const s=i.catalog||[];return`
    <div class="page-head">
      <div><h1>商品</h1><p>本地商品库（接现有 catalog API）。</p></div>
      <div style="display:flex;gap:8px">
        <a class="btn" href="/admin/candidates" data-nav="candidates">+ 录入候选商品</a>
        <button class="btn secondary" type="button" id="btn-import-demo">导入 Demo 包</button>
      </div>
    </div>
    <section class="panel"><div class="panel-body">
      ${s.length?`<table><thead><tr><th>标题</th><th>审批</th><th>平台商品 ID</th><th>平台审核</th><th>更新</th></tr></thead><tbody>
          ${s.map(t=>`<tr>
              <td>${n(t.title)}</td>
              <td><span class="tag ${n(t.approval_status)}">${n(t.approval_status)}</span></td>
              <td>${n(t.platform_product_id||"—")}</td>
              <td>${ue(t)}</td>
              <td>${n(t.updated_at||t.created_at||"")}</td>
            </tr>`).join("")}
          </tbody></table>`:j("商品库为空","请先在「日常运营 ➔ 候选商品」保存商品事实并生成 Listing 草稿，再前往审批页查看内容并人工审批。")}
    </div></section>
  `}function me(){const s=i.syncedOrders||[],t=i.sla||{},e=[...new Set(s.map(d=>String(d.status||"").trim()).filter(Boolean))],a=[...new Set(s.map(d=>String(d.platform||"tiktok")))],r=(d,l,b,f="")=>`
    <article class="kpi-card ${f}">
      <span class="kpi-label">${n(d)}</span>
      <strong class="kpi-value">${n(l)}</strong>
      <span class="kpi-hint">${n(b)}</span>
    </article>`,o=s.map(d=>{const l=String(d.order_id||d.id||""),b=String(d.status||"未知状态"),f=String(d._synced_at||"—"),u=String(d.platform||"tiktok"),O=u!=="tiktok",T=i.selectedOrderId===l?"is-selected":"",c=O?' disabled title="Shopee 当前只读导入；代发/物流写尚未接入。"':"";return`<button type="button" class="order-row ${T}" data-order-work="${n(l)}" data-order-status="${n(b)}" data-order-platform="${n(u)}" data-order-search="${n(`${l} ${b} ${u} ${f}`.toLowerCase())}"${c}>
        <span class="order-row-avatar">${n(l.slice(-2).toUpperCase()||"?")}</span>
        <span class="order-row-main"><strong>${n(l)}</strong><span class="order-row-meta">${n(u)} · ${n(b)} · ${n(f)}</span></span>
        <span class="order-row-arrow" aria-hidden="true">${O?"只读":"→"}</span>
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
          <button class="btn secondary" type="button" id="btn-sync-orders" ${i.user&&i.user.role==="viewer"?'disabled title="只读账号不能执行同步写操作"':""}>只读同步</button>
        </div>
      </div>
      <div class="orders-safety">代发与物流回填须先过对应审批；本后台不自动下单、不付款、不发货、不退款。发货与退款请在 TikTok Shop 卖家中心由人操作。</div>
      <div class="orders-kpis">
        ${r("临期",t.due_soon??0,"接近履约时限",Number(t.due_soon||0)>0?"is-warn":"")}
        ${r("超时",t.overdue??0,"需优先处理",Number(t.overdue||0)>0?"is-bad":"")}
        ${r("窗口内",t.ok??0,"仍在时限内")}
        ${r("已同步",s.length,i.orderPlatform?`当前：${i.orderPlatform}`:"当前店铺 · 全部平台")}
      </div>
      <div class="orders-workspace">
        <section class="panel order-queue" aria-label="已同步订单">
          <div class="queue-head">
            <div><div class="eyebrow">QUEUE</div><h2>已同步订单</h2><p class="muted">${s.length} 个订单</p></div>
            <div class="queue-tools">
              <input id="order-search" type="search" placeholder="搜索订单号或状态" aria-label="搜索订单" />
              <select id="order-platform-filter" aria-label="按平台筛选"><option value="">全部平台</option>${a.map(d=>`<option value="${n(d)}"${i.orderPlatform===d?" selected":""}>${n(d)}</option>`).join("")}</select>
              <select id="order-status-filter" aria-label="按状态筛选"><option value="">全部状态</option>${e.map(d=>`<option value="${n(d)}">${n(d)}</option>`).join("")}</select>
              <label class="btn secondary" for="shopee-csv" title="Shopee 只读 CSV 导入；不调用平台 API">导入 Shopee CSV</label>
              <input id="shopee-csv" type="file" accept=".csv,text/csv" hidden />
            </div>
          </div>
          <div class="order-list" id="order-list">
            ${o||j("暂无订单","点击「只读同步」从平台适配器拉取（mock/offline 可能为空）。")}
          </div>
        </section>
        <div id="order-workbench" class="order-detail"></div>
      </div>
    </div>
  `}const fe=[{v:"pass",label:"通过"},{v:"fail",label:"不通过"}];function ge(s){const t=s.spotCheck||[],e=s.spotCheckCriteria||[];if(!t.length)return j("暂无可抽检样本","先用 demo 包或生成 listing 草稿，样本会出现在这里。");const a=s.spotCheckCohort||[],r=new Set(a),o=t.filter(f=>r.has(String(f.offer_id||""))).length,d=a.length&&o!==t.length?`<div class="tag failed" style="margin-bottom:8px">当前样本 ${t.length} 个，其中 ${o} 个属于 M1 cohort（共 ${a.length} 个 offerId）—— 映射不上就不是 M1 抽样，本页结论不得当作 M1 验收。</div>`:"",l=e.map(f=>`<th>${n(f.label)}</th>`).join(""),b=t.map((f,u)=>{const O=e.map(T=>`<td class="sc-cell">${fe.map(c=>`<label class="sc-opt"><input type="radio" name="sc-${u}-${n(T.id)}" value="${c.v}" />${c.label}</label>`).join("")}</td>`).join("");return`<tr data-offer="${n(f.offer_id)}" data-title="${n(f.title)}" data-idx="${u}">
        <td>${u+1}</td><td>${n(f.offer_id)}</td><td>${n(f.title)}</td>${O}</tr>`}).join("");return`
    ${d}
    <div class="muted" style="margin-bottom:8px">
      门槛：<strong>十包中至少 ${s.spotCheckThresholdSamples??8} 包四项全部通过</strong>
      （即样本级通过率 ≥ ${((s.spotCheckThreshold??.8)*100).toFixed(0)}%，DESIGN §9 Done 条件 2）。
      某一包挂任一项即整包不通过；逐格比率只作诊断。
    </div>
    <table class="sc-table"><thead><tr><th>#</th><th>offer_id</th><th>输入标题</th>${l}</tr></thead>
    <tbody>${b}</tbody></table>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
      <button class="btn" type="button" id="btn-spot-score">计算通过率</button>
      <button class="btn secondary" type="button" id="btn-spot-export">导出 md</button>
    </div>
    <div id="spot-result" style="margin-top:12px"></div>
  `}async function Ut(){const s=await S("/api/admin/spot-check");i.spotCheck=s.items||[],i.spotCheckCriteria=s.criteria||[],i.spotCheckThreshold=s.threshold??.8,i.spotCheckThresholdSamples=s.threshold_samples??8,i.spotCheckCohort=s.cohort||[],i.spotCheckReport=""}function ve(){const s=[];return document.querySelectorAll("#spot-body tr[data-idx]").forEach(t=>{const e=t.getAttribute("data-idx"),a={};(i.spotCheckCriteria||[]).forEach(r=>{const o=document.querySelector(`input[name="sc-${e}-${r.id}"]:checked`);a[r.id]=o?o.value:"pending"}),s.push({offer_id:t.getAttribute("data-offer")||"",title:t.getAttribute("data-title")||"",scores:a})}),s}async function be(){const s=ve(),t=document.getElementById("spot-result");let e;try{e=await S("/api/admin/spot-check/report",{method:"POST",body:JSON.stringify({rows:s})})}catch(d){let l=[];try{const f=JSON.parse(d&&d.message?d.message:"");f&&Array.isArray(f.errors)&&(l=f.errors)}catch{l=[]}const b=l.length?l.map(f=>`· ${n(f.message||JSON.stringify(f))}`).join("<br>"):n(String(d&&d.message||d));t&&(t.innerHTML=`
        <div><span class="tag failed">报告被拒绝：未生成通过率</span></div>
        <div class="muted" style="margin-top:6px">已选判定仍保留在表格里，修正后可直接重算。</div>
        <div style="margin-top:6px">${b}</div>`);return}i.spotCheckReport=e.report_md||"";const a=e.scores||{},r=a.pass_rate===null||a.pass_rate===void 0?a.status==="no_data"?"无数据（尚未打分）":"未完成（样本未齐或仍有未打分）":`${(a.pass_rate*100).toFixed(1)}%`,o=a.meets_threshold===null||a.meets_threshold===void 0?"":a.meets_threshold?'<span class="tag approved">达标</span>':'<span class="tag failed">未达标</span>';t&&(t.innerHTML=`
      <div><strong>通过率（样本级）：${n(r)}</strong> ${o}</div>
      <div class="muted">整包四项全过 ${a.whole_sample_passed??0}/${a.cohort_size??10}；提交样本 ${a.samples??0} 个（命中 cohort ${a.cohort_matched??0} 个）</div>
      <div class="muted">逐格诊断：已打分 ${a.scored??0} 格（通过 ${a.passed??0} / 不通过 ${a.failed??0}），未打分 ${a.pending??0} 格 —— 不是 M1 通过率。</div>`)}function $e(){return`
    <div class="page-head">
      <div><h1>人工抽检</h1><p>四判据逐条人工判定；自动计算通过率并可导出报告。</p></div>
      <button class="btn secondary" type="button" id="btn-spot-reload">刷新样本</button>
    </div>
    <section class="panel"><div class="panel-head"><h2>样本</h2></div>
    <div class="panel-body" id="spot-body">${ge(i)}</div></section>
  `}function ye(){const s=(i.drafts||[]).filter(t=>t.kind==="listing"||t.kind==="video_script");return`
    <div class="page-head">
      <div><h1>选品</h1><p>草稿生成与选品摘要（接现有 drafts / jobs API）。</p></div>
      <div style="display:flex;gap:8px">
        <button class="btn secondary" type="button" id="btn-gen-listing">生成 listing 草稿</button>
        <button class="btn secondary" type="button" id="btn-run-research">跑选品摘要</button>
      </div>
    </div>
    <section class="panel"><div class="panel-head"><h2>最近草稿</h2></div><div class="panel-body">
      ${s.length?`<table><thead><tr><th>标题</th><th>类型</th><th>状态</th><th>创建</th></tr></thead><tbody>
          ${s.slice(0,30).map(t=>`<tr>
              <td>${n(t.title)}</td><td>${n(t.kind)}</td>
              <td><span class="tag ${n(t.status)}">${n(t.status)}</span></td>
              <td>${n(t.created_at)}</td></tr>`).join("")}
          </tbody></table>`:j("暂无选品草稿","可生成 mock listing，或跑本地选品摘要报表。")}
    </div></section>
  `}const D="缺数据";function $t(s){if(s==null||s==="")return"—";if(s===D)return D;const t=Number(s);return Number.isFinite(t)?(t/100).toFixed(2):n(s)}function Rt(s){return s==null||s===""?"—":n(s)}function K(s){return s==null||s===""||s===D}function X(s){if(K(s))return D;const t=Number(s);return Number.isFinite(t)?(t/100).toFixed(2):D}function _e(...s){if(s.some(e=>K(e)))return D;let t=0;for(const e of s){const a=Number(e);if(!Number.isFinite(a))return D;t+=a}return t}function L(s){return K(s)||!Number.isFinite(Number(s))?`<span class="missing">${D}</span>`:n(X(s))}function mt(s){return K(s)?`<span class="missing">${D}</span>`:n(s)}function Ft(s){return K(s)||!Number.isFinite(Number(s))?`<span class="missing">${D}</span>`:`${n(s)}%`}function Ht(s){return s&&s.data_status==="ok"?"完整":`<span class="missing">${D}</span>`}function we(){const s=i.reports||[],t=i.profit||{},e=t.totals||{},a=t.alerts||[],r=i.adSpendRows||[],o=i.user&&i.user.role!=="viewer",d=i.profitShop||"default";return`
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
      <article class="card"><h3>广告费 (THB)</h3><div class="metric">${n($t(e.ad_spend??t.ad_spend??0))}</div><p class="hint">店铺 ${n(d)} · 记账</p></article>
      <article class="card"><h3>ROAS</h3><div class="metric">${Rt(e.ROAS??t.ROAS)}</div><p class="hint">销售额 / 广告费</p></article>
      <article class="card"><h3>ACOS</h3><div class="metric">${Rt(e.ACOS??t.ACOS)}</div><p class="hint">广告费 / 销售额</p></article>
      <article class="card"><h3>扣广告后利润 (THB)</h3><div class="metric">${n($t(e.profit_after_ads??t.profit_after_ads))}</div><p class="hint">已扣记账广告费</p></article>
    </div>
    <div id="ad-roas-alert">
      ${a.length?a.map(l=>`<div class="banner warn">${n(l.message||"")} 保本 ROAS ${n(l.break_even_roas??"—")} · 最高 CPA ${n(l.max_cpa_minor==null?"—":$t(l.max_cpa_minor))} THB。只提醒，不自动操作。</div>`).join(""):'<div class="banner">没有低于保本的提醒。本页不修改广告预算。</div>'}
    </div>
    <section class="panel" style="margin-bottom:12px">
      <div class="panel-head"><h2>广告费明细</h2><span class="muted">${r.length} 行 · 只读拉取为 mock</span></div>
      <div class="panel-body" id="ad-spend-table">
        ${r.length?`<table><thead><tr><th>日期</th><th>店铺</th><th>SKU</th><th>渠道</th><th>花费 THB</th><th>展示</th><th>点击</th><th>转化</th><th>来源</th></tr></thead><tbody>
            ${r.map(l=>`<tr>
                <td>${n(l.spend_date)}</td>
                <td>${n(l.shop_id)}</td>
                <td>${n(l.sku||l.product_id||"—")}</td>
                <td>${n(l.channel)}</td>
                <td>${n(l.amount_thb)}</td>
                <td>${n(l.impressions??"—")}</td>
                <td>${n(l.clicks??"—")}</td>
                <td>${n(l.conversions??"—")}</td>
                <td>${n(l.source)}</td>
              </tr>`).join("")}
            </tbody></table>`:j("还没有广告费","operator 可手工记账，或从 mock 拉取。不会调用 TikTok Ads。")}
        <form id="ad-spend-form" class="grid-3" style="margin-top:16px">
          <div class="field"><label>日期</label><input name="spend_date" type="date" required ${o?"":"disabled"} /></div>
          <div class="field"><label>花费 (THB)</label><input name="amount_thb" inputmode="decimal" placeholder="12.50" required ${o?"":"disabled"} /></div>
          <div class="field"><label>渠道</label><input name="channel" value="tiktok_ads" ${o?"":"disabled"} /></div>
          <div class="field"><label>SKU</label><input name="sku" placeholder="可空，表示整店" ${o?"":"disabled"} /></div>
          <div class="field"><label>展示 / 点击 / 转化</label><input name="metrics" placeholder="1000,25,1 可空" ${o?"":"disabled"} /></div>
          <div class="field" style="display:flex;align-items:flex-end;gap:8px">
            <button class="btn" type="submit" ${o?"":"disabled"}>记账</button>
            <button class="btn secondary" type="button" id="btn-ads-pull" ${o?"":"disabled"}>从 mock 拉取</button>
          </div>
        </form>
        <p class="muted">${o?"记账只写本地表。mock 拉取不外呼、不改预算。":"当前是只读账号，不能记账。"}</p>
        <p id="ad-spend-form-error" class="error"></p>
      </div>
    </section>
    <section class="panel"><div class="panel-head"><h2>本地报表</h2></div><div class="panel-body">
      ${s.length?s.map(l=>`<article style="margin-bottom:16px">
                <h3 style="margin:0 0 6px">${n(l.kind)} · ${n(l.created_at)}</h3>
                <pre class="mono">${n(l.body_md||"")}</pre>
              </article>`).join(""):j("暂无报表","跑一次选品摘要或日报后会出现在这里。")}
    </div></section>
  `}function yt(s,t){if(!s.length)return j("暂无汇总","选定范围没有可汇总的订单。");const e=`<th>${n(t)}</th><th>订单数</th><th>完整</th><th>缺数据</th><th>收入(THB)</th><th>佣金</th><th>支付费</th><th>广告占位</th><th>广告费</th><th>ROAS</th><th>ACOS</th><th>扣广告后利润</th><th>采购成本</th><th>运费</th><th>毛利</th><th>毛利率</th><th>状态</th>`,a=s.map(r=>`<tr>
        <td>${n(r.key||r.shop_id||"")}</td>
        <td>${n(r.orders??0)}</td>
        <td>${n(r.complete_orders??0)}</td>
        <td>${n(r.incomplete_orders??0)}</td>
        <td>${L(r.revenue)}</td>
        <td>${L(r.platform_commission)}</td>
        <td>${L(r.payment_fee)}</td>
        <td>${L(r.ads)}</td>
        <td>${L(r.ad_spend)}</td>
        <td>${mt(r.ROAS)}</td>
        <td>${mt(r.ACOS)}</td>
        <td>${L(r.profit_after_ads)}</td>
        <td>${L(r.purchase_cost)}</td>
        <td>${L(r.shipping)}</td>
        <td>${L(r.gross_profit)}</td>
        <td>${Ft(r.gross_margin_pct)}</td>
        <td>${Ht(r)}</td>
      </tr>`).join("");return`<div class="table-wrap"><table><thead><tr>${e}</tr></thead><tbody>${a}</tbody></table></div>`}function ke(s){if(!s.length)return j("暂无订单","选定店铺与日期范围内没有已同步订单。");const t="<th>订单</th><th>店铺</th><th>日期</th><th>收入(THB)</th><th>佣金</th><th>支付费</th><th>广告占位</th><th>广告费</th><th>ROAS</th><th>ACOS</th><th>扣广告后利润</th><th>采购成本</th><th>运费</th><th>毛利</th><th>毛利率</th><th>状态</th>",e=s.map(a=>`<tr>
        <td>${n(a.order_id)}</td>
        <td>${n(a.shop_id)}</td>
        <td>${n(a.date||"")}</td>
        <td>${L(a.revenue)}</td>
        <td>${L(a.platform_commission)}</td>
        <td>${L(a.payment_fee)}</td>
        <td>${L(a.ads)}</td>
        <td>${L(a.ad_spend)}</td>
        <td>${mt(a.ROAS)}</td>
        <td>${mt(a.ACOS)}</td>
        <td>${L(a.profit_after_ads)}</td>
        <td>${L(a.purchase_cost)}</td>
        <td>${L(a.shipping)}</td>
        <td>${L(a.gross_profit)}</td>
        <td>${Ft(a.gross_margin_pct)}</td>
        <td>${Ht(a)}</td>
      </tr>`).join("");return`<div class="table-wrap"><table><thead><tr>${t}</tr></thead><tbody>${e}</tbody></table></div>`}function Se(){const s=i.profit||{},t=s.totals||{},e=s.alerts||[],a=i.profitShops||[],r=['<option value="">全部店铺</option>'].concat(a.map(x=>`<option value="${n(x.shop_id)}"${i.profitShop===x.shop_id?" selected":""}>${n(x.shop_id)}</option>`)).join(""),o=Number(s.ad_spend_row_count||0),d=s.rates||{},l=d.placeholder!==!1||o===0,b=t.revenue??s.revenue,f=t.purchase_cost,u=_e(t.platform_commission,t.payment_fee),O=t.ad_spend??s.ad_spend,T=t.ROAS??s.ROAS,c=t.ACOS??s.ACOS,m=t.profit_after_ads??s.profit_after_ads,h=t.gross_margin_pct,v=e.length>0,g=v?"低于保本：只提醒（alert_only），不自动改价、不建审批、不调预算":"只读利润控制台 · 数字来自已同步订单与广告费记账，缺数据不编造",w=[["收入 (THB)",X(b),"已同步订单销售额"],["采购成本 COGS (THB)",X(f),"代发采购；缺则标缺数据"],["平台费 (THB)",X(u),"佣金 + 支付费"],["广告费 (THB)",X(O),o?"记账 ad_spend":"无记账不编造"],["ROAS",K(T)?D:String(T),"销售额 / 记账广告费"],["ACOS",K(c)?D:String(c),"广告费 / 销售额"],["扣广告后利润 (THB)",X(m),"profit_after_ads"],["毛利率",K(h)?D:`${h}%`,"gross_margin_pct"]],k=[`佣金 ${d.commission_pct??"?"}%`,`支付 ${d.payment_fee_pct??"?"}%`,`广告 ${d.ads_pct??"?"}%`,`FX ${d.fx_rate??"?"}（${d.fx_source||"env"}）`],I=l?`占位费率（PRICING_*，非平台结算实收）：${k.join(" · ")}。广告记账为空或走占位路径时，扣广告后利润用占位 ads_pct，不编造广告费。${d.note||s.note||""} 低于保本只提醒（alert_only）。本页无付款/退款/提现按钮，也不改价、不建审批、不调预算。`:`费率：${k.join(" · ")}。${d.note||s.note||""} 低于保本只提醒（alert_only）。本页无付款/退款/提现按钮。`,N=v?e.map(x=>`<div class="banner warn">${n(x.message||"")} 保本 ROAS ${n(x.break_even_roas??"—")} · 最高 CPA ${n(x.max_cpa_minor==null?"—":X(x.max_cpa_minor))} THB。只提醒（alert_only），不自动改价、不建审批、不调预算。</div>`).join(""):"";return`
    <div class="banner ${v?"warn":""}">${J.check}<span>${n(g)}</span></div>
    <div class="page-head">
      <div>
        <h1>利润</h1>
        <p>正式控制台利润页。复用 <code>GET /api/workflow/profit</code>。viewer 只读。不是 /workflow 自测面板。</p>
      </div>
      <form class="filters" id="profit-console-form">
        <div class="field"><label for="profit-shop">店铺</label>
          <select id="profit-shop" name="shop_id">${r}</select>
        </div>
        <div class="field"><label for="profit-from">从</label>
          <input id="profit-from" name="from" type="date" value="${n(i.profitFrom)}" />
        </div>
        <div class="field"><label for="profit-to">到</label>
          <input id="profit-to" name="to" type="date" value="${n(i.profitTo)}" />
        </div>
        <button class="btn secondary" type="submit" id="btn-profit-query">查询</button>
      </form>
    </div>
    <div id="profit-summary-cards" class="grid-4" style="margin-bottom:12px">
      ${w.map(([x,C,G])=>`
        <article class="card">
          <h3>${n(x)}</h3>
          <div class="metric">${C===D?`<span class="missing">${D}</span>`:n(C)}</div>
          <p class="hint">${n(G)}</p>
        </article>`).join("")}
    </div>
    <div id="profit-alerts">${N}</div>
    <p class="muted" id="profit-summary">订单 ${n(s.order_count??0)} · 缺数据 ${n(s.incomplete_order_count??0)} · 单位 THB（API 为 satang/100） · 只读</p>
    <section class="panel">
      <div class="panel-head"><h2>合计</h2><span class="muted">与 API totals 同字段</span></div>
      <div class="panel-body" id="profit-totals">${yt([Object.assign({},t,{key:"合计"})],"范围")}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>按日</h2></div>
      <div class="panel-body" id="profit-by-day">${yt(s.by_day||[],"日期")}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>按周</h2></div>
      <div class="panel-body" id="profit-by-week">${yt(s.by_week||[],"ISO 周")}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>订单明细</h2><span class="muted">${n(s.order_count??0)} 行</span></div>
      <div class="panel-body" id="profit-orders">${ke(s.orders||[])}</div>
    </section>
    <footer class="profit-foot" id="profit-rates-note">${n(I)}</footer>
  `}function xe(){const s=i.runs||[],t=i.selectedRun,e=i.user&&i.user.role==="owner",a=i.user&&i.user.role!=="viewer",r=s.length?`<table><thead><tr><th>状态</th><th>当前步</th><th>流程</th><th>创建</th></tr></thead><tbody>
      ${s.map(d=>{const l=t&&t.id===d.id?' style="background:var(--bg)"':"";return`<tr class="clickable" data-run="${n(d.id)}"${l}>
            <td><span class="tag ${n(d.status)}">${n(d.status)}</span></td>
            <td>${n(d.current_step||"—")}</td>
            <td>${n(d.flow_id)}</td>
            <td>${n(d.created_at)}</td>
          </tr>`}).join("")}
      </tbody></table>`:j("暂无运行","点「一键 mock 演示」把 Scout→定价→Stylist→QA→审批→上架→订单/代发/物流→客服写入本库。");let o=j("选择运行","左侧点一行查看时间线。");if(t){const d=t.timeline||t.steps||[],l=t.status==="awaiting_approval";o=`
      <div class="panel-head">
        <h2>${n(t.status)} · ${n(t.current_step||"—")}</h2>
        <span class="muted">${n(t.id)}</span>
      </div>
      <div class="panel-body">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
          <button class="btn secondary" type="button" id="btn-run-retry" ${a?"":"disabled"}>重试当前步</button>
          <button class="btn danger" type="button" id="btn-run-cancel" ${a?"":"disabled"}>取消</button>
          ${l?`<button class="btn" type="button" id="btn-run-approve" ${e?"":"disabled"}>通过审批</button>
                 <button class="btn danger" type="button" id="btn-run-reject" ${e?"":"disabled"}>打回</button>
                 ${e?"":'<span class="muted">仅 owner 可审批</span>'}`:""}
        </div>
        ${l?`<div class="field"><label>审批说明（打回必填）</label>
               <textarea id="run-decision-note" placeholder="通过可写简短说明；打回必须写原因"></textarea></div>`:""}
        <h3>时间线</h3>
        ${d.length?d.map(b=>{let f=b.rationale||"";if(!f&&Array.isArray(b.ai_trace)){const u=b.ai_trace.find(O=>O&&O.rationale);f=u&&u.rationale||""}else!f&&b.ai_trace&&b.ai_trace.rationale&&(f=b.ai_trace.rationale);return`<article class="card" style="margin-bottom:10px">
                    <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
                      <strong>${n(b.label||b.step_id)}</strong>
                      <span class="tag ${n(b.status)}">${n(b.status)}</span>
                    </div>
                    <p class="muted" style="margin:6px 0">尝试 ${n(b.attempt)} · 成本 $${n(b.cost_usd??"0")} · ${n(b.started_at||b.created_at||"")}</p>
                    <p style="margin:0 0 8px"><strong>AI 理由：</strong>${n(f||"—")}</p>
                    <div class="diff">
                      <div><div class="muted" style="margin-bottom:4px">输入</div><pre>${n(JSON.stringify(b.input||{},null,2))}</pre></div>
                      <div><div class="muted" style="margin-bottom:4px">输出</div><pre>${n(JSON.stringify(b.output||{},null,2))}</pre></div>
                    </div>
                  </article>`}).join(""):j("无步骤","启动后会出现 Scout / Stylist / 审批等步骤。")}
      </div>
    `}return`
    <div class="page-head">
      <div>
        <h1>AI 工作流</h1>
        <p>编排器 run 列表、时间线与审批挂起。不打开真实发货/退款/TikTok 写入。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" id="btn-demo-orch" ${a?"":"disabled"} title="${a?"强制 WORKFLOW_PLATFORM=mock，不外呼，TTS_WRITE 保持关闭":"只读账号不能触发演示"}">一键 mock 演示</button>
        <button class="btn secondary" type="button" id="btn-start-run" ${a?"":"disabled"}>启动流水线</button>
      </div>
    </div>
    <div class="split">
      <section class="panel"><div class="panel-head"><h2>运行列表</h2></div><div class="panel-body">${r}</div></section>
      <section class="panel" id="run-detail">${o}</section>
    </div>
  `}function Oe(s){return s==="bad"?"bad":s==="warn"?"warn":s==="idle"?"idle":""}function Ee(s){if(!s||s.status==="missing")return"LLM 预算 · 缺数据";const t=Number(s.prompt_tokens||0)+Number(s.completion_tokens||0),e=s.cost_usd==null?"—":Number(s.cost_usd).toFixed(4);return`LLM ${t} tokens · $${e}`}function Ie(){const s=i.agentStats||{agents:[],runs:[],budget:{},task_logs:{}},t=s.agents||[],e=s.runs||[],a=i.agentRun,r=t.some(m=>m.health==="bad"),o=t.some(m=>m.health==="warn"),d=!r&&!o,l=r?"有代理失败 · 数字来自已落库的编排记录":o?"有代理挂起、待审批或 QA 打回":"只读统计 · 数字来自编排 run、步骤、审批表与 LLM 用量",b=s.budget||{},f=b.limit_status==="missing"?"日预算上限 · 缺数据":`日预算上限 $${Number(b.daily_limit_usd).toFixed(2)}`,u=Number(b.spent_in_range_usd||0).toFixed(4),O=t.length?t.map(m=>{const h=m.counts||{},v=m.recent_artifacts||[],g=v.length?`<ul class="artifact-list">${v.map(w=>`<li><button type="button" class="linkish" data-agent-run="${n(w.run_id)}" title="${n(w.href||"")}">${n(w.label)}</button></li>`).join("")}</ul>`:'<p class="hint">最近产物 · 暂无</p>';return`<section class="card agent-card" data-agent="${n(m.id)}">
            <div class="health-top">
              <span class="health-dot ${Oe(m.health)}"></span>
              <span class="health-name">${n(m.name)}</span>
              <span class="tag-mono">${n(m.role||m.id)}</span>
              <span class="tag-mono agent-kind">${n(m.kind||"")}</span>
            </div>
            <div class="health-desc">${n(m.description||"")}</div>
            <div class="agent-metrics">
              <div><strong>${n(h.runs??0)}</strong><span>运行</span></div>
              <div><strong>${n(h.succeeded??0)}</strong><span>成功</span></div>
              <div><strong>${n(h.qa_rejected??0)}</strong><span title="QA 打回">打回</span></div>
              <div><strong>${n(h.awaiting_approval??0)}</strong><span title="等待人工审批">待审</span></div>
              <div><strong>${n(h.failed??0)}</strong><span>失败</span></div>
            </div>
            <p class="hint">${n(Ee(m.llm))}${h.suspended?` · 挂起 ${n(h.suspended)}`:""}</p>
            ${g}
          </section>`}).join(""):j("暂无代理定义","流水线步骤未加载。"),T=e.length?`<table><thead><tr><th>Run</th><th>当前阶段</th><th>挂起原因</th><th>更新</th></tr></thead><tbody>
      ${e.map(m=>{const h=a&&a.run&&a.run.id===m.id?' style="background:var(--bg)"':"";return`<tr class="clickable" data-agent-run="${n(m.id)}"${h}>
            <td class="mono" title="${n(m.id)}">${n(String(m.id||"").slice(0,8))}</td>
            <td>${n(m.current_step_label||m.current_step||"—")}</td>
            <td><span class="tag ${n(m.suspend_code||m.status)}">${n(m.suspend_reason||"—")}</span></td>
            <td>${n(m.updated_at||m.created_at||"")}</td>
          </tr>`}).join("")}
      </tbody></table>`:j("暂无编排 run","在「AI 工作流」启动一条流水线，或让 demo-orch --db 指向本服务的工作台库之后，这里出现真实计数。");let c=j("选择运行","左侧点一行查看步骤时间线。本页只读。");if(a&&a.run){const m=a.steps||[];c=`
      <div class="panel-head">
        <h2>${n(a.run.suspend_reason||a.run.status)} · ${n(a.run.current_step_label||"—")}</h2>
        <span class="muted mono">${n(a.run.id)}</span>
      </div>
      <div class="panel-body">
        <p class="muted" style="margin-top:0">状态 ${n(a.run.status)} · 店 ${n(a.run.shop_id||"—")} · 只读时间线，不含发货 / 退款 / 付款。</p>
        ${m.length?`<ol class="timeline">${m.map(h=>{const v=h.status==="failed"?"bad":h.status==="awaiting_approval"||h.status==="suspended"?"warn":h.status==="succeeded"?"":"idle",g=h.finished_at||h.started_at||"",w=h.error&&h.error.message?h.error.message:"";return`<li>
                    <span class="health-dot ${v}" style="margin-top:6px"></span>
                    <div>
                      <strong>${n(h.label||h.step_id)}</strong>
                      <span class="tag ${n(h.status)}">${n(h.status)}</span>
                      <div class="muted">${n(g)}${h.rewrite_round?` · 重写 ${n(h.rewrite_round)}`:""}</div>
                      ${w?`<div class="error">${n(w)}</div>`:""}
                    </div>
                  </li>`}).join("")}</ol>`:j("无步骤","这条 run 还没有步骤记录。")}
      </div>`}return`
    <div class="banner ${d?"":"warn"}">${J.check}<span>${n(l)}</span></div>
    <div class="page-head">
      <div>
        <h1>AI 代理</h1>
        <p>选品 → 定价 → 文案图片 → 质检 → 审批 → 上架 → 订单 → 代发 → 物流 → 客服。只读，不打开真写。</p>
      </div>
      <form class="filters" id="agent-stats-form">
        <div class="field"><label for="agent-from">从</label><input id="agent-from" name="from" type="date" value="${n(i.agentFrom)}" /></div>
        <div class="field"><label for="agent-to">到</label><input id="agent-to" name="to" type="date" value="${n(i.agentTo)}" /></div>
        <button class="btn secondary" type="submit" id="btn-agent-stats">查询</button>
      </form>
    </div>
    <p class="muted" style="margin:-6px 0 12px">${n(f)} · 区间内已记账 $${n(u)} · 任务轨迹 ${n(s.task_logs&&s.task_logs.events||0)} 条${s.task_logs&&s.task_logs.business_tasks&&s.task_logs.business_tasks.label?` · 业务库任务日志 ${n(s.task_logs.business_tasks.label)}`:""}</p>
    <div class="grid-agents">${O}</div>
    <div class="split">
      <section class="panel"><div class="panel-head"><h2>编排 run</h2></div><div class="panel-body">${T}</div></section>
      <section class="panel" id="agent-run-detail">${c}</section>
    </div>
  `}const Jt=[["logistics_query","物流查询 · 白名单"],["tracking_status","物流状态 · 白名单"],["sla_reminder","时效提醒 · 白名单"],["refund","退款 · 必审"],["compensation","赔偿 · 必审"],["price_match","差价 · 必审"],["complaint","投诉 · 必审"]];function jt(s){const t=Jt.find(e=>e[0]===s);return t?t[1].split(" · ")[0]:s||"—"}function Ce(){const s=i.user&&i.user.role!=="viewer",t=i.csInbox||[],e=i.csDrafts||[],a=e.filter(d=>d.status==="pending_approval"&&d.approval_id),r=t.length?`<div class="table-wrap"><table><thead><tr><th>会话</th><th>类目</th><th>正文</th><th>时间</th><th></th></tr></thead><tbody>
      ${t.map(d=>`<tr>
          <td>${n(d.conversation_id)}</td>
          <td><span class="health-dot"></span> ${n(jt(d.category))}</td>
          <td>${n(d.body)}</td>
          <td>${n(d.created_at||"")}</td>
          <td class="cs-actions"><button class="btn secondary pill" type="button" data-cs-draft-msg="${n(d.id)}" ${s?"":"disabled"}>生成草稿</button></td>
        </tr>`).join("")}
      </tbody></table></div>`:j("入站为空","记一条本地 mock 消息，或点刷新。不会去拉 TikTok 客服。"),o=d=>d.length?`<div class="table-wrap"><table><thead><tr><th>类目</th><th>状态</th><th>话术</th><th>动作</th></tr></thead><tbody>
        ${d.map(l=>{const b=l.sent_stub||l.status==="sent_stub",f=l.whitelist?'<span class="tag approved">白名单免审</span>':'<span class="tag pending">必审</span>',u=[];b?u.push('<span class="tag approved">sent_stub</span>','<span class="tag approved">dry_run</span>'):l.whitelist&&u.push(`<button class="btn pill ok" type="button" data-cs-send="${n(l.id)}" ${s?"":"disabled"}>stub 发送</button>`),l.approval_id&&!b&&u.push(`<a class="btn secondary pill" href="/admin/approvals?id=${encodeURIComponent(l.approval_id)}" data-cs-approval="${n(l.approval_id)}">查看审批</a>`);const O=u.length?u.join(""):"—";return`<tr>
              <td>${n(jt(l.category))} ${f}</td>
              <td><span class="tag ${n(l.status)}">${n(l.status)}</span></td>
              <td>${n(l.body_th||l.body_en||"")}</td>
              <td class="cs-actions">${O}</td>
            </tr>`}).join("")}
        </tbody></table></div>`:"";return`
    <div class="banner">${J.check}<span>客服白名单在本地 stub。不外呼 TikTok 客服。</span></div>
    <div class="page-head">
      <div>
        <h1>客服</h1>
        <p>入站 inbox、话术草稿、白名单 stub 发送。非白名单建 auto_reply 审批。</p>
      </div>
      <button class="btn secondary pill" type="button" id="btn-refresh-cs">刷新</button>
    </div>
    ${i.csNote?`<div class="banner">${n(i.csNote)}</div>`:""}
    ${i.csError?`<p class="error" id="cs-form-error">${n(i.csError)}</p>`:'<p class="error" id="cs-form-error"></p>'}
    <div class="split cs-layout">
      <section class="panel">
        <div class="panel-head"><h2>入站 inbox</h2><span class="muted">${t.length} 条 · 本地 mock</span></div>
        <div class="panel-body">${r}</div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>草稿</h2><span class="muted">待审 ${a.length}</span></div>
        <div class="panel-body">
          <h3 style="margin:0 0 8px">待审草稿</h3>
          ${a.length?o(a):j("没有待审草稿","非白名单类目生成草稿后会出现在这里，并可跳到审批详情。")}
          <h3 style="margin:16px 0 8px">全部草稿</h3>
          ${e.length?o(e):j("暂无客服草稿","从入站点「生成草稿」。")}
        </div>
      </section>
    </div>
    <section class="panel">
      <div class="panel-head"><h2>记入本地入站</h2><span class="muted">不外呼平台</span></div>
      <div class="panel-body">
        <form id="cs-inbox-form" class="cs-form">
          <div class="field"><label>会话 ID</label><input name="conversation_id" required placeholder="conv-1" ${s?"":"disabled"} /></div>
          <div class="field"><label>类目</label>
            <select name="category" ${s?"":"disabled"}>
              ${Jt.map(([d,l])=>`<option value="${n(d)}">${n(l)}</option>`).join("")}
            </select>
          </div>
          <div class="field"><label>语言</label>
            <select name="lang" ${s?"":"disabled"}>
              <option value="th">th</option>
              <option value="en">en</option>
              <option value="zh">zh</option>
            </select>
          </div>
          <div class="field"><label>订单号（可空）</label><input name="order_id" ${s?"":"disabled"} /></div>
          <div class="field cs-form-wide"><label>买家消息</label><textarea name="body" required placeholder="พัสดุถึงไหนแล้วคะ" ${s?"":"disabled"}></textarea></div>
          <div class="field cs-form-wide">
            <button class="btn pill" type="submit" ${s?"":"disabled"}>记入本地入站</button>
            <p class="muted">${s?"只写本地 inbox，不外呼平台。":"当前是只读账号，不能写入站或发送。"}</p>
          </div>
        </form>
      </div>
    </section>
    <footer class="page-foot" id="cs-constraints">不外呼 TikTok 客服；发货/退款仍 403；不打开 TTS_WRITE_*；涉钱类（refund/compensation/price_match/complaint）必审。</footer>
  `}function Ae(s){return s==="pending"?"待你确认":s==="approved"?"已标记通过":s==="rejected"?"已标记拒绝":s||"—"}function Te(){const s=i.user&&i.user.role!=="viewer",t=i.outreachDrafts||[],e=t.filter(r=>r.status==="pending"),a=t.length?`<div class="outreach-grid">${t.map(r=>{const o=r.payload||{},d=o.message||{},l=Array.isArray(d.terms)?d.terms:[],b=Array.isArray(d.followups)?d.followups:[],f=l.length?`<ul class="outreach-terms">${l.map(c=>`<li><span>${n(c.label||c.key||"")}</span> ${n(c.value||"")}</li>`).join("")}</ul>`:"",u=b.length?b.map(c=>`<p class="outreach-copy"><strong>第 ${n(c.day)} 天</strong> ${n(c.body_th||"")}<br><span class="muted">${n(c.body_zh||"")}</span></p>`).join(""):"",O=r.status==="pending"?`<div class="outreach-actions">
                  <button class="btn pill ok" type="button" data-outreach-approve="${n(r.id)}" ${s?"":"disabled"}>标记通过</button>
                  <button class="btn secondary pill" type="button" data-outreach-reject="${n(r.id)}" ${s?"":"disabled"}>标记拒绝</button>
                </div>`:'<p class="muted">只改了状态。没有发送。</p>',T=o.run_id?`<a class="btn secondary pill" href="/admin/workflows?run=${encodeURIComponent(o.run_id)}">工作流</a>`:"";return`<article class="outreach-card panel">
            <div class="outreach-meta">
              <span class="tag">${n(o.creator_type||r.kind||"达人")}</span>
              <span class="tag ${n(r.status)}">${n(Ae(r.status))}</span>
              ${T}
            </div>
            <h3>${n(r.title||o.creator_name||"达人")}</h3>
            <p class="outreach-copy">${n(d.dm_th||r.body_th||"")}</p>
            <p class="muted outreach-copy">${n(d.dm_zh||"")}</p>
            <p class="muted outreach-copy">${n(d.dm_en||r.body_en||"")}</p>
            <p class="outreach-copy"><strong>样品</strong> ${n(d.sample_th||"")}<br><span class="muted">${n(d.sample_zh||"")}</span></p>
            ${f}
            ${u}
            <p class="muted">${n(d.closing_note||"")}</p>
            ${O}
          </article>`}).join("")}</div>`:j("还没有达人建联草稿","跑一条 AI 工作流或一键 mock 演示后，通过 QA 的商品会在这里出现。草稿不会自动发送。");return`
    <div class="banner">${J.check}<span>达人建联只产草稿。不会自动发送，也不会外呼 TikTok。</span></div>
    <div class="page-head">
      <div>
        <h1>达人建联</h1>
        <p>泰语邀约草稿待你确认。通过或拒绝只改状态，不发送私信、不寄样、不付款。</p>
      </div>
      <button class="btn secondary pill" type="button" id="btn-refresh-outreach">刷新</button>
    </div>
    ${i.outreachNote?`<div class="banner">${n(i.outreachNote)}</div>`:""}
    ${i.outreachError?`<p class="error" id="outreach-form-error">${n(i.outreachError)}</p>`:'<p class="error" id="outreach-form-error"></p>'}
    <section class="panel">
      <div class="panel-head">
        <h2>草稿</h2>
        <span class="muted">待你确认 ${e.length} / ${t.length}</span>
      </div>
      <div class="panel-body">
        <div class="field">
          <label for="outreach-note">拒绝原因</label>
          <input id="outreach-note" placeholder="标记拒绝时必填" ${s?"":"disabled"} />
        </div>
        <p class="muted">${s?"通过不需要原因。两种决定都不发送。":"当前是只读账号，不能改状态。"}</p>
      </div>
    </section>
    ${a}
    <footer class="page-foot" id="outreach-constraints">不自动发送；不外呼 TikTok；不打开 TTS_WRITE_*；批准只改状态，不发送私信、不寄样、不付款。</footer>
  `}function Re(s){return!s||!s.groups?"":`${s.groups.map(e=>`
      <h3 style="margin-top:16px">${n(e.label)}</h3>
      <table>
        ${(e.items||[]).map(a=>`<tr>
            <td>${a.secret?"🔒 ":""}${n(a.name)}</td>
            <td>${a.value===""||a.value===void 0?'<span class="muted">未设置</span>':n(a.value)}</td>
            <td class="muted">${n(a.detail||"")}</td>
          </tr>`).join("")}
      </table>`).join("")}<p class="muted" style="margin-top:12px">${n(s.note||"")}</p>`}function je(s){const t=s&&s.pii_retention;return!t||!t.ran?`<span class="muted">${n(t&&t.summary||"尚未执行")}</span>`:`${n(t.ran_at||"")}<div class="muted">${n(t.summary||"")}</div>`}function Ne(s){if(!s)return'<span class="muted">加载中…</span>';if(!s.available)return`<span class="muted">${n(s.reason||"不适用")}</span>`;const t=s.used===null||s.used===void 0?"?":s.used,e=s.limit===null||s.limit===void 0?"?":s.limit,a=s.exhausted?"failed":"approved",r=s.exhausted?"今日已用完":`剩 ${s.remaining}`;return`${t} / ${e} <span class="tag ${a}">${n(r)}</span>`}function Pe(){var t,e;const s=i.status||{};return`
    <div class="page-head"><div><h1>设置</h1><p>环境与能力开关（只读展示）。</p></div></div>
    <section class="panel"><div class="panel-body">
      <table>
        <tr><th>项</th><th>值</th></tr>
        <tr><td>TikTok 模式</td><td>${n(s.tiktok_mode)}</td></tr>
        <tr><td>平台适配器</td><td>${n(s.platform)}</td></tr>
        <tr><td>LLM</td><td>${n(s.llm_provider)} ${s.llm_degraded?"(降级)":""}</td></tr>
        <tr><td>时区</td><td>${n(s.timezone)}</td></tr>
        <tr><td>上架写通道</td><td>${s.publish_enabled?"可（仍须审批）":"关"}</td></tr>
        <tr><td>TTS 真写武装</td><td>${s.tts_write_armed?"是":"否"}</td></tr>
        <tr><td>今日写配额</td><td>${Ne(i.ttsQuota)}</td></tr>
        <tr><td>发货</td><td>禁用（去卖家中心）</td></tr>
        <tr><td>退款</td><td>禁用（去卖家中心）</td></tr>
        <tr><td>上次 PII 清理</td><td>${je(i.settings)}</td></tr>
        <tr><td>当前用户</td><td>${n((t=i.user)==null?void 0:t.username)} / ${n((e=i.user)==null?void 0:e.role)}</td></tr>
      </table>
      ${Re(i.settings)}
      <p class="muted" style="margin-top:12px">${n(s.note||"")}</p>
      <p class="muted">开发自测面板：<a href="/workflow">/workflow</a></p>
    </div></section>
  `}function _t(s){if(!s)return"缺数据";const t=new Date(s);if(Number.isNaN(t.getTime()))return String(s).slice(0,16);const e=a=>String(a).padStart(2,"0");return`${e(t.getMonth()+1)}-${e(t.getDate())} ${e(t.getHours())}:${e(t.getMinutes())}`}function Nt(s,t){return s==="ok"?"succeeded":s==="waiting_approval"?"awaiting_approval":s==="failed"?"failed":s==="suspended"?"suspended":t||""}function Be(s){return s==="failed"?"bad":s==="waiting_approval"||s==="suspended"?"warn":s==="ok"?"":"idle"}function Pt(s,t){return!t||!t.length?"":`<div class="run-summary"><div class="muted">${n(s)}</div>${t.map(e=>`<div><span class="k">${n(e.label)}</span>${n(e.value)}</div>`).join("")}</div>`}function Le(s){if(!s||s.status!=="recorded")return'<span class="missing">LLM · 缺数据</span>';const t=Number(s.prompt_tokens||0)+Number(s.completion_tokens||0),e=s.cost_usd==null?"—":Number(s.cost_usd).toFixed(4),a=s.model?` · ${n(s.model)}`:"";return`LLM ${n(t)} tokens · $${n(e)}${a}`}function Bt(s){return s?`<pre class="error-data" data-error-code="${n(s.code||"")}">${n(JSON.stringify(s,null,2))}</pre>`:""}function wt(s,t){if(!s||!s.id)return"";const e=s.pending?"去审批队列":"查看审批",a=s.href||`/admin/approvals?id=${encodeURIComponent(s.id)}`;return`<a class="${t}" href="${n(a)}" data-orch-approval="${n(s.id)}">${e}</a>`}function qe(){const s=i.orchRuns||[],t=i.orchRun,e=t&&t.run,a={ok:0,waiting:0,failed:0};s.forEach(f=>{f.display_status==="ok"?a.ok+=1:f.display_status==="waiting_approval"?a.waiting+=1:f.display_status==="failed"&&(a.failed+=1)});const r=e&&(e.display_status==="failed"||e.display_status==="waiting_approval"),o=e?e.display_status==="waiting_approval"?"等待人工审批 · 从这里进入审批队列，本页不代批":e.display_status==="failed"?"有步骤失败 · 错误以数据返回，未外呼平台":"这条运行的时间线只读 · 不打开 TikTok 真写":"只读时间线 · 数字来自已落库的编排记录 · 不打开 TikTok 真写",d=[["最近运行",s.length,"本页列表",s.length?"":"idle"],["成功",a.ok,"终态成功",a.ok?"":"idle"],["等待人工审批",a.waiting,"链到审批队列",a.waiting?"warn":"idle"],["失败",a.failed,"错误以数据返回",a.failed?"bad":"idle"]],l=s.length?`<table><thead><tr><th>状态</th><th>商品</th><th>开始</th><th>耗时</th><th>停在</th></tr></thead><tbody>
      ${s.map(f=>{const u=e&&e.id===f.id?' style="background:var(--bg)"':"",O=f.product&&f.product.title_label||"缺数据",T=f.pending_approval?`<div>${wt(f.pending_approval,"queue-link")}</div>`:"";return`<tr class="clickable" data-orch-run="${n(f.id)}"${u}>
            <td><span class="tag ${n(Nt(f.display_status,f.status))}">${n(f.display_label||f.status)}</span></td>
            <td title="${n(f.product&&f.product.url||"")}">${n(O)}</td>
            <td>${n(_t(f.started_at))}</td>
            <td>${n(f.duration_label||"缺数据")}</td>
            <td>${n(f.stopped_step_label||"—")}${T}</td>
          </tr>`}).join("")}
      </tbody></table>`:j("暂无编排运行","在总览点「一键 mock 演示」，跑完后这里出现完整时间线。");let b=j("选择运行","左侧点一行查看每个 agent 步骤。本页只读。");if(e){const f=t.timeline||[];b=`
      <div class="panel-head">
        <h2>${n(e.display_label||e.status)} · ${n(e.stopped_step_label||"—")}</h2>
        <span class="muted mono">${n(e.id)}</span>
      </div>
      <div class="panel-body">
        <p class="muted" style="margin-top:0">商品 ${n(e.product&&e.product.title_label||"缺数据")} · 开始 ${n(_t(e.started_at))} · 耗时 ${n(e.duration_label||"缺数据")}</p>
        ${e.pending_approval?`<p>${wt(e.pending_approval,"btn secondary pill")}</p>`:""}
        ${Bt(e.error)}
        <h3>时间线</h3>
        ${f.length?`<ol class="timeline" id="orch-run-timeline">${f.map(u=>`<li data-step="${n(u.step_id)}" data-display-status="${n(u.display_status)}">
                    <span class="health-dot ${Be(u.display_status)}" style="margin-top:6px"></span>
                    <div>
                      <div class="step-head">
                        <strong>${n(u.label||u.step_id)}</strong>
                        <span class="tag ${n(Nt(u.display_status,u.status))}">${n(u.display_label||u.status)}</span>
                      </div>
                      <div class="muted">${n(_t(u.started_at||u.finished_at))} · ${n(u.duration_label||"缺数据")}${u.attempt?` · 尝试 ${n(u.attempt)}`:""}</div>
                      ${Pt("输入",u.input_summary)}
                      ${Pt("输出",u.output_summary)}
                      <p class="hint" style="margin:6px 0 0">${Le(u.llm)}</p>
                      ${Bt(u.error)}
                      ${u.approval?`<p style="margin:8px 0 0">${wt(u.approval,"btn secondary pill")}</p>`:""}
                    </div>
                  </li>`).join("")}</ol>`:j("无步骤","这条 run 还没有步骤记录。")}
      </div>`}return`
    <div class="banner ${r?"warn":""}" id="orch-run-banner">${J.check}<span>${n(o)}</span></div>
    <div class="page-head">
      <div>
        <h1>编排运行</h1>
        <p>选品 → 定价 → 文案图片 → 质检 → 短视频脚本 → 达人建联 → 审批 → 上架 → 订单 → 代发 → 物流 → 客服。只读，不代批、不外呼。</p>
      </div>
    </div>
    <div class="grid-4 run-kpis" id="orch-run-health" style="margin-bottom:12px">
      ${d.map(([f,u,O,T])=>`
        <section class="card health-card">
          <div class="health-top">
            <span class="health-dot ${T}"></span>
            <span class="health-name">${n(f)}</span>
          </div>
          <div class="metric">${n(u)}</div>
          <div class="health-desc">${n(O)}</div>
        </section>`).join("")}
    </div>
    <div class="split">
      <section class="panel" id="orch-run-list"><div class="panel-head"><h2>最近运行</h2></div><div class="panel-body">${l}</div></section>
      <section class="panel" id="orch-run-detail">${b}</section>
    </div>
    <footer class="page-foot">本页只读。不外呼 TikTok，不打开发货、退款或付款，真写开关保持关闭。待审单进入现有审批队列。</footer>
  `}async function Me(){await H();try{const e=await S("/api/workflow/shops");i.profitShops=e.shops||[]}catch{i.profitShops=[{shop_id:"default"}]}const s=new URLSearchParams;i.profitShop&&s.set("shop_id",i.profitShop),i.profitFrom&&s.set("from",i.profitFrom),i.profitTo&&s.set("to",i.profitTo);const t=s.toString();i.profit=await S("/api/workflow/profit"+(t?`?${t}`:""))}async function De(){const s=new URLSearchParams;i.agentFrom&&s.set("from",i.agentFrom),i.agentTo&&s.set("to",i.agentTo);const t=s.toString();i.agentStats=await S("/api/admin/agents/stats"+(t?`?${t}`:""));const e=i.agentStats&&i.agentStats.runs||[],a=i.agentRun&&i.agentRun.run&&i.agentRun.run.id,r=a&&e.some(o=>o.id===a)?a:e[0]&&e[0].id;r?i.agentRun=await S(`/api/admin/agents/runs/${encodeURIComponent(r)}`):i.agentRun=null}async function Ue(){const s=await S("/api/workflow/outreach/drafts");i.outreachDrafts=s.drafts||[]}async function Fe(){const[s,t]=await Promise.all([S("/api/workflow/cs/inbox"),S("/api/workflow/cs/drafts")]);i.csInbox=s.messages||[],i.csDrafts=t.drafts||[]}async function He(s){const t=await S("/api/admin/runs?limit=50");i.orchRuns=t.runs||[];const a=s||""||i.orchRuns[0]&&i.orchRuns[0].id;if(!a){i.orchRun=null;return}try{i.orchRun=await S(`/api/admin/runs/${encodeURIComponent(a)}`)}catch(r){if(r.status===404&&i.orchRuns[0]&&i.orchRuns[0].id!==a){i.orchRun=await S(`/api/admin/runs/${encodeURIComponent(i.orchRuns[0].id)}`);return}throw r}}async function tt(s){const t=await S("/api/orchestrator/runs?limit=50");i.runs=t.runs||[],s?i.selectedRun=await S(`/api/orchestrator/runs/${encodeURIComponent(s)}`):i.selectedRun&&i.selectedRun.id?i.selectedRun=await S(`/api/orchestrator/runs/${encodeURIComponent(i.selectedRun.id)}`):i.runs[0]?i.selectedRun=await S(`/api/orchestrator/runs/${encodeURIComponent(i.runs[0].id)}`):i.selectedRun=null}async function Je(){if(Y[i.route]){i.shops.length||await Dt(),await dt.load();return}if(i.route==="overview")await H();else if(i.route==="workflows")await H(),await tt(Et()||i.selectedRun&&i.selectedRun.id);else if(i.route==="runs")await He(Et()||i.orchRun&&i.orchRun.run&&i.orchRun.run.id);else if(i.route==="agents")await De();else if(i.route==="approvals")await H(),await kt(ne()||i.selectedApproval&&i.selectedApproval.id);else if(i.route==="cs")await H(),await Fe();else if(i.route==="outreach")await H(),await Ue();else if(i.route==="products"){await H();const s=await S("/api/workflow/catalog");i.catalog=s.items||s.catalog||[]}else if(i.route==="orders"){await H();const s=i.activeShop||"default",t=i.shopGeneration,e=`?shop_id=${encodeURIComponent(s)}`;try{const a=await S(`/api/workflow/orders/synced${e}`);if(t!==i.shopGeneration||(i.activeShop||"default")!==s||(i.syncedOrders=a.orders||[],i.sla=a.sla||await S(`/api/workflow/orders/sla${e}`),t!==i.shopGeneration||(i.activeShop||"default")!==s))return}catch(a){if(t!==i.shopGeneration||(i.activeShop||"default")!==s)return;throw i.syncedOrders=[],i.sla=null,a}}else if(i.route==="scout"){await H();const s=await S("/api/workflow/drafts");i.drafts=s.drafts||[]}else if(i.route==="spotcheck")await H(),await Ut();else if(i.route==="reports"){await H();const s=await S("/api/workflow/reports");i.reports=s.reports||[];const t=i.profitShop||"default",e=await S(`/api/workflow/profit?shop_id=${encodeURIComponent(t)}`);i.profit=e;const a=await S(`/api/workflow/ad-spend?shop_id=${encodeURIComponent(t)}`);i.adSpendRows=a.rows||[]}else if(i.route==="profit")await Me();else if(i.route==="settings"){await H();try{i.ttsQuota=await S("/api/admin/tts-quota")}catch{i.ttsQuota={available:!1,reason:"读取配额失败。"}}try{i.settings=await S("/api/admin/settings")}catch{i.settings=null}}}function Ge(){var lt;const s=document.getElementById("order-workbench");if(s){et.attach(s,(lt=i.user)==null?void 0:lt.role);const _=P=>{history.replaceState({},"",`/admin/orders?order=${encodeURIComponent(P)}`),i.selectedOrderId=P,document.querySelectorAll("[data-order-work]").forEach(W=>{W.classList.toggle("is-selected",W.dataset.orderWork===P)}),et.shopId=i.activeShop||"default",et.open(P)};document.querySelectorAll("[data-order-work]").forEach(P=>{P.onclick=()=>_(P.dataset.orderWork)});const $=document.getElementById("order-search"),y=document.getElementById("order-status-filter"),A=document.getElementById("order-platform-filter"),q=()=>{const P=String(($==null?void 0:$.value)||"").trim().toLowerCase(),W=String((y==null?void 0:y.value)||""),pt=String((A==null?void 0:A.value)||"");document.querySelectorAll("[data-order-work]").forEach(V=>{const Gt=!P||String(V.dataset.orderSearch||"").includes(P),Wt=!W||V.dataset.orderStatus===W,zt=!pt||V.dataset.orderPlatform===pt;V.hidden=!(Gt&&Wt&&zt)})};$==null||$.addEventListener("input",q),y==null||y.addEventListener("change",q),A==null||A.addEventListener("change",()=>{i.orderPlatform=A.value,q()});const R=document.getElementById("shopee-csv");R==null||R.addEventListener("change",async()=>{const P=R.files&&R.files[0];if(!P)return;const W=i.activeShop||"default",pt=i.shopGeneration;try{const V=await P.text();if(pt!==i.shopGeneration||(i.activeShop||"default")!==W){R.value="",i.pageError="店铺已切换，已取消本次 CSV 导入，请重新选择文件。",E();return}await S("/api/workflow/orders/import/shopee",{method:"POST",body:JSON.stringify({csv_text:V,shop_id:W})}),R.value="",await E()}catch(V){i.pageError=V.message||"Shopee CSV 导入失败",E()}});const M=new URLSearchParams(location.search).get("order"),z=(i.syncedOrders||[]).find(P=>String(P.order_id||P.id)===M);z&&String(z.platform||"tiktok")==="tiktok"?_(M):(i.selectedOrderId="",et.clear())}const t=document.getElementById("btn-refresh-overview");t&&(t.onclick=()=>E()),document.querySelectorAll("[data-approval]").forEach(_=>{_.addEventListener("click",()=>{const $=_.getAttribute("data-approval");$&&st("approvals",{id:$})})}),document.querySelectorAll("[data-agent-run]").forEach(_=>{_.addEventListener("click",async()=>{const $=_.getAttribute("data-agent-run");$&&(i.agentRun=await S(`/api/admin/agents/runs/${encodeURIComponent($)}`),E())})});const e=document.getElementById("agent-stats-form");e&&e.addEventListener("submit",_=>{var $,y;_.preventDefault(),i.agentFrom=(($=document.getElementById("agent-from"))==null?void 0:$.value)||"",i.agentTo=((y=document.getElementById("agent-to"))==null?void 0:y.value)||"",i.agentRun=null,E()});const a=document.getElementById("profit-console-form");a&&a.addEventListener("submit",_=>{var $,y,A;_.preventDefault(),i.profitShop=(($=document.getElementById("profit-shop"))==null?void 0:$.value)||"",i.profitFrom=((y=document.getElementById("profit-from"))==null?void 0:y.value)||"",i.profitTo=((A=document.getElementById("profit-to"))==null?void 0:A.value)||"",E()}),document.querySelectorAll("[data-orch-run]").forEach(_=>{_.addEventListener("click",async $=>{if($.target&&$.target.closest&&$.target.closest("[data-orch-approval]"))return;const y=_.getAttribute("data-orch-run");if(!y)return;const A=`/admin/runs?run=${encodeURIComponent(y)}`;`${location.pathname}${location.search}`!==A&&history.pushState({},"",A),i.orchRun=await S(`/api/admin/runs/${encodeURIComponent(y)}`),E()})}),document.querySelectorAll("[data-orch-approval]").forEach(_=>{_.addEventListener("click",$=>{$.preventDefault(),$.stopPropagation();const y=_.getAttribute("data-orch-approval");y&&st("approvals",{id:y})})}),document.querySelectorAll("[data-run]").forEach(_=>{_.addEventListener("click",async()=>{const $=_.getAttribute("data-run"),y=`/admin/workflows?run=${encodeURIComponent($)}`;`${location.pathname}${location.search}`!==y&&history.pushState({},"",y),i.selectedRun=await S(`/api/orchestrator/runs/${encodeURIComponent($)}`),E()})});const r=document.getElementById("btn-start-run");r&&(r.onclick=async()=>{const _=await S("/api/orchestrator/runs",{method:"POST",body:JSON.stringify({url:ae})});i.selectedRun=_,await tt(_.id),E()});const o=document.getElementById("btn-demo-orch");o&&(o.onclick=async()=>{if(!(i.user&&i.user.role==="viewer")){o.disabled=!0,o.textContent="演示运行中…";try{const _=await S("/api/admin/demo-orch",{method:"POST",body:"{}"}),$=_.run_id,y=_.href||`/admin/runs?run=${encodeURIComponent($)}`;`${location.pathname}${location.search}`!==y&&history.pushState({},"",y),E()}catch(_){i.pageError=_.message||"演示失败",E()}}});const d=document.getElementById("btn-run-approve");d&&(d.onclick=async()=>{var y;const _=((y=document.getElementById("run-decision-note"))==null?void 0:y.value)||"",$=i.selectedRun.id;await S(`/api/orchestrator/runs/${encodeURIComponent($)}/approve`,{method:"POST",body:JSON.stringify({note:_})}),await tt($),E()});const l=document.getElementById("btn-run-reject");l&&(l.onclick=async()=>{var y;const _=(((y=document.getElementById("run-decision-note"))==null?void 0:y.value)||"").trim();if(!_){alert("打回必须填写原因");return}const $=i.selectedRun.id;await S(`/api/orchestrator/runs/${encodeURIComponent($)}/reject`,{method:"POST",body:JSON.stringify({note:_})}),await tt($),E()});const b=document.getElementById("btn-run-cancel");b&&(b.onclick=async()=>{const _=i.selectedRun.id;await S(`/api/orchestrator/runs/${encodeURIComponent(_)}/cancel`,{method:"POST",body:JSON.stringify({note:"admin cancel"})}),await tt(_),E()});const f=document.getElementById("btn-run-retry");f&&(f.onclick=async()=>{const _=i.selectedRun.id,$=i.selectedRun.current_step;if(!$){alert("没有可重试的当前步骤");return}await S(`/api/orchestrator/runs/${encodeURIComponent(_)}/steps/${encodeURIComponent($)}/retry`,{method:"POST",body:"{}"}),await tt(_),E()});const u=document.getElementById("btn-save-inputs");u&&u.addEventListener("click",async()=>{const _=i.draftDetail||{},$=A=>(document.getElementById(A)||{}).value||"",y=document.getElementById("pi-msg");try{const A=await S(`/api/workflow/drafts/${_.id}/inputs`,{method:"POST",body:JSON.stringify({category_id:$("pi-category"),warehouse_id:$("pi-warehouse"),price_amount:$("pi-price"),image_path:$("pi-image")})});y&&(y.textContent=`已保存：${(A.saved||[]).join("、")||"无"}（草稿已回到待审）`),await E()}catch(A){y&&(y.textContent=`保存失败：${A&&A.message||A}`)}});const O=document.getElementById("btn-approve");O&&(O.onclick=async()=>{var y,A;const _=((y=document.getElementById("decision-note"))==null?void 0:y.value)||"",$=i.selectedApproval.id;await S(`/api/workflow/approvals/${encodeURIComponent($)}/approve`,{method:"POST",body:JSON.stringify({note:_,expected_hash:((A=i.selectedApproval)==null?void 0:A.payload_hash)||""})}),await kt($),E()});const T=document.getElementById("btn-reject");T&&(T.onclick=async()=>{var y;const _=(((y=document.getElementById("decision-note"))==null?void 0:y.value)||"").trim();if(!_){alert("打回必须填写原因");return}const $=i.selectedApproval.id;await S(`/api/workflow/approvals/${encodeURIComponent($)}/reject`,{method:"POST",body:JSON.stringify({note:_})}),await kt($),E()});const c=document.getElementById("btn-import-demo");c&&(c.onclick=async()=>{await S("/api/workflow/catalog/import-demo",{method:"POST",body:"{}"}),E()});const m=document.getElementById("btn-sync-orders");m&&(m.onclick=async()=>{try{await S("/api/workflow/orders/sync",{method:"POST",body:JSON.stringify({shop_id:i.activeShop||"default"})})}catch(_){i.pageError=_.message||"订单同步失败"}E()});const h=document.getElementById("btn-gen-listing");h&&(h.onclick=async()=>{await S("/api/workflow/drafts/generate",{method:"POST",body:JSON.stringify({kind:"listing",items:[{name:"抽屉分隔盒",specs:"外径 20cm，材质 PP"}]})}),E()});const v=document.getElementById("btn-spot-score");v&&(v.onclick=async()=>{await be()});const g=document.getElementById("btn-spot-export");g&&(g.onclick=()=>{if(!i.spotCheckReport){window.alert("请先点「计算通过率」，再导出。");return}const _=new Blob([i.spotCheckReport],{type:"text/markdown;charset=utf-8"}),$=document.createElement("a");$.href=URL.createObjectURL(_),$.download="m1-spot-check.md",$.click(),URL.revokeObjectURL($.href)});const w=document.getElementById("btn-spot-reload");w&&(w.onclick=async()=>{await Ut(),E()});const k=document.getElementById("btn-run-research");k&&(k.onclick=async()=>{await S("/api/workflow/jobs/research",{method:"POST"}),E()});const I=document.getElementById("btn-job-research");I&&(I.onclick=async()=>{await S("/api/workflow/jobs/research",{method:"POST"}),E()});const N=document.getElementById("btn-job-daily");N&&(N.onclick=async()=>{await S("/api/workflow/jobs/daily",{method:"POST"}),E()});const x=document.getElementById("ad-spend-form");x&&(x.onsubmit=async _=>{_.preventDefault();const $=new FormData(x),y=String($.get("metrics")||"").split(",").map(R=>R.trim()),A={shop_id:i.profitShop||"default",spend_date:String($.get("spend_date")||""),amount_thb:String($.get("amount_thb")||""),channel:String($.get("channel")||"tiktok_ads"),sku:String($.get("sku")||"")||null,impressions:y[0]?Number(y[0]):null,clicks:y[1]?Number(y[1]):null,conversions:y[2]?Number(y[2]):null},q=document.getElementById("ad-spend-form-error");try{await S("/api/workflow/ad-spend",{method:"POST",body:JSON.stringify(A)}),q&&(q.textContent=""),E()}catch(R){q&&(q.textContent=R.message||"记账失败")}});const C=document.getElementById("btn-ads-pull");C&&(C.onclick=async()=>{const _=new Date,$=new Date(_.getTime()-6*864e5),y=q=>q.toISOString().slice(0,10),A=document.getElementById("ad-spend-form-error");try{await S("/api/workflow/ad-spend/pull",{method:"POST",body:JSON.stringify({shop_id:i.profitShop||"default",date_from:y($),date_to:y(_)})}),A&&(A.textContent=""),E()}catch(q){A&&(A.textContent=q.message||"拉取失败")}});const G=document.getElementById("btn-refresh-cs");G&&(G.onclick=()=>E());const at=document.getElementById("cs-inbox-form");at&&(at.onsubmit=async _=>{_.preventDefault();const $=new FormData(at),y=document.getElementById("cs-form-error");try{await S("/api/workflow/cs/inbox",{method:"POST",body:JSON.stringify({conversation_id:String($.get("conversation_id")||"").trim(),category:String($.get("category")||"").trim(),body:String($.get("body")||"").trim(),lang:String($.get("lang")||"th").trim()||"th",order_id:String($.get("order_id")||"").trim()||null})}),i.csNote="已写入本地 mock inbox。未外呼 TikTok 客服。",i.csError="",y&&(y.textContent=""),E()}catch(A){i.csError=A.message||"写入失败",y&&(y.textContent=i.csError)}}),document.querySelectorAll("[data-cs-draft-msg]").forEach(_=>{_.addEventListener("click",async()=>{const $=_.getAttribute("data-cs-draft-msg");try{const y=await S("/api/workflow/cs/draft",{method:"POST",body:JSON.stringify({message_id:$})});i.csNote=y.note||(y.whitelist?"白名单免审。":"已建 auto_reply 审批。"),i.csError="",E()}catch(y){i.csError=y.message||"生成草稿失败",E()}})}),document.querySelectorAll("[data-cs-send]").forEach(_=>{_.addEventListener("click",async()=>{const $=_.getAttribute("data-cs-send");try{const y=await S(`/api/workflow/cs/drafts/${encodeURIComponent($)}/send`,{method:"POST",body:"{}"});i.csNote=y.sent_stub?"已本地 stub 发送（sent_stub / dry_run）。未外呼 TikTok 客服。":y.note||"已处理。",i.csError="",E()}catch(y){i.csError=y.message||"发送失败",E()}})});const ct=document.getElementById("btn-refresh-outreach");ct&&(ct.onclick=()=>E()),document.querySelectorAll("[data-outreach-approve]").forEach(_=>{_.addEventListener("click",async()=>{var y;const $=_.getAttribute("data-outreach-approve");try{const A=await S(`/api/workflow/outreach/drafts/${encodeURIComponent($)}/approve`,{method:"POST",body:JSON.stringify({note:((y=document.getElementById("outreach-note"))==null?void 0:y.value)||""})});i.outreachNote=A.sent?"":"已标记通过。没有发送私信，没有寄样，也没有付款。",i.outreachError="",E()}catch(A){i.outreachError=A.message||"标记失败",E()}})}),document.querySelectorAll("[data-outreach-reject]").forEach(_=>{_.addEventListener("click",async()=>{var A;const $=(((A=document.getElementById("outreach-note"))==null?void 0:A.value)||"").trim();if(!$){i.outreachError="标记拒绝必须填写原因。",E();return}const y=_.getAttribute("data-outreach-reject");try{await S(`/api/workflow/outreach/drafts/${encodeURIComponent(y)}/reject`,{method:"POST",body:JSON.stringify({note:$})}),i.outreachNote="已标记拒绝。没有发送。",i.outreachError="",E()}catch(q){i.outreachError=q.message||"标记失败",E()}})}),document.querySelectorAll("[data-cs-approval]").forEach(_=>{_.addEventListener("click",$=>{$.preventDefault();const y=_.getAttribute("data-cs-approval");y&&st("approvals",{id:y})})})}const dt=new Kt(S,()=>({shop:i.activeShop||"default",generation:i.shopGeneration,route:i.route,user:i.user}));let Lt=0;async function E(s=!0){const t=++Lt,e=document.getElementById("app");if(i.route=ie(),(i.route!=="orders"||!i.user)&&et.clear(),(!Y[i.route]||!i.user)&&dt.clear(),i.route==="login"||!i.user){if(!i.user&&i.route!=="login")try{const o=await ee();i.user=o.user,window.__CSRF__=o.csrf_token||""}catch{i.route="login",history.replaceState({},"","/admin/login"),ht();return}if(i.route==="login"){ht();return}}Y[i.route]&&s&&(e.innerHTML=It('<p role="status" class="ops-empty">正在加载当前店铺运营数据…</p>'),Ct());try{s&&await Je()}catch(o){if(o.status===401){i.user=null,i.route="login",history.replaceState({},"","/admin/login"),ht();return}i.pageError=o.message}if(t!==Lt)return;const r=({...Object.fromEntries(Object.keys(Y).map(o=>[o,()=>dt.render()])),overview:At,workflows:xe,runs:qe,agents:Ie,approvals:pe,products:he,orders:me,scout:ye,spotcheck:$e,reports:we,profit:Se,cs:Ce,outreach:Te,settings:Pe}[i.route]||At)();e.innerHTML=It(r+(i.pageError?`<p class="error">${n(i.pageError)}</p>`:"")),i.pageError="",Ct(),Ge(),Y[i.route]&&dt.bind(e,E)}window.addEventListener("popstate",()=>E());E();
