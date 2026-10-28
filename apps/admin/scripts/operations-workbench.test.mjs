import test from 'node:test';
import assert from 'node:assert/strict';
import {OperationsWorkbench,optionalNumber,displayMetric,safeContentUrl,escapeHtml,readImage} from '../src/operations-workbench.js';
test('missing and zero remain different; invalid metrics rejected',()=>{assert.equal(optionalNumber(''),null);assert.equal(optionalNumber('0'),0);assert.throws(()=>optionalNumber('-1'));assert.throws(()=>optionalNumber('NaN'));assert.equal(displayMetric(null),'缺数据');assert.equal(displayMetric(0),'0');assert.equal(displayMetric(.05,true),'5.00%');});
test('source text escaped; previews restricted to protected content route',()=>{assert.equal(escapeHtml('<img onerror="x">'),'&lt;img onerror=&quot;x&quot;&gt;');assert.equal(safeContentUrl('javascript:alert(1)'),'');assert.equal(safeContentUrl('https://elsewhere/image.png'),'');assert.ok(safeContentUrl('/api/admin/operations/assets/a/content?shop_id=a'));});
test('late shop response cannot overwrite new shop data',async()=>{let ctx={shop:'a',generation:0,route:'candidates',user:{role:'owner'}};let release;const work=new OperationsWorkbench(async url=>url.includes('shop_id=a')?new Promise(r=>release=r):{items:[{id:'b',name:'B'}],total:1},()=>ctx);const first=work.load();ctx={...ctx,shop:'b',generation:1};await work.load();release({items:[{id:'a',name:'A'}],total:1});await first;assert.equal(work.data.candidates[0].id,'b');});
test('late failures cannot clear current shop',async()=>{let ctx={shop:'a',generation:0,route:'candidates',user:{}};let fail;const work=new OperationsWorkbench(async url=>url.includes('shop_id=a')?new Promise((r,j)=>fail=j):{items:[{id:'b'}],total:1},()=>ctx);const first=work.load();ctx={...ctx,shop:'b',generation:1};await work.load();fail(Error('old failure'));await first;assert.equal(work.error,'');assert.equal(work.data.candidates[0].id,'b');});
test('lists bounded to one page and page offsets preserved',async()=>{const calls=[];const ctx={shop:'a',generation:0,route:'candidates',user:{}};const work=new OperationsWorkbench(async url=>{calls.push(url);return {items:[{id:'a'}],total:1000};},()=>ctx);await work.load();assert.equal(calls.length,1);work.offset=50;await work.load();assert.ok(calls[1].includes('offset=50'));assert.equal(work.total,1000);});
test('viewer cannot see mutation controls',async()=>{const work=new OperationsWorkbench(async()=>({items:[{id:'1',name:'x'}],total:1}),()=>({shop:'a',generation:0,route:'candidates',user:{role:'viewer'}}));await work.load();assert.ok(!work.render().includes('id="ops-form"'));assert.ok(!work.render().includes('data-ops-delete'));});
test('image limits checked before reading bytes',async()=>{await assert.rejects(readImage({type:'image/svg+xml',size:10}),/仅接受/);await assert.rejects(readImage({type:'image/png',size:6*1024*1024}),/5 MiB/);});

test('editing older associations does not erase unseen linked rows',async()=>{const {associationOptions,preserveAssets}=await import('../src/operations-workbench.js');const recent=Array.from({length:50},(_,i)=>({id:`new-${i}`,name:`new ${i}`}));assert.equal(associationOptions(recent,'old-candidate')[0][0],'old-candidate');assert.deepEqual(preserveAssets(['old-asset','new-0'],recent,['new-2']),['old-asset','new-2']);});
test('date filters scoped to read requests and remain encoded',async()=>{const urls=[];const ctx={shop:'a',generation:0,route:'monitoring',user:{}};const w=new OperationsWorkbench(async u=>{urls.push(u);return {items:[],total:0};},()=>ctx);await w.load();w.filters={start_date:'2026-10-01',end_date:'2026-10-22'};await w.load();assert.ok(urls.some(u=>u.includes('metrics?')&&u.includes('start_date=2026-10-01')));assert.ok(!w.url('metrics',ctx).includes('start_date'));});

test('older same-route request cannot overwrite pagination total',async()=>{let resolve;let count=0;const ctx={shop:'a',generation:0,route:'candidates',user:{}};const w=new OperationsWorkbench(async()=>++count===1?new Promise(r=>resolve=r):{items:[{id:'new'}],total:200},()=>ctx);const first=w.load();await w.load();resolve({items:[{id:'old'}],total:5});await first;assert.equal(w.total,200);assert.equal(w.data.candidates[0].id,'new');});

test('related choices load one bounded page on demand without replacing form model',async()=>{const urls=[];const ctx={shop:'a',generation:0,route:'campaigns',user:{}};const w=new OperationsWorkbench(async url=>{urls.push(url);const isCandidate=url.includes('/candidates?');return {items:isCandidate?Array.from({length:50},(_,i)=>({id:`c-${url.includes('offset=50')?i+50:i}`,name:`C ${i}`})):[],total:isCandidate?120:0};},()=>ctx);await w.load();w.edit={id:'plan',name:'unsaved name',asset_ids:['old']};const added=await w.loadMoreReferences('candidates');assert.equal(added.length,50);assert.equal(w.data.candidates.length,100);assert.equal(w.references.candidates.offset,100);assert.equal(w.edit.name,'unsaved name');assert.equal(urls.filter(u=>u.includes('/candidates?')).length,2);assert.ok(urls.at(-1).includes('limit=50&offset=50'));});
test('related page arriving after shop switch cannot append into new shop',async()=>{let resolve;const ctx={shop:'a',generation:0,route:'campaigns',user:{}};const w=new OperationsWorkbench(async url=>url.includes('offset=50')?new Promise(r=>resolve=r):{items:url.includes('/candidates?')?Array.from({length:50},(_,i)=>({id:`${ctx.shop}-${i}`})):[],total:url.includes('/candidates?')?100:0},()=>ctx);await w.load();const pending=w.loadMoreReferences('candidates');ctx.shop='b';ctx.generation++;await w.load();resolve({items:[{id:'old-more'}],total:100});assert.equal(await pending,null);assert.ok(w.data.candidates.every(x=>x.id.startsWith('b-')));});

test('monitoring exposes plan filter and sends campaign_id to read API',async()=>{const calls=[];const ctx={shop:'a',generation:0,route:'monitoring',user:{role:'owner'}};const w=new OperationsWorkbench(async u=>{calls.push(u);return {items:u.includes('/campaigns?')?[{id:'plan-1',name:'Plan One'}]:[],total:0};},()=>ctx);await w.load();assert.match(w.filterForm(),/name="campaign_id"/);w.filters={campaign_id:'plan-1'};await w.load();assert.ok(calls.some(u=>u.includes('/metrics?')&&u.includes('campaign_id=plan-1')));ctx.route='operations';assert.ok(!w.filterForm().includes('name="campaign_id"'));});

test('candidate form renders extract url button and endpoint is wired',async()=>{const ctx={shop:'a',generation:0,route:'candidates',user:{role:'owner'}};const w=new OperationsWorkbench(async()=>({items:[],total:0}),()=>ctx);await w.load();const html=w.render();assert.ok(html.includes('data-ops-extract-url'));assert.ok(html.includes('自动提取'));assert.ok(w.url('candidates/extract-url',ctx).includes('/candidates/extract-url?shop_id=a'));});

for (const [source, degraded, status, label] of [
  ['mock', false, 'pending', 'Mock 模板'],
  ['openai', false, 'pending', '模型 openai'],
  ['openai', true, 'needs_revision', '降级模板'],
]) {
  test(`candidate generation shows ${label} and ${status} without approving`, async () => {
    const ctx = {shop:'a', generation:0, route:'candidates', user:{role:'operator'}};
    const requests = [], messages = [];
    const work = new OperationsWorkbench(async (url, options) => {
      requests.push({url, options});
      return {title:'Storage', source, degraded, status, approved:false};
    }, () => ctx);
    const button = {dataset:{opsQuickList:'candidate-1', name:'Storage'}, isConnected:true};
    const root = {
      querySelector: () => null,
      querySelectorAll: selector => selector === '[data-ops-quick-list]' ? [button] : [],
    };
    work.feedback = (text, error) => messages.push({text, error});
    work.bind(root, () => {});
    await button.onclick();
    assert.equal(requests.length, 1);
    assert.match(requests[0].url, /candidates\/candidate-1\/quick-list\?shop_id=a/);
    assert.equal(requests[0].options.method, 'POST');
    assert.ok(messages.at(-1).text.includes(label));
    assert.equal(button.textContent, status === 'needs_revision' ? '需修改' : '待审批');
    if (status === 'needs_revision') {
      assert.match(messages.at(-1).text, /修改候选商品事实后重新生成/);
      assert.ok(!messages.at(-1).text.includes('审批页'));
    }
    assert.ok(messages.every(x => !/直接批准|审核入库|已入库/.test(x.text)));
    assert.match(work.card('candidates', {id:'x', name:'Storage'}, false), /生成 Listing 草稿/);
  });
}

test('late candidate generation response does not show results in another shop', async () => {
  const ctx = {shop:'a', generation:0, route:'candidates', user:{role:'owner'}};
  let release;
  const messages = [];
  const work = new OperationsWorkbench(() => new Promise(resolve => release = resolve), () => ctx);
  const button = {dataset:{opsQuickList:'x'}, isConnected:true};
  work.feedback = text => messages.push(text);
  work.bind({
    querySelector: () => null,
    querySelectorAll: selector => selector === '[data-ops-quick-list]' ? [button] : [],
  }, () => {});
  const pending = button.onclick();
  ctx.shop = 'b';
  ctx.generation++;
  release({title:'Old shop', source:'mock', status:'pending'});
  await pending;
  assert.equal(messages.length, 1);
});


test("batch metric import handles multi-row success, partial failure with exact row index, and invalid JSON", async () => {
  const ctx = { shop: "a", generation: 0, route: "monitoring", user: { role: "owner" } };
  const calls = [];
  const messages = [];
  let shouldFailRow = false;

  const w = new OperationsWorkbench(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts?.body || "{}") });
    if (shouldFailRow && calls.length === 2) {
      throw Error("Simulated row failure");
    }
    return { ok: true };
  }, () => ctx);
  w.feedback = (text, err) => messages.push({ text, err });

  // Helper to trigger import form submit
  async function triggerImport(rowsJson, campaignId = "camp-1") {
    let preventDefaultCalled = false;
    const button = { disabled: false, isConnected: true };
    const fdMock = {
      get: (k) => k === "rows" ? rowsJson : k === "campaign_id" ? campaignId : null,
    };
    const importer = {
      querySelector: (sel) => sel === "button" ? button : null,
    };
    const root = {
      querySelector: (sel) => sel === "#ops-import" ? importer : null,
      querySelectorAll: () => [],
    };

    const origFormData = globalThis.FormData;
    globalThis.FormData = class {
      constructor(f) { return fdMock; }
    };
    try {
      w.bind(root, () => {});
      await importer.onsubmit({ preventDefault: () => { preventDefaultCalled = true; } });
    } finally {
      globalThis.FormData = origFormData;
    }
    return { preventDefaultCalled, button };
  }

  // 1. Successful 2-row import
  await triggerImport(JSON.stringify([
    { day: "2026-10-21", spend: 10 },
    { day: "2026-10-22", spend: 20 },
  ]));
  assert.equal(calls.length, 2);
  assert.equal(calls[0].body.campaign_id, "camp-1");
  assert.equal(calls[0].body.day, "2026-10-21");
  assert.equal(calls[1].body.day, "2026-10-22");
  assert.equal(w.note, "已导入 2 行。");

  // 2. Partial failure on 2nd row
  calls.length = 0;
  messages.length = 0;
  shouldFailRow = true;
  await triggerImport(JSON.stringify([
    { day: "2026-10-23", spend: 30 },
    { day: "2026-10-24", spend: 40 },
    { day: "2026-10-25", spend: 50 },
  ]));
  assert.equal(calls.length, 2); // 1st succeeded, 2nd failed, 3rd not sent
  assert.ok(messages.at(-1).err);
  assert.match(messages.at(-1).text, /已保存 1 行，第 2 行失败：Simulated row failure。修正后可重新导入。/);

  // 3. Malformed JSON
  messages.length = 0;
  await triggerImport("not valid json");
  assert.ok(messages.at(-1).err);
  assert.match(messages.at(-1).text, /第 1 行失败/);

  // 4. Non-array or empty
  messages.length = 0;
  await triggerImport("[]");
  assert.ok(messages.at(-1).err);
  assert.match(messages.at(-1).text, /1–100 行 JSON 数组/);
});

test("viewer role hides all mutation controls across materials, campaigns, and monitoring routes", async () => {
  for (const route of ["materials", "campaigns", "monitoring"]) {
    const w = new OperationsWorkbench(async () => ({ items: [{ id: "1", name: "x" }], total: 1 }), () => ({
      shop: "a",
      generation: 0,
      route,
      user: { role: "viewer" },
    }));
    await w.load();
    const html = w.render();
    assert.ok(!html.includes('id="ops-form"'), `route ${route} should not have #ops-form`);
    assert.ok(!html.includes("data-ops-delete"), `route ${route} should not have delete button`);
    if (route === "materials") {
      assert.ok(!html.includes('id="ops-draft-import"'));
    }
    if (route === "monitoring") {
      assert.ok(!html.includes('id="ops-import"'));
    }
  }
});

test("switching shop halts ongoing batch metric import without continuing into new shop", async () => {
  const ctx = { shop: "a", generation: 0, route: "monitoring", user: { role: "owner" } };
  const calls = [];
  const w = new OperationsWorkbench(async (url, opts) => {
    calls.push({ url, shop: ctx.shop, body: JSON.parse(opts?.body || "{}") });
    if (calls.length === 1) {
      // simulate shop switch mid-import
      ctx.shop = "b";
      ctx.generation++;
    }
    return { ok: true };
  }, () => ctx);

  const fdMock = {
    get: (k) => k === "rows" ? JSON.stringify([{ day: "2026-10-21" }, { day: "2026-10-22" }, { day: "2026-10-23" }]) : "camp-1",
  };
  const importer = { querySelector: () => ({ disabled: false, isConnected: true }) };
  const root = { querySelector: (s) => s === "#ops-import" ? importer : null, querySelectorAll: () => [] };

  const origFormData = globalThis.FormData;
  globalThis.FormData = class { constructor() { return fdMock; } };
  try {
    w.bind(root, () => {});
    await importer.onsubmit({ preventDefault: () => {} });
  } finally {
    globalThis.FormData = origFormData;
  }

  // Only the first call should have fired before shop switch aborted the rest
  assert.equal(calls.length, 1);
  assert.equal(calls[0].shop, "a");
  assert.equal(w.note, ""); // note for shop b should not be set with shop a progress
});

test("candidate card renders research and verify buttons for operator/owner, hidden for viewer", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "operator" } };
  const w = new OperationsWorkbench(async () => ({ items: [{ id: "c1", name: "Box", facts: "20L" }], total: 1 }), () => ctx);
  await w.load();
  const html = w.render();
  assert.ok(html.includes('data-ops-research="c1"'));
  assert.ok(html.includes('data-ops-verify="c1"'));

  const viewerCtx = { ...ctx, user: { role: "viewer" } };
  const wViewer = new OperationsWorkbench(async () => ({ items: [{ id: "c1", name: "Box", facts: "20L" }], total: 1 }), () => viewerCtx);
  await wViewer.load();
  const vHtml = wViewer.render();
  assert.ok(!vHtml.includes("data-ops-research"));
  assert.ok(!vHtml.includes("data-ops-verify"));
});

test("research recommendations are displayed separately from confirmed facts and generation does not alter candidate", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  const calls = [];
  const messages = [];
  let rerendered = false;

  const mockRun = {
    id: "run-research-1",
    module: "research",
    status: "succeeded",
    model_channel: "mock",
    input_version_digest: "digest-1",
    output: {
      recommendation: "shortlist",
      facts: ["容量 20L", "可叠放"],
      hypotheses: ["适合泰国潮湿季节"],
      missing_info: ["缺少外箱包装尺寸"],
    },
  };

  const w = new OperationsWorkbench(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts?.body || "{}") });
    return mockRun;
  }, () => ctx);
  w.feedback = (text, err) => messages.push({ text, err });
  w.data = { candidates: [{ id: "c1", name: "Box", facts: "原始事实", status: "researching" }] };

  const researchBtn = { dataset: { opsResearch: "c1", name: "Box" }, isConnected: true };
  const root = {
    querySelector: () => null,
    querySelectorAll: (sel) => sel === "[data-ops-research]" ? [researchBtn] : [],
  };

  w.bind(root, () => { rerendered = true; });
  await researchBtn.onclick();

  // 1. Assert API call made
  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.includes("/work-model-runs?"));
  assert.equal(calls[0].body.module, "research");
  assert.equal(calls[0].body.object_id, "c1");

  // 2. Candidate original facts & status NOT modified by suggestion generation
  assert.equal(w.data.candidates[0].facts, "原始事实");
  assert.equal(w.data.candidates[0].status, "researching");

  // 3. Render shows suggestions separate from facts, with adoption button
  const rendered = w.card("candidates", w.data.candidates[0], false);
  assert.ok(rendered.includes("AI 研究建议"));
  assert.ok(rendered.includes("容量 20L； 可叠放"));
  assert.ok(rendered.includes("适合泰国潮湿季节"));
  assert.ok(rendered.includes("缺少外箱包装尺寸"));
  assert.ok(rendered.includes('data-ops-adopt-run="run-research-1"'));
});

test("adopting research updates candidate facts and status with version check", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  const calls = [];
  const messages = [];

  const w = new OperationsWorkbench(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts?.body || "{}") });
    return {
      id: "c1",
      name: "Box",
      facts: "原始事实\n容量 20L",
      status: "shortlisted",
      version_digest: "digest-2",
    };
  }, () => ctx);
  w.feedback = (text, err) => messages.push({ text, err });
  w.data = { candidates: [{ id: "c1", name: "Box", facts: "原始事实", status: "researching" }] };
  w.candidateSuggestions = {
    c1: { id: "run-research-1", module: "research", input_version_digest: "digest-1" },
  };

  const adoptBtn = {
    dataset: { opsAdoptRun: "run-research-1", candId: "c1", version: "digest-1" },
    isConnected: true,
  };
  const root = {
    querySelector: () => null,
    querySelectorAll: (sel) => sel === "[data-ops-adopt-run]" ? [adoptBtn] : [],
  };

  w.bind(root, () => {});
  await adoptBtn.onclick();

  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.includes("/candidates/c1/adopt-run?"));
  assert.equal(calls[0].body.run_id, "run-research-1");
  assert.equal(calls[0].body.expected_version_digest, "digest-1");
  assert.equal(w.data.candidates[0].status, "shortlisted");
  assert.ok(!w.candidateSuggestions.c1); // suggestion cleared after adoption
  assert.ok(messages.some((m) => m.text.includes("成功采纳")));
});

test("version conflict on adoption shows error and preserves original candidate state", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  const messages = [];

  const w = new OperationsWorkbench(async () => {
    throw Error("版本冲突：商品候选事实自模型分析后已被修改");
  }, () => ctx);
  w.feedback = (text, err) => messages.push({ text, err });
  w.data = { candidates: [{ id: "c1", name: "Box", facts: "新修改的事实", status: "researching" }] };
  w.candidateSuggestions = {
    c1: { id: "old-run", module: "research", input_version_digest: "old-digest" },
  };

  const adoptBtn = {
    dataset: { opsAdoptRun: "old-run", candId: "c1", version: "old-digest" },
    isConnected: true,
  };
  const root = {
    querySelector: () => null,
    querySelectorAll: (sel) => sel === "[data-ops-adopt-run]" ? [adoptBtn] : [],
  };

  w.bind(root, () => {});
  await adoptBtn.onclick();

  assert.equal(w.data.candidates[0].facts, "新修改的事实");
  assert.ok(messages.some((m) => m.err && m.text.includes("版本冲突")));
  assert.equal(adoptBtn.disabled, false);
});

test("switching shop isolates late research response from showing in new shop", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  let release;
  const messages = [];

  const w = new OperationsWorkbench(() => new Promise((resolve) => release = resolve), () => ctx);
  w.feedback = (text) => messages.push(text);
  const researchBtn = { dataset: { opsResearch: "c1", name: "Box" }, isConnected: true };
  const root = {
    querySelector: () => null,
    querySelectorAll: (sel) => sel === "[data-ops-research]" ? [researchBtn] : [],
  };

  w.bind(root, () => {});
  const pending = researchBtn.onclick();

  // switch to shop b
  ctx.shop = "b";
  ctx.generation++;

  release({
    id: "run-late",
    module: "research",
    output: { recommendation: "shortlist" },
  });
  await pending;

  // Verify suggestion is not saved in new shop
  assert.ok(!w.candidateSuggestions?.c1);
  assert.equal(messages.length, 1); // only the initial "正在为..." message
});

test("copy generation, quality check and asset adoption call the right endpoints", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  const calls = [];
  const messages = [];
  const w = new OperationsWorkbench(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts?.body || "{}") });
    if (calls.length === 1) {
      return { id: "run-copy-1", module: "write_copy", status: "succeeded", model_channel: "spy",
        output: { title_th: "กล่อง", body_th: "รายละเอียด", title_en: "Box", body_en: "Details" } };
    }
    if (calls.length === 2) {
      return { id: "run-q-1", module: "quality_check", status: "succeeded", model_channel: "spy",
        output: { decision: "pass", checks: { facts_supported: true, language_ok: true,
          sensitive_claims_checked: true, copyright_source_present: true }, issues: [], required_edits: [] } };
    }
    return { id: "asset-1", status: "usable", replayed: false };
  }, () => ctx);
  w.feedback = (text, err) => messages.push({ text, err });
  w.data = { candidates: [{ id: "c1", name: "Box", facts: "容量 20L", status: "researching" }] };

  const copyBtn = { dataset: { opsWriteCopy: "c1", name: "Box" }, isConnected: true };
  const qBtn = { dataset: { opsQualityCheck: "run-copy-1", candId: "c1", name: "Box" }, isConnected: true };
  const adoptBtn = { dataset: { opsAdoptAsset: "run-q-1", candId: "c1" }, isConnected: true };
  const root = {
    querySelector: () => null,
    querySelectorAll: (sel) => ({
      "[data-ops-write-copy]": [copyBtn],
      "[data-ops-quality-check]": [qBtn],
      "[data-ops-adopt-asset]": [adoptBtn],
    }[sel] || []),
  };
  w.bind(root, () => {});

  await copyBtn.onclick();
  assert.equal(calls[0].body.module, "write_copy");
  assert.equal(calls[0].body.object_id, "c1");
  assert.ok(calls[0].url.includes("/work-model-runs?"));

  await qBtn.onclick();
  assert.equal(calls[1].body.module, "quality_check");
  assert.equal(calls[1].body.scope.copy_run_id, "run-copy-1");

  await adoptBtn.onclick();
  assert.ok(calls[2].url.includes("/candidates/c1/adopt-asset-run?"));
  assert.equal(calls[2].body.quality_run_id, "run-q-1");
  assert.ok(messages.some((m) => m.text.includes("可用素材")));
});

test("copy panel shows server verdict and hides adoption when quality did not pass", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  const w = new OperationsWorkbench(async () => ({}), () => ctx);
  await w.load();
  w.copyState = {
    c1: {
      copy: { status: "succeeded", model_channel: "spy", output: { title_th: "กล่อง" } },
      quality: { id: "run-q-1", status: "succeeded", output: { decision: "needs_review",
        checks: { facts_supported: true, language_ok: false, sensitive_claims_checked: true, copyright_source_present: true },
        issues: ["泰文标题需要复核"], required_edits: ["重写泰文标题"] } },
    },
  };
  const html = w.card("candidates", { id: "c1", name: "Box", facts: "20L", status: "researching" }, false);
  assert.ok(html.includes("待人工复核"));
  assert.ok(html.includes("泰文标题需要复核"));
  assert.ok(html.includes("重写泰文标题"));
  assert.ok(!html.includes("data-ops-adopt-asset"));
});

test("viewer sees copy panel without mutation controls", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "viewer" } };
  const w = new OperationsWorkbench(async () => ({}), () => ctx);
  await w.load();
  w.copyState = {
    c1: {
      copy: { status: "succeeded", output: { title_th: "กล่อง" } },
      quality: { id: "run-q-1", status: "succeeded", output: { decision: "pass",
        checks: { facts_supported: true, language_ok: true, sensitive_claims_checked: true, copyright_source_present: true } } },
    },
  };
  const html = w.card("candidates", { id: "c1", name: "Box", facts: "20L", status: "researching" }, true);
  assert.ok(html.includes("data-copy-panel"));
  assert.ok(!html.includes("data-ops-adopt-asset"));
  assert.ok(!html.includes("data-ops-quality-check"));
});

test("switching shop clears copy state so late results cannot surface in the new shop", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  let release;
  const messages = [];
  const w = new OperationsWorkbench(() => new Promise((resolve) => release = resolve), () => ctx);
  w.feedback = (text) => messages.push(text);
  const copyBtn = { dataset: { opsWriteCopy: "c1", name: "Box" }, isConnected: true };
  const root = { querySelector: () => null, querySelectorAll: (sel) => (sel === "[data-ops-write-copy]" ? [copyBtn] : []) };
  w.bind(root, () => {});
  const pending = copyBtn.onclick();
  ctx.shop = "b";
  ctx.generation++;
  release({ id: "run-copy-late", module: "write_copy", status: "succeeded", output: {} });
  await pending;
  assert.ok(!w.copyState?.c1);
});

// ---- O-18：投放计划助手 / 指标复盘 / 总控建议 ----

test("plan assistant shows server suggestion and adoption button for owner only", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  const w = new OperationsWorkbench(async () => ({ items: [], total: 0 }), () => ctx);
  await w.load();
  w.planState = {
    c1: { run: { id: "run-plan-1", status: "succeeded", output: {
      name: "泰区首发计划", objective: "验证点击与转化",
      audience_hypotheses: ["居家收纳人群"], budget_note: "首周小额测试",
      execution_checklist: ["上传素材", "回填计划编号"],
      tracking_fields: ["campaign_id","day","currency","impressions","clicks","spend","revenue","orders"],
      missing: ["计划编号待回填"] } } },
  };
  const html = w.card("candidates", { id: "c1", name: "Box", facts: "20L", status: "researching" }, false);
  assert.ok(html.includes("data-plan-panel"));
  assert.ok(html.includes("泰区首发计划"));
  assert.ok(html.includes("验证点击与转化"));
  assert.ok(html.includes("首周小额测试"));
  assert.ok(html.includes("上传素材"));
  assert.ok(html.includes("tracking_fields") === false);
  assert.ok(html.includes("campaign_id"));
  assert.ok(html.includes("data-ops-adopt-campaign"));
  assert.ok(html.includes("草稿"));
  // viewer 只读：无采纳按钮
  const ro = w.card("candidates", { id: "c1", name: "Box", facts: "20L", status: "researching" }, true);
  assert.ok(ro.includes("data-plan-panel"));
  assert.ok(!ro.includes("data-ops-adopt-campaign"));
});

test("plan assistant failure shows failure code instead of adoption", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  const w = new OperationsWorkbench(async () => ({ items: [], total: 0 }), () => ctx);
  await w.load();
  w.planState = { c1: { run: { id: "r", status: "failed", failure_code: "WORK_MODEL_ASSET_OUT_OF_SCOPE", output: null } } };
  const html = w.card("candidates", { id: "c1", name: "Box", facts: "20L", status: "researching" }, false);
  assert.ok(html.includes("WORK_MODEL_ASSET_OUT_OF_SCOPE"));
  assert.ok(!html.includes("data-ops-adopt-campaign"));
});

test("review panel shows server metrics and states ratios are recomputed server-side", async () => {
  const ctx = { shop: "a", generation: 0, route: "campaigns", user: { role: "owner" } };
  const w = new OperationsWorkbench(async () => ({ items: [], total: 0 }), () => ctx);
  await w.load();
  w.reviewState = { p1: { run: { id: "r", status: "succeeded", output: {
    scope: { campaign_id: "p1", currency: "THB", from: "2026-10-01" },
    known_metrics: { impressions: 1000, clicks: 25, spend: 20.5, revenue: 65, orders: 2,
      ctr: 0.025, cpc: 0.82, roas: 3.1707317073170733 },
    observations: ["点击率 2.5%"], data_gaps: ["缺少来源拆分"], next_actions: ["补齐来源"] } } } };
  const html = w.card("campaigns", { id: "p1", name: "Plan", platform: "tiktok", status: "draft", notes: "" }, false);
  assert.ok(html.includes("data-review-panel"));
  assert.ok(html.includes("THB"));
  assert.ok(html.includes("2.50%"));
  assert.ok(html.includes("0.82"));
  assert.ok(html.includes("原始指标不被复盘结果改写"));
  // 复盘是只读呈现：不提供执行按钮
  assert.ok(!html.includes("data-ops-adopt-campaign"));
});

test("review failure keeps failure reason visible", async () => {
  const ctx = { shop: "a", generation: 0, route: "campaigns", user: { role: "owner" } };
  const w = new OperationsWorkbench(async () => ({ items: [], total: 0 }), () => ctx);
  await w.load();
  w.reviewState = { p1: { run: { id: "r", status: "failed", failure_code: "WORK_MODEL_METRICS_TAMPERED", output: null } } };
  const html = w.card("campaigns", { id: "p1", name: "Plan", platform: "tiktok", status: "draft", notes: "" }, false);
  assert.ok(html.includes("WORK_MODEL_METRICS_TAMPERED"));
});

test("plan assistant request sends only same-shop assets of the candidate", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  const requests = [];
  const w = new OperationsWorkbench(async (url, options) => {
    requests.push({ url, options });
    return { items: [{ id: "asset-1", candidate_id: "c1" }, { id: "asset-2", candidate_id: "other" }], total: 2 };
  }, () => ctx);
  w.feedback = () => {};
  await w.load();
  requests.length = 0;
  const btn = { dataset: { opsPlan: "c1", name: "Box" }, isConnected: true };
  const root = { querySelector: () => null, querySelectorAll: (sel) => (sel === "[data-ops-plan]" ? [btn] : []) };
  w.bind(root, () => {});
  await btn.onclick();
  const runs = requests.filter((r) => r.options && r.options.method === "POST");
  assert.equal(runs.length, 1);
  const body = JSON.parse(runs[0].options.body);
  assert.equal(body.module, "prepare_campaign");
  assert.equal(body.object_id, "c1");
  assert.deepEqual(body.scope.asset_ids, ["asset-1"]);
});

test("review request binds campaign id and current filters", async () => {
  const ctx = { shop: "a", generation: 0, route: "campaigns", user: { role: "owner" } };
  const requests = [];
  const w = new OperationsWorkbench(async (url, options) => {
    requests.push({ url, options });
    return { items: [], total: 0 };
  }, () => ctx);
  w.feedback = () => {};
  await w.load();
  requests.length = 0;
  w.filters = { start_date: "2026-10-01", end_date: "2026-10-31", source: "manual" };
  const btn = { dataset: { opsReview: "p1", name: "Plan" }, isConnected: true };
  const root = { querySelector: () => null, querySelectorAll: (sel) => (sel === "[data-ops-review]" ? [btn] : []) };
  w.bind(root, () => {});
  await btn.onclick();
  assert.equal(requests.length, 1);
  const body = JSON.parse(requests[0].options.body);
  assert.equal(body.module, "record_metrics");
  assert.equal(body.object_id, "p1");
  assert.equal(body.scope.from, "2026-10-01");
  assert.equal(body.scope.to, "2026-10-31");
  assert.equal(body.scope.source, "manual");
});

test("late plan response does not surface in another shop", async () => {
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  let release;
  const w = new OperationsWorkbench(() => new Promise((resolve) => release = resolve), () => ctx);
  w.feedback = () => {};
  const btn = { dataset: { opsPlan: "c1", name: "Box" }, isConnected: true };
  const root = { querySelector: () => null, querySelectorAll: (sel) => (sel === "[data-ops-plan]" ? [btn] : []) };
  w.bind(root, () => {});
  const pending = btn.onclick();
  ctx.shop = "b";
  ctx.generation++;
  release({ id: "run-plan-late", status: "succeeded", output: {} });
  await pending;
  assert.ok(!w.planState?.c1);
});

// ---- O-19：调用记录 / 结果面板 ----

test("records panel lists metadata page by page and counts exactly", async () => {
  const ctx = { shop: "a", generation: 0, route: "records", user: { role: "owner" } };
  const urls = [];
  const w = new OperationsWorkbench(async (url) => {
    urls.push(url);
    return { items: Array.from({ length: 50 }, (_, i) => ({ id: `c-${i}`, module: "research",
      model_channel: "mock", prompt_version: "work-models-v1.2", created_at: "2026-10-27",
      input_object_ids: [], output_object_ids: [], human_changes: [], human_review: false,
      failure_reason: null, run: { id: `r-${i}`, status: "succeeded" } })), total: 61 };
  }, () => ctx);
  await w.load();
  assert.equal(w.records.length, 50);
  assert.equal(w.recordsTotal, 61);
  assert.ok(urls[0].includes("work-model-records?"));
  assert.ok(urls[0].includes("limit=50"));
  assert.ok(urls[0].includes("offset=0"));
  const html = w.render();
  assert.ok(html.includes("调用记录 · 61"));
  assert.ok(html.includes("第 1 页 · 每页 50 条 · 本页 50 条"));
  // 下一页可用（还有 11 条）
  assert.ok(html.includes("data-ops-record-next"));
});

test("records panel opens detail on demand and body is fetched separately", async () => {
  const ctx = { shop: "a", generation: 0, route: "records", user: { role: "owner" } };
  const requests = [];
  const w = new OperationsWorkbench(async (url, opts) => {
    requests.push({ url, opts });
    if (url.includes("work-model-records/")) {
      return { call: { id: "call-1", module: "research", model_channel: "mock", source: "api",
        human_changes: ["人工修改文案正文"], human_review: true },
        run: { id: "run-1", module: "research", status: "succeeded", failure_code: null,
          failure_reason: null, output: { recommendation: "shortlisted" },
          raw_output: '{"recommendation":"shortlisted"}', context: { candidate_digest: "d1" },
          output_object_ids: ["asset-1"], input_object_ids: ["cand-1"] } };
    }
    return { items: [{ id: "call-1", module: "research", model_channel: "mock",
      prompt_version: "v1", created_at: "2026-10-27", input_object_ids: [],
      output_object_ids: [], human_changes: [], human_review: false, failure_reason: null,
      run: { id: "run-1", status: "succeeded" } }], total: 1 };
  }, () => ctx);
  await w.load();
  assert.equal(requests.length, 1);
  assert.ok(!requests[0].url.includes("work-model-records/"), "列表不读详情");

  const btn = { dataset: { opsRecordOpen: "call-1" }, isConnected: true };
  const root = { querySelector: () => null,
    querySelectorAll: (s) => (s === "[data-ops-record-open]" ? [btn] : []) };
  let rerendered = false;
  w.bind(root, () => { rerendered = true; });
  await btn.onclick();
  assert.equal(requests.length, 2);
  assert.ok(requests[1].url.includes("work-model-records/call-1"));
  assert.ok(!requests[1].opts || !requests[1].opts.method, "详情是只读 GET");
  const html = w.render();
  assert.ok(html.includes("结构化结果"));
  assert.ok(html.includes("人工修改文案正文"));
  assert.ok(html.includes("shortlisted"));
});

test("records panel shows failure code and failure reason", async () => {
  const ctx = { shop: "a", generation: 0, route: "records", user: { role: "owner" } };
  const w = new OperationsWorkbench(async () => ({ items: [{ id: "call-x", module: "write_copy",
    model_channel: "mock", prompt_version: "v1", created_at: "2026-10-27", input_object_ids: [],
    output_object_ids: [], human_changes: [], human_review: false,
    failure_reason: "WORK_MODEL_SENSITIVE_LEAK: 输出含凭据",
    run: { id: "r-x", status: "failed", failure_code: "WORK_MODEL_SENSITIVE_LEAK" } }], total: 1 }), () => ctx);
  await w.load();
  const html = w.render();
  assert.ok(html.includes("WORK_MODEL_SENSITIVE_LEAK"));
  assert.ok(html.includes("输出含凭据"));
});

test("records panel renders model html as text, not markup", async () => {
  const ctx = { shop: "a", generation: 0, route: "records", user: { role: "owner" } };
  const w = new OperationsWorkbench(async () => ({ items: [{ id: "call-1", module: "research",
    model_channel: "mock", prompt_version: "v1", created_at: "2026-10-27", input_object_ids: [],
    output_object_ids: [], human_changes: [], human_review: false, failure_reason: null,
    run: { id: "run-1", status: "succeeded" } }], total: 1 }), () => ctx);
  await w.load();
  w.recordDetailOpen = "call-1";
  w.recordDetail = { "call-1": { call: { id: "call-1", module: "research", model_channel: "mock",
      source: "api", human_changes: [], human_review: false },
    run: { id: "run-1", module: "research", status: "succeeded", failure_code: null,
      failure_reason: null, output: { note: "<img src=x onerror=alert(1)>" },
      raw_output: "<script>alert(2)</script>", context: {}, output_object_ids: [], input_object_ids: [] } } };
  const html = w.render();
  assert.ok(!html.includes("<img src=x onerror=alert(1)>"), "模型 HTML 必须被转义");
  assert.ok(!html.includes("<script>alert(2)</script>"), "原始输出必须被转义");
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"));
  assert.ok(html.includes("&lt;script&gt;alert(2)&lt;/script&gt;"));
});

test("records panel is read only: no generate or adopt controls", async () => {
  const ctx = { shop: "a", generation: 0, route: "records", user: { role: "owner" } };
  const w = new OperationsWorkbench(async () => ({ items: [], total: 0 }), () => ctx);
  await w.load();
  const html = w.render();
  assert.ok(!html.includes("data-ops-adopt-run"));
  assert.ok(!html.includes("data-ops-adopt-asset"));
  assert.ok(!html.includes("data-ops-adopt-campaign"));
  assert.ok(!html.includes("data-ops-research"));
  assert.ok(!html.includes("data-ops-write-copy"));
  assert.ok(!html.includes("data-ops-quick-list"));
  assert.ok(!html.includes('id="ops-form"'));
});

test("records reading triggers no write request", async () => {
  const ctx = { shop: "a", generation: 0, route: "records", user: { role: "owner" } };
  const requests = [];
  const w = new OperationsWorkbench(async (url, opts) => {
    requests.push({ url, method: (opts && opts.method) || "GET" });
    return { items: [], total: 0 };
  }, () => ctx);
  await w.load();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].method, "GET");
});

test("switching shop clears records and isolates late record response", async () => {
  const ctx = { shop: "a", generation: 0, route: "records", user: { role: "owner" } };
  let release;
  const w = new OperationsWorkbench((url) => url.includes("shop_id=a")
    ? new Promise((r) => { release = r; })
    : { items: [{ id: "b-1", module: "verify", model_channel: "mock", prompt_version: "v1",
        created_at: "2026-10-27", input_object_ids: [], output_object_ids: [],
        human_changes: [], human_review: false, failure_reason: null,
        run: { id: "rb", status: "succeeded" } }], total: 1 }, () => ctx);
  const first = w.load();
  ctx.shop = "b";
  ctx.generation++;
  await w.load();
  release({ items: [{ id: "a-1", module: "research", model_channel: "mock", prompt_version: "v1",
      created_at: "2026-10-27", input_object_ids: [], output_object_ids: [],
      human_changes: [], human_review: false, failure_reason: null,
      run: { id: "ra", status: "succeeded" } }], total: 1 });
  await first;
  assert.equal(w.records.length, 1);
  assert.equal(w.records[0].id, "b-1");
});

test("late record detail response does not land in the new shop", async () => {
  const ctx = { shop: "a", generation: 0, route: "records", user: { role: "owner" } };
  let release;
  const w = new OperationsWorkbench((url) => url.includes("/work-model-records/")
    ? new Promise((r) => { release = r; })
    : { items: [], total: 0 }, () => ctx);
  await w.load();
  const pending = w.openRecord("call-late");
  ctx.shop = "b";
  ctx.generation++;
  release({ call: { id: "call-late" }, run: { id: "run-late" } });
  await pending;
  assert.ok(!w.recordDetail?.["call-late"]);
});

test("records module filter resets offset and clears open detail", async () => {
  const ctx = { shop: "a", generation: 0, route: "records", user: { role: "owner" } };
  const urls = [];
  const w = new OperationsWorkbench(async (url) => { urls.push(url); return { items: [], total: 0 }; }, () => ctx);
  await w.load();
  w.recordsOffset = 50;
  w.recordDetailOpen = "call-1";
  w.recordDetail = { "call-1": { call: { id: "call-1" } } };
  const filter = { querySelector: (s) => (s === "#ops-record-filter" ? filter : null), querySelectorAll: () => [] };
  filter.querySelector = (s) => (s === "#ops-record-filter" ? filter : null);
  filter.onsubmit = null;
  const origFormData = globalThis.FormData;
  globalThis.FormData = class { constructor() { return { get: () => "verify" }; } };
  try {
    w.bind(filter, () => {});
    await filter.onsubmit({ preventDefault: () => {} });
  } finally {
    globalThis.FormData = origFormData;
  }
  assert.equal(w.recordModule, "verify");
  assert.equal(w.recordsOffset, 0);
  assert.equal(w.recordDetailOpen, "");
  assert.deepEqual(w.recordDetail, {});
});

test("viewer can read the records panel but sees no mutation entry", async () => {
  const ctx = { shop: "a", generation: 0, route: "records", user: { role: "viewer" } };
  const w = new OperationsWorkbench(async () => ({ items: [{ id: "c-1", module: "research",
    model_channel: "mock", prompt_version: "v1", created_at: "2026-10-27", input_object_ids: [],
    output_object_ids: [], human_changes: [], human_review: false, failure_reason: null,
    run: { id: "r-1", status: "succeeded" } }], total: 1 }), () => ctx);
  await w.load();
  const html = w.render();
  assert.ok(html.includes("调用记录 · 1"));
  assert.ok(!html.includes("data-ops-adopt-run"));
  assert.ok(!html.includes('id="ops-form"'));
});

test("copy panel renders body from the run output, not from the top level", async () => {
  // 运行记录的结构化正文在 output 里；顶层没有 title_th。
  // 真实浏览器验收发现的存量缺陷：正文渲染成四个空行。
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  const w = new OperationsWorkbench(async () => ({}), () => ctx);
  await w.load();
  w.copyState = {
    c1: {
      copy: { id: "run-copy-1", status: "succeeded", model_channel: "spy", output: {
        title_th: "กล่องจัดเก็บ", body_th: "ความจุ 20 ลิตร",
        title_en: "Storage Box", body_en: "20L capacity." } },
    },
  };
  const html = w.card("candidates", { id: "c1", name: "Box", facts: "20L", status: "researching" }, false);
  assert.ok(html.includes("กล่องจัดเก็บ"), "泰文标题应来自 output.title_th");
  assert.ok(html.includes("20L capacity."), "英文正文应来自 output.body_en");
  assert.ok(html.includes("data-ops-quality-check"));
});

test("panels survive a cleared nested state instead of throwing", async () => {
  // 采纳成功后会删掉 state.run / state.quality，状态对象仍在但嵌套值为空。
  // 真实浏览器验收发现的存量缺陷：解引用空 run 会让整页卡在「正在加载…」。
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  const w = new OperationsWorkbench(async () => ({}), () => ctx);
  await w.load();
  const candidate = { id: "c1", name: "Box", facts: "20L", status: "researching" };
  w.planState = { c1: { run: undefined } };
  w.copyState = { c1: { copy: { id: "r", status: "succeeded", output: { title_th: "x" } }, quality: undefined } };
  w.reviewState = { c1: { run: undefined } };
  const html = w.card("candidates", candidate, false);
  assert.ok(!html.includes("data-plan-panel"));
  assert.ok(!html.includes("data-review-panel"));
  assert.ok(html.includes("data-copy-panel"));
});

test("operation feedback survives the re-render that follows it", async () => {
  // feedback() 只写 DOM 的话，随后的重绘会把提示整块抹掉，用户看不到操作结果。
  const ctx = { shop: "a", generation: 0, route: "candidates", user: { role: "owner" } };
  const w = new OperationsWorkbench(async () => ({}), () => ctx);
  await w.load();
  // feedback() 同时写 DOM 与状态；node 环境没有 document，桩掉 DOM 分支。
  const origDocument = globalThis.document;
  globalThis.document = { getElementById: () => null };
  try {
    w.feedback("文案已采纳为可用素材");
  } finally {
    globalThis.document = origDocument;
  }
  const html = w.render();
  assert.ok(html.includes("文案已采纳为可用素材"), "操作提示必须在重绘后仍然可见");
});
