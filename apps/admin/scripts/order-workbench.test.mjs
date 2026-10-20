import test from 'node:test';
import assert from 'node:assert/strict';
import { OrderWorkbench, orderPhase } from '../src/order-workbench.js';

function fixture({ manual = false } = {}) {
  const rows = new Map();
  const calls = [];
  let serial = 0;
  const row = (id) => {
    if (!rows.has(id)) rows.set(id, { dropship: null, tracking: null });
    return rows.get(id);
  };
  const approval = (payload) => { const n = ++serial; return { id: `approval-${n}`, status: 'pending', payload, payload_hash: `hash-${n}` }; };
  const request = async (path, options = {}) => {
    const method = options.method || 'GET';
    const body = JSON.parse(options.body || '{}');
    // 真实实现给订单接口带 shop_id 查询串；fixture 用去掉查询串的路径做路由。
    const clean = path.split('?')[0];
    calls.push({ path: clean, method, body });
    const decide = clean.match(/\/approvals\/([^/]+)\/(approve|reject)$/);
    if (decide) {
      for (const data of rows.values()) for (const kind of ['dropship', 'tracking']) {
        if (data[kind]?.approval.id === decide[1]) data[kind].approval.status = decide[2] === 'approve' ? 'approved' : 'rejected';
      }
      return {};
    }
    const [, id, suffix] = clean.match(/\/orders\/([^/]+)\/(.*)$/);
    const data = row(decodeURIComponent(id));
    const missing = () => { throw Object.assign(new Error('not found'), { status: 404 }); };
    if (method === 'GET') {
      if (suffix === 'dropship/manual-sheet') return { copy_text: 'fixture instructions' };
      return structuredClone(data[suffix] || missing());
    }
    if (suffix === 'dropship/draft') data.dropship = { draft: { status: 'pending_approval', payload: { mode: manual ? 'manual' : 'mock' } }, approval: approval({ order_id: id }) };
    if (suffix === 'dropship/execute') {
      assert.equal(data.dropship.approval.status, 'approved');
      data.dropship.draft.status = manual ? 'awaiting_manual_po' : 'executed';
      data.dropship.draft.result = manual ? {} : { carrier: 'JNT', tracking_no: 'JT246813579012' };
    }
    if (suffix === 'dropship/record-po') {
      assert.equal(data.dropship.draft.status, 'awaiting_manual_po');
      data.dropship.draft.status = 'executed';
      data.dropship.draft.external_po_id = body.alibaba_order_id;
    }
    if (suffix === 'tracking/draft') data.tracking = { draft: { status: 'pending_approval', payload: Object.keys(body).length ? body : data.dropship.draft.result }, approval: approval(Object.keys(body).length ? body : data.dropship.draft.result) };
    if (suffix === 'tracking/push') {
      assert.equal(data.tracking.approval.status, 'approved');
      data.tracking.draft.status = 'executed';
      data.tracking.shipment = { ...data.tracking.draft.payload };
    }
    return {};
  };
  const work = new OrderWorkbench(request);
  work.role = 'owner';
  return { work, request, row, calls, posts: () => calls.filter((x) => x.method === 'POST') };
}

test('opening reads progress; preparing pauses before human approval', async () => {
  const f = fixture();
  await f.work.open('fixture-order');
  assert.equal(f.posts().length, 0);
  await f.work.act('continue');
  assert.equal(orderPhase(f.work.data), 'approve_dropship');
  assert.deepEqual(f.posts().map((x) => x.path.split('/').slice(-2).join('/')), ['dropship/draft']);
});

test('mock chain consumes separate explicit approvals and resumes from server after refresh', async () => {
  const f = fixture();
  await f.work.open('fixture-order');
  await f.work.act('continue');
  const dropshipApproval = f.work.data.dropship.approval;
  await f.work.act('approve', { approvalId: dropshipApproval.id });
  const decision = f.posts().find((x) => x.path.endsWith(`/approvals/${dropshipApproval.id}/approve`));
  assert.equal(decision.body.expected_hash, dropshipApproval.payload_hash);
  assert.equal(orderPhase(f.work.data), 'approve_tracking');
  assert.equal(f.posts().filter((x) => x.path.endsWith('/tracking/push')).length, 0);
  await f.work.act('approve', { approvalId: f.work.data.tracking.approval.id });
  assert.equal(orderPhase(f.work.data), 'complete');
  const resumed = new OrderWorkbench(f.request);
  resumed.role = 'owner';
  await resumed.open('fixture-order');
  const before = f.posts().length;
  await resumed.act('continue');
  assert.equal(orderPhase(resumed.data), 'complete');
  assert.equal(f.posts().length, before);
  assert.equal(f.posts().filter((x) => x.path.endsWith('/dropship/execute')).length, 1);
  assert.equal(f.posts().filter((x) => x.path.endsWith('/tracking/push')).length, 1);
});

test('manual PO and tracking input stay separate; no invented carrier or tracking number', async () => {
  const f = fixture({ manual: true });
  await f.work.open('manual-order');
  await f.work.act('continue');
  await f.work.act('approve', { approvalId: f.work.data.dropship.approval.id });
  assert.equal(orderPhase(f.work.data), 'manual_po');
  await f.work.act('sheet');
  assert.equal(f.work.sheet, 'fixture instructions');
  await f.work.act('record_po', { alibaba_order_id: '246813579012345678' });
  assert.equal(orderPhase(f.work.data), 'tracking_details');
  assert.equal(f.posts().filter((x) => x.path.includes('/tracking/')).length, 0);
  await f.work.act('tracking', { carrier: 'JNT', tracking_no: 'JT246813579013' });
  assert.equal(orderPhase(f.work.data), 'approve_tracking');
  await f.work.act('approve', { approvalId: f.work.data.tracking.approval.id });
  assert.equal(f.work.data.tracking.shipment.tracking_no, 'JT246813579013');
});

test('changed approval requires a new displayed review, never approves unseen revision', async () => {
  const f = fixture();
  await f.work.open('fixture-order');
  await f.work.act('continue');
  const old = f.work.data.dropship.approval.id;
  f.row('fixture-order').dropship.approval.id = 'replacement';
  await f.work.act('approve', { approvalId: old });
  assert.match(f.work.error, /已变化/);
  assert.equal(f.posts().filter((x) => x.path.includes('/approvals/')).length, 0);
});

test('viewer cannot write and operator cannot approve', async () => {
  const f = fixture();
  await f.work.open('fixture-order');
  f.work.role = 'viewer';
  await f.work.act('continue');
  assert.equal(f.posts().length, 0);
  f.work.role = 'owner';
  await f.work.act('continue');
  f.work.role = 'operator';
  await f.work.act('approve', { approvalId: f.work.data.dropship.approval.id });
  assert.equal(f.posts().length, 1);
  assert.match(f.work.html(), /等待店主审批/);
});

test('a failed read is not interpreted as a missing draft', async () => {
  let writes = 0;
  const work = new OrderWorkbench(async (_path, options = {}) => {
    if (options.method === 'POST') writes++;
    throw Object.assign(new Error('forbidden'), { status: 403 });
  });
  work.role = 'owner';
  await work.open('fixture-order');
  await work.act('continue');
  assert.equal(writes, 0);
  assert.equal(work.data, null);
  assert.equal(work.error, 'forbidden');
});

test('failed platform push stays visible; refresh does not retry it', async () => {
  const f = fixture();
  const work = new OrderWorkbench(async (path, options) => {
    if (path.split('?')[0].endsWith('/tracking/push')) throw Object.assign(new Error('TTS_WRITE_BLOCKED'), { status: 409 });
    return f.request(path, options);
  });
  work.role = 'owner';
  await work.open('fixture-order');
  await work.act('continue');
  await work.act('approve', { approvalId: work.data.dropship.approval.id });
  await work.act('approve', { approvalId: work.data.tracking.approval.id });
  assert.equal(work.error, 'TTS_WRITE_BLOCKED');
  assert.equal(orderPhase(work.data), 'push_tracking');
  const before = f.posts().length;
  await work.act('refresh');
  assert.equal(f.posts().length, before);
});

test('switching selected order during execution stops automatic follow-up writes', async () => {
  const f = fixture();
  let release;
  let entered;
  const started = new Promise((resolve) => { entered = resolve; });
  const blocked = new Promise((resolve) => { release = resolve; });
  const work = new OrderWorkbench(async (path, options) => {
    if (path.split('?')[0].endsWith('/dropship/execute')) { entered(); await blocked; }
    return f.request(path, options);
  });
  work.role = 'owner';
  await work.open('old-order');
  await work.act('continue');
  f.row('old-order').dropship.approval.status = 'approved';
  const running = work.act('continue');
  await started;
  await work.open('new-order');
  release();
  await running;
  assert.equal(work.orderId, 'new-order');
  assert.equal(work.data.dropship, null);
  assert.equal(f.posts().filter((x) => x.path.includes('/old-order/tracking/')).length, 0);
});

test('double-click cannot start duplicate writes', async () => {
  const f = fixture();
  await f.work.open('fixture-order');
  await Promise.all([f.work.act('continue'), f.work.act('continue')]);
  assert.equal(f.posts().length, 1);
});

test('untrusted order text and manual sheet are escaped and cleared', async () => {
  const f = fixture({ manual: true });
  await f.work.open('<img src=x onerror=alert(1)>');
  f.work.sheet = '</textarea><script>alert(1)</script>';
  assert.doesNotMatch(f.work.html(), /<script>|<img/);
  f.work.clear();
  assert.equal(f.work.sheet, '');
  assert.equal(f.work.data, null);
});


test('server-stored failure reason survives refresh and new open', async () => {
  const f = fixture();
  const row = f.row('fixture-order');
  row.dropship = {
    draft: { status: 'executed', payload: { carrier: 'JNT', tracking_no: 'JT246813579012' }, result: { carrier: 'JNT', tracking_no: 'JT246813579012' } },
    approval: { status: 'executed', payload: {} },
  };
  row.tracking = {
    draft: {
      status: 'execute_failed',
      payload: { carrier: 'JNT', tracking_no: 'JT246813579012' },
      result: { code: 'TTS_WRITE_BLOCKED', message: 'synthetic write blocked' },
    },
    approval: { status: 'execute_failed', payload: {}, execution_error: 'TTS_WRITE_BLOCKED synthetic write blocked' },
    shipment: null,
  };
  const fresh = new OrderWorkbench(f.request);
  fresh.role = 'owner';
  await fresh.open('fixture-order');
  assert.match(fresh.html(), /TTS_WRITE_BLOCKED/);
  await fresh.act('refresh');
  assert.match(fresh.html(), /TTS_WRITE_BLOCKED/);
  assert.match(fresh.html(), /synthetic write blocked/);
});


test('esc escapes both quote characters in attribute contexts', () => {
  const work = new OrderWorkbench(async () => ({}));
  work.orderId = "x' onmouseover='alert(1)";
  work.role = 'owner';
  work.host = { isConnected: true, innerHTML: '', querySelectorAll: () => [], querySelector: () => null };
  const html = work.html();
  assert.doesNotMatch(html, /onmouseover='alert/);
  assert.match(html, /&#39;/);
});
