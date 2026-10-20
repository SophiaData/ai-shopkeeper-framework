CREATE TABLE users (
  id TEXT PRIMARY KEY, name TEXT NOT NULL,
  feishu_open_id TEXT UNIQUE,
  role TEXT NOT NULL DEFAULT 'owner',     -- owner|operator|finance|viewer
  is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE shops (
  id TEXT PRIMARY KEY, platform TEXT NOT NULL, site TEXT NOT NULL,
  currency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (platform, site)
);

CREATE TABLE shop_credentials (
  shop_id TEXT NOT NULL REFERENCES shops(id),
  kind TEXT NOT NULL,                     -- access_token|refresh_token|cookie
  secret_ref TEXT NOT NULL,
  expires_at TIMESTAMPTZ, last_refreshed_at TIMESTAMPTZ,
  auth_state TEXT NOT NULL DEFAULT 'valid',  -- valid|expiring|invalid
  PRIMARY KEY (shop_id, kind)
);
-- auth_state != 'valid' → 闸门硬拒一切平台出站写（DESIGN §7 规则8）

CREATE TABLE payout_accounts (
  id TEXT PRIMARY KEY, shop_id TEXT NOT NULL REFERENCES shops(id),
  provider TEXT NOT NULL, currency TEXT NOT NULL,
  fee_rate NUMERIC(8,6), entity TEXT
);

CREATE TABLE tasks (                       -- 提前定义（purchase_orders/tool_calls 引用）
  id TEXT PRIMARY KEY,
  shop_id TEXT REFERENCES shops(id),
  agent TEXT NOT NULL, type TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,            -- arq 重投递防护
  input JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued|running|succeeded|failed|cancelled
  output JSONB, error TEXT,
  parent_task TEXT REFERENCES tasks(id),
  started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE tool_call_blobs (             -- 原始响应/凭证分层存储；供全域引用
  id TEXT PRIMARY KEY,
  body_enc BYTEA NOT NULL,                -- 压缩+加密（封装同 §3.2，AAD=blob id）
  content_type TEXT, size_bytes BIGINT,
  retain_until TIMESTAMPTZ NOT NULL,      -- 过期清理
  access_role TEXT NOT NULL DEFAULT 'admin'  -- 单独权限：仅 admin 角色可解密读取（R2 P2-12）
);                                         -- 引用者：disputes.evidence_ref / tool_calls.blob_ref / messages.body_blob

CREATE TABLE platform_events (             -- 收件箱（order_status_history 引用）
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL REFERENCES shops(id),
  entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  source TEXT NOT NULL,                   -- 'webhook'|'poll'
  event_type TEXT NOT NULL,
  dedup_key TEXT NOT NULL,                -- sha256(platform_event_id) 优先；
                                          -- 兜底 sha256(event_type|entity_id|platform_update_time_raw)，
                                          -- 时间取平台原始字符串不截断（禁止时间片取整）；
                                          -- raw 值随事件 payload 原样保留，哈希在入库前计算（R3 P0-10 缺口①）
  payload JSONB NOT NULL,                 -- ★最小字段集：entity/status/amount/timestamps/
                                          --   platform_raw_status；不含收件人姓名/电话/地址（§4.4）
  platform_update_time TIMESTAMPTZ,       -- 水位判断 + dedup 兜底时间源
  status TEXT NOT NULL DEFAULT 'pending', -- pending|processed|failed
  error TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(), processed_at TIMESTAMPTZ,
  UNIQUE (shop_id, dedup_key)
);
CREATE INDEX idx_pe_pending ON platform_events(received_at) WHERE status='pending';
CREATE INDEX idx_pe_entity ON platform_events(entity_type, entity_id);
-- 跨 source（webhook 与 poll 描述同一变更）会生成两个不同 dedup_key：有意为之，
-- 由 §2.1 rank 回填规则保证效果幂等（R2 P0-10 缺口3 明示）


CREATE TABLE suppliers (
  id TEXT PRIMARY KEY, name TEXT NOT NULL,
  warehouse_type TEXT NOT NULL,           -- 履约只允许 'overseas_warehouse'
  promised_ship_days INTEGER,
  blacklist_score INTEGER NOT NULL DEFAULT 0,
  blacklisted BOOLEAN NOT NULL DEFAULT FALSE, notes TEXT
);

CREATE TABLE products (
  id TEXT PRIMARY KEY,
  source_platform TEXT NOT NULL, source_item_id TEXT NOT NULL,
  source_url TEXT NOT NULL, title_raw TEXT NOT NULL,
  supplier_id TEXT REFERENCES suppliers(id),
  reference_cost_price BIGINT,            -- 仅选品比价，禁止用于利润核算（P2-4）
  cost_currency TEXT NOT NULL DEFAULT 'CNY',
  moq INTEGER, images JSONB NOT NULL DEFAULT '[]',
  raw_snapshot JSONB,                     -- ★过 §4.4 JSONB 守卫
  status TEXT NOT NULL DEFAULT 'sourced',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_platform, source_item_id)
);

CREATE TABLE supplier_offers (
  id TEXT PRIMARY KEY,
  product_id TEXT NOT NULL REFERENCES products(id),
  price BIGINT NOT NULL, currency TEXT NOT NULL DEFAULT 'CNY',
  warehouse_type TEXT NOT NULL, ship_days INTEGER,
  is_selected BOOLEAN NOT NULL DEFAULT FALSE, quoted_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE listings (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL REFERENCES shops(id),
  product_id TEXT NOT NULL REFERENCES products(id),
  platform_product_id TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  -- draft|pending_approval|approved|publishing|publish_unknown|live|rejected|stale|delisted
  price BIGINT NOT NULL, currency TEXT NOT NULL,
  compliance_report JSONB,
  current_version INTEGER NOT NULL DEFAULT 0,
  published_version INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (shop_id, platform_product_id)
);

CREATE TABLE listing_versions (
  id TEXT PRIMARY KEY,
  listing_id TEXT NOT NULL REFERENCES listings(id),
  version INTEGER NOT NULL, payload JSONB NOT NULL,
  created_by_task TEXT, approval_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (listing_id, version)
);
-- 发布前置校验：approved 的 approval_id 对应版本 == listings.current_version，否则 approval → stale

CREATE TABLE listing_items (
  id TEXT PRIMARY KEY,
  listing_id TEXT NOT NULL REFERENCES listings(id),
  platform_sku_id TEXT, attrs JSONB NOT NULL,
  price BIGINT NOT NULL
  -- stock 列已删除（R2 P2-8③）：平台库存权威 = stock_sync_state.platform_qty
);

CREATE TABLE inventory_snapshots (
  id TEXT PRIMARY KEY,
  listing_item_id TEXT NOT NULL REFERENCES listing_items(id),
  source TEXT NOT NULL,                   -- 'supplier_1688'|'platform'
  qty INTEGER NOT NULL, captured_at TIMESTAMPTZ NOT NULL,
  UNIQUE (listing_item_id, source, captured_at)
);

CREATE TABLE stock_sync_state (
  listing_item_id TEXT PRIMARY KEY REFERENCES listing_items(id),
  supplier_qty INTEGER, platform_qty INTEGER,
  buffer_ratio NUMERIC(4,3) NOT NULL DEFAULT 0.800,
  last_pushed_at TIMESTAMPTZ, last_pull_at TIMESTAMPTZ,
  circuit_state TEXT NOT NULL DEFAULT 'normal'  -- normal|throttled|cutoff
);
-- 平台库存 = min(供应商库存 × buffer_ratio, 每日上限)；供应商断货/报价失效 → cutoff → 下架审批
-- （DESIGN §7 规则7）


CREATE TABLE orders (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL REFERENCES shops(id),
  platform_order_id TEXT NOT NULL,
  internal_number TEXT NOT NULL UNIQUE,   -- 'SO-{站点}-{yyyymmdd}-{base32(ulid尾)}'
  state TEXT NOT NULL,                    -- §2.1 矩阵
  platform_status_raw TEXT,               -- "最近一次收到的平台原始状态"，逐次留痕于 history
  buyer_note TEXT,
  currency TEXT NOT NULL,
  item_amount BIGINT NOT NULL,            -- 商品总额
  buyer_shipping_fee BIGINT NOT NULL DEFAULT 0,
  platform_discount_amount BIGINT NOT NULL DEFAULT 0,  -- 平台承担（非我方收入）
  seller_discount_amount BIGINT NOT NULL DEFAULT 0,    -- 卖家承担（我方成本）
  tax_withheld_amount BIGINT NOT NULL DEFAULT 0,       -- 平台代扣税
  settlement_base_amount BIGINT,          -- = item_amount - seller_discount + buyer_shipping_fee
                                          --   （平台补贴不扣减，R2 P2-3；sale 分录口径）
  expected_payout_amount BIGINT,
  actual_payout_amount BIGINT,            -- 结算单回填
  ship_deadline TIMESTAMPTZ, deliver_deadline TIMESTAMPTZ,   -- P1-13
  aftersale_deadline TIMESTAMPTZ,          -- 售后窗截止（默认平台规则 T+15，gate_policies 可配，R3 [P0]-3）
  placed_at TIMESTAMPTZ, paid_at TIMESTAMPTZ, shipped_at TIMESTAMPTZ,
  delivered_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
  cancelled_at TIMESTAMPTZ, refunded_at TIMESTAMPTZ,
  state_before_refund TEXT,               -- 缓存；回退权威=history（§2.1 脚注5）
  state_before_refund_at TIMESTAMPTZ,
  version INTEGER NOT NULL DEFAULT 0,     -- 乐观锁（唯一并发控制，§4.5）
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (shop_id, platform_order_id)
);
CREATE INDEX idx_orders_state ON orders(shop_id, state);
CREATE INDEX idx_orders_deadline ON orders(state, ship_deadline);

CREATE TABLE order_items (
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(id),
  line_no INTEGER NOT NULL,
  listing_item_id TEXT REFERENCES listing_items(id),
  sku_snapshot JSONB NOT NULL,
  qty INTEGER NOT NULL,
  qty_shipped INTEGER NOT NULL DEFAULT 0, -- qty_cancelled 仅计"未发货取消"（R2 P2-2）
  qty_refunded INTEGER NOT NULL DEFAULT 0,-- 独立维度：≤ qty_shipped（退款基于已发货）
  qty_cancelled INTEGER NOT NULL DEFAULT 0,
  unit_price BIGINT NOT NULL, currency TEXT NOT NULL, subtotal BIGINT NOT NULL,
  discount_share BIGINT NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'normal',
  -- normal|refund_requested|refunding|partially_refunded|refunded
  -- |cancel_requested|cancelled|shipped|partially_shipped|delivered|returned
  UNIQUE (order_id, line_no)
);

CREATE TABLE order_addresses (
  order_id TEXT PRIMARY KEY REFERENCES orders(id),
  -- 密文封装：BYTEA = version(1)||key_id(2)||nonce(12)||ciphertext||tag(16)
  -- AES-256-GCM；AAD = order_id || '|' || field_name（防同单两列密文互换，R2 P2-12）
  recipient_enc BYTEA NOT NULL,
  address_enc BYTEA NOT NULL,
  phone_hmac BYTEA, name_hmac BYTEA,      -- HMAC-SHA256(pepper@KMS)，按电话/姓名查单
  district_code TEXT,
  accessed_at TIMESTAMPTZ, accessed_by TEXT
);

CREATE TABLE order_status_history (
  id BIGSERIAL PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(id),
  from_state TEXT, to_state TEXT NOT NULL,
  source TEXT NOT NULL,                   -- platform_event|agent|manual|bootstrap
  source_event_id TEXT REFERENCES platform_events(id),
  late_event BOOLEAN NOT NULL DEFAULT FALSE,
  actor TEXT, reason TEXT, platform_status_raw TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_osh_order_event ON order_status_history(order_id, to_state, source_event_id) WHERE source_event_id IS NOT NULL;  -- 部分唯一索引（R2 P2-9）

CREATE TABLE shipments (
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(id),
  direction TEXT NOT NULL DEFAULT 'forward',  -- forward|return（P1-5）
  carrier TEXT, tracking_no TEXT,
  status TEXT NOT NULL DEFAULT 'created',
  -- created|picked_up|in_transit|delivered|exception|returned_to_warehouse
  tracking_validated BOOLEAN DEFAULT FALSE,
  sla_deadline TIMESTAMPTZ, sla_met BOOLEAN,
  shipped_at TIMESTAMPTZ, delivered_at TIMESTAMPTZ,
  restock_qty INTEGER NOT NULL DEFAULT 0,     -- 退货再售数量
  UNIQUE (order_id, direction, carrier, tracking_no)
);
CREATE INDEX idx_shipments_sla ON shipments(sla_deadline, sla_met);   -- R2 P2-9

CREATE TABLE shipment_items (
  id TEXT PRIMARY KEY,
  shipment_id TEXT NOT NULL REFERENCES shipments(id),
  order_item_id TEXT NOT NULL REFERENCES order_items(id),
  qty INTEGER NOT NULL
);
-- 守恒（R1 P1-5）：Σ forward shipment_items.qty ≤ order_items.qty，OrderService 校验

CREATE TABLE shipment_events (
  id BIGSERIAL PRIMARY KEY,
  shipment_id TEXT NOT NULL REFERENCES shipments(id),
  node_desc TEXT NOT NULL, occurred_at TIMESTAMPTZ,
  raw JSONB,                              -- ★过 §4.4 JSONB 守卫
  UNIQUE (shipment_id, node_desc, occurred_at)
);

CREATE TABLE disputes (
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(id),
  platform_dispute_id TEXT NOT NULL, reason_code TEXT,
  stage TEXT NOT NULL,                    -- opened|evidence|platform_review|resolved
  evidence_due_at TIMESTAMPTZ, outcome TEXT, liability TEXT,
  platform_fee BIGINT,
  evidence_ref TEXT REFERENCES tool_call_blobs(id),  -- 凭证走加密 blob（P0-7 次级旁路）
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (order_id, platform_dispute_id)
);

CREATE TABLE order_refunds (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL REFERENCES shops(id),
  order_id TEXT NOT NULL REFERENCES orders(id),
  platform_refund_id TEXT NOT NULL,
  type TEXT NOT NULL,                     -- full|partial|chargeback|claim
  dispute_id TEXT REFERENCES disputes(id),            -- R2 P2-8② 互引
  amount BIGINT NOT NULL, currency TEXT NOT NULL,
  liability TEXT NOT NULL DEFAULT 'unknown',  -- seller|buyer|carrier|supplier|platform
  refund_channel TEXT,                    -- 原路退回|余额|线下
  reason TEXT,
  state TEXT NOT NULL,                    -- requested|approved|rejected|completed
  approved_by TEXT, decided_at TIMESTAMPTZ, settled_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (shop_id, platform_refund_id)
);

CREATE TABLE order_refund_items (
  id TEXT PRIMARY KEY,
  refund_id TEXT NOT NULL REFERENCES order_refunds(id),
  order_item_id TEXT NOT NULL REFERENCES order_items(id),
  qty INTEGER NOT NULL,
  amount BIGINT NOT NULL,
  restock BOOLEAN,
  return_shipment_id TEXT REFERENCES shipments(id),   -- R2 P2-8①：关联逆向收货
  UNIQUE (refund_id, order_item_id)
);

CREATE TABLE penalties (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL REFERENCES shops(id),
  platform_penalty_id TEXT NOT NULL, rule_code TEXT,
  amount BIGINT, points INTEGER, occurred_at TIMESTAMPTZ,
  appeal_deadline TIMESTAMPTZ, appeal_state TEXT,
  settled_in_settlement_id TEXT,
  UNIQUE (shop_id, platform_penalty_id)
);


CREATE TABLE purchase_orders (             -- 业务态投影（执行细节在 external_writes，§2.2）
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL REFERENCES shops(id),
  supplier_id TEXT NOT NULL REFERENCES suppliers(id),
  state TEXT NOT NULL DEFAULT 'DRAFT',    -- DRAFT|PENDING_APPROVAL|SUBMITTED|PAID|SHIPPED|RECEIVED|DONE|CANCELLED
  external_po_id TEXT,                    -- external_writes CONFIRMED 时回填
  total_cost BIGINT,                      -- = 商品口径 Σ(qty×unit_cost)，不含运费（R3 P2-14：与 payable_amount 差异=运费）
  payable_amount BIGINT, paid_amount BIGINT, paid_at TIMESTAMPTZ, payment_ref TEXT,
  allocated_shipping_cost BIGINT NOT NULL DEFAULT 0,  -- PO 级运费按行金额占比分摊
  approval_id TEXT UNIQUE,                -- 1:1（P0-9 方案 A）
  created_by_task TEXT REFERENCES tasks(id),          -- tasks 已在 §3.0（R2 P0-2）
  submitted_at TIMESTAMPTZ,               -- CONFIRMED 时回填
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_po_state ON purchase_orders(state);
-- 守恒：payable_amount = Σ(qty×unit_cost) + allocated_shipping_cost（Service 校验）

CREATE TABLE purchase_order_items (
  id TEXT PRIMARY KEY,
  po_id TEXT NOT NULL REFERENCES purchase_orders(id),
  order_item_id TEXT NOT NULL REFERENCES order_items(id),
  qty INTEGER NOT NULL, unit_cost BIGINT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  allocated_shipping_cost BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE platform_settlements (        -- 结算单真相源
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL REFERENCES shops(id),
  platform_settlement_id TEXT NOT NULL,
  period_start DATE, period_end DATE, settlement_currency TEXT NOT NULL,
  fx_to_site NUMERIC(18,8),
  total_amount BIGINT NOT NULL, asset_detail JSONB NOT NULL,   -- ★JSONB 守卫
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (shop_id, platform_settlement_id)
);

CREATE TABLE settlement_lines (
  id TEXT PRIMARY KEY,
  settlement_id TEXT NOT NULL REFERENCES platform_settlements(id),
  line_no INTEGER NOT NULL,               -- R3 P2-8：平台明细行号，防同单同费同额误判重
  platform_order_id TEXT, platform_line_id TEXT, fee_type TEXT,
  amount BIGINT NOT NULL, currency TEXT NOT NULL,
  raw JSONB,                              -- ★JSONB 守卫
  UNIQUE (settlement_id, platform_line_id),
  UNIQUE (settlement_id, line_no)
);

CREATE TABLE recon_diffs (
  id TEXT PRIMARY KEY,
  shop_id TEXT REFERENCES shops(id),      -- R3 P2-7：差异按店归集追责
  recon_line TEXT NOT NULL,  -- platform_vs_local|po_vs_1688|sla|ledger_vs_settlement|amount_conservation
  ref_type TEXT, ref_id TEXT,
  expected BIGINT, actual BIGINT, diff_amount BIGINT,
  severity TEXT NOT NULL,                 -- critical|warn|info
  state TEXT NOT NULL DEFAULT 'open',     -- open|resolved|ignored
  resolution TEXT, resolved_by TEXT, resolved_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


CREATE TABLE gate_policies (               -- 闸门规则表（DESIGN §6 落库）
  id TEXT PRIMARY KEY,
  action_type TEXT NOT NULL,              -- create_listing|update_price|place_po|auto_reply|
                                          -- ledger_correction|delist|update_stock|...
  threshold JSONB NOT NULL,
  -- 例：update_price {"price_delta_pct":5}；place_po pre-flight
  --   {"cost_delta_pct":2,"fx_delta_pct":1,"require_stock":true}
  require_approval BOOLEAN NOT NULL, channel TEXT NOT NULL,
  version INTEGER NOT NULL, effective_from DATE NOT NULL, effective_to DATE,
  UNIQUE (action_type, version)           -- R2 P2-9
);

CREATE TABLE approvals (                   -- 审批八态（DESIGN §4 同步）
  id TEXT PRIMARY KEY,
  shop_id TEXT REFERENCES shops(id),
  action_type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  payload JSONB NOT NULL,                 -- ★内嵌决策依据快照（offer_id/unit_cost/qty/fx_rate/
                                          --   fx_at/listing_version/quoted_at）；PII 只存 address_ref
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  -- pending|approved|executing|executed|execute_failed|stale|rejected|expired
  CHECK (status <> 'approved' OR decided_at IS NOT NULL),   -- R3 P2-2：防 NULL 静默失配
  channel TEXT NOT NULL,                  -- feishu|console
  requested_by TEXT REFERENCES users(id),
  decided_by TEXT REFERENCES users(id),
  dual_approved_by TEXT REFERENCES users(id),   -- 大额双人复核
  decided_at TIMESTAMPTZ,
  decide_before TIMESTAMPTZ NOT NULL,     -- 审批超时 → expired
  execute_within_seconds INTEGER NOT NULL DEFAULT 900,  -- 执行窗口，超窗 → stale
  executing_since TIMESTAMPTZ,            -- reaper 依据（R2 [P1]-2）
  executed_at TIMESTAMPTZ, execution_ref TEXT,   -- → external_writes.id（环引用，无 FK，应用层保证）
  execution_error TEXT,
  preflight_snapshot JSONB, preflight_result TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 原子消费（唯一合法消费方式；覆盖执行窗口）：
--   UPDATE approvals SET status='executing', executing_since=now()
--   WHERE id=:id AND status='approved'
--     AND decide_before > now()
--     AND decided_at + make_interval(secs => execute_within_seconds) > now()
--   → rowcount=1 才允许 pre-flight + 外呼
-- reaper：status='executing' AND (executing_since IS NULL OR now() > executing_since + execute_within_seconds)
--   → 置 execute_failed；关联 external_writes 转 UNKNOWN 对账流程（R3 P2-2 NULL 兜底）

CREATE TABLE gate_decisions (
  id BIGSERIAL PRIMARY KEY,
  approval_id TEXT REFERENCES approvals(id),
  policy_id TEXT NOT NULL REFERENCES gate_policies(id),
  policy_version INTEGER NOT NULL,
  input_snapshot JSONB NOT NULL, decision TEXT NOT NULL, reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE external_writes (             -- ★唯一外部写执行账本（PO 的 UNKNOWN 语义泛化于此）
  id TEXT PRIMARY KEY,
  shop_id TEXT REFERENCES shops(id),      -- R3 P2-3：按店隔离与运维统计
  adapter TEXT NOT NULL,                  -- 'tiktok_api'|'alibaba'|'feishu'
  operation TEXT NOT NULL,                -- create_product|update_price|update_stock|delist|
                                          --   ship_order|update_tracking|place_po|send_message
  target_type TEXT NOT NULL, target_id TEXT NOT NULL,
  request_key TEXT NOT NULL,
  payload JSONB NOT NULL,                 -- ★收件人等 PII 只存 pii_ref（→order_addresses），
                                          --   外呼时现场解密拼装（R2 [P0]-3）
  payload_hash TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'PENDING',
  -- PENDING|SENT|UNKNOWN|CONFIRMED|FAILED|RECONCILED|NON_RETRYABLE
  sent_at TIMESTAMPTZ, attempt_no INTEGER NOT NULL DEFAULT 1,
  probe_strategy TEXT,                    -- search_by_sku|list_products_since|query_by_request_id|none
  probe_result JSONB, reconciled_at TIMESTAMPTZ,
  approval_id TEXT REFERENCES approvals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),      -- R2 P2-9
  UNIQUE (adapter, operation, request_key, attempt_no)
);
-- 重提 = 新 attempt_no + 新 request_key，旧 request_key 永久 dead（R1 P1-3）
-- probe_strategy='none' 的接口 UNKNOWN 只能转人工（写死）

CREATE TABLE po_status_history (           -- R3 [P1]-1：PO 状态变更留痕（§2.3）
  id BIGSERIAL PRIMARY KEY,
  po_id TEXT NOT NULL REFERENCES purchase_orders(id),
  from_state TEXT, to_state TEXT NOT NULL,
  source TEXT NOT NULL,                   -- poll_1688|agent|manual
  external_write_id TEXT REFERENCES external_writes(id),
  actor TEXT, reason TEXT, raw JSONB,     -- ★JSONB 守卫
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (po_id, to_state, source, created_at)
);

CREATE TABLE tool_calls (
  id BIGSERIAL PRIMARY KEY,
  task_id TEXT REFERENCES tasks(id),
  adapter TEXT NOT NULL, method TEXT NOT NULL,
  request JSONB,                          -- ★写入前过 §4.4 脱敏白名单管道
  response JSONB,                         -- 摘要级；全文 → tool_call_blobs（§3.0）
  blob_ref TEXT REFERENCES tool_call_blobs(id),
  status TEXT NOT NULL, duration_ms INTEGER, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE conversations (               -- R2 [P1]-3 客服域回归
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL REFERENCES shops(id),
  order_id TEXT REFERENCES orders(id),
  channel TEXT NOT NULL, buyer_ref TEXT,
  risk_category TEXT NOT NULL DEFAULT 'low',  -- low|medium|high（high=涉钱必审）
  state TEXT NOT NULL DEFAULT 'open',     -- open|waiting|closed
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
  id BIGSERIAL PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  role TEXT NOT NULL,                     -- buyer|agent|human
  body_redacted TEXT NOT NULL,            -- §4.4 守卫：含 PII 的原文只进加密 blob
  body_blob TEXT REFERENCES tool_call_blobs(id),
  auto_sent BOOLEAN NOT NULL DEFAULT FALSE,
  approval_id TEXT REFERENCES approvals(id),  -- 白名单外回复的审批单
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE fx_rates (
  id BIGSERIAL PRIMARY KEY,
  rate_date DATE NOT NULL, from_ccy TEXT NOT NULL, to_ccy TEXT NOT NULL,
  rate NUMERIC(18,8) NOT NULL, source TEXT NOT NULL,
  UNIQUE (rate_date, from_ccy, to_ccy, source)
);

CREATE TABLE ledger_entries (              -- 只追加！修正=冲正（R1-R3，§4.3）
  id BIGSERIAL PRIMARY KEY,
  shop_id TEXT NOT NULL REFERENCES shops(id),          -- R2 P2-5
  entry_key TEXT NOT NULL UNIQUE,         -- 确定性幂等键：
                                          -- 'sale:order={id}:settlement={sid}' / 'refund:refund_id={id}'
                                          -- 'penalty:penalty_id={id}' / 'fx_gain_loss:{原entry_id}' ...
  order_id TEXT REFERENCES orders(id),
  settlement_id TEXT REFERENCES platform_settlements(id),  -- R2 P2-5 逐单调平
  entry_type TEXT NOT NULL,
  -- sale|platform_commission|affiliate_commission|transaction_fee|tax_withheld
  -- |platform_subsidy|seller_discount|shipping_subsidy|goods_cost|domestic_shipping
  -- |refund|refund_commission_reversal|affiliate_reversal|order_cancel_loss
  -- |chargeback_loss|dispute_fee|platform_penalty|fx_gain_loss|payout_diff|adjustment
  currency TEXT NOT NULL,
  amount BIGINT NOT NULL,                 -- 原币：正=收入，负=支出
  base_currency TEXT NOT NULL DEFAULT 'CNY',
  base_amount BIGINT NOT NULL,
  -- CNY; entry_key 冲正形态（R3 P2-7）：'reversal_of={原entry_id}'（确定性、重放安全）
  fx_legs JSONB,                          -- 两跳汇率链：站点币→结算币→CNY（各带 source/fee）
  fx_rate_id BIGINT REFERENCES fx_rates(id),
  basis TEXT NOT NULL,                    -- accrual|settlement|correction
  reversal_kind TEXT,                     -- 'error'|'settlement_reestimate'
  reversal_of BIGINT REFERENCES ledger_entries(id),
  memo TEXT, ref_type TEXT, ref_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (reversal_of IS NULL OR reversal_of <> id)
);
CREATE INDEX idx_ledger_order ON ledger_entries(order_id);
CREATE INDEX idx_ledger_ref ON ledger_entries(ref_type, ref_id);
-- 生产库触发器禁 UPDATE/DELETE；修正一律冲正+重记；任何代码路径不得 UPDATE

CREATE TABLE platform_fees (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL REFERENCES shops(id), fee_type TEXT NOT NULL,
  rate NUMERIC(8,6) NOT NULL,
  effective_from DATE NOT NULL, effective_to DATE,
  UNIQUE (shop_id, fee_type, effective_from)
);
-- 重叠区间排他（R2 P2-7）：PG 用 EXCLUDE（需 btree_gist 扩展）；SQLite 无此约束，由写入前校验拦截（§4.5）
CREATE EXTENSION IF NOT EXISTS btree_gist;  -- PG 专有；SQLAlchemy 方言分支跳过
ALTER TABLE platform_fees ADD CONSTRAINT uq_pf_no_overlap
  EXCLUDE USING gist (shop_id WITH =, fee_type WITH =, daterange(effective_from, effective_to, '[]') WITH &&);

CREATE TABLE kpi_daily (                   -- 派生表：可 DELETE+INSERT 全量重建
  stat_date DATE NOT NULL,
  shop_id TEXT NOT NULL REFERENCES shops(id),
  gmv BIGINT, orders INTEGER, profit BIGINT,
  ship_on_time_rate NUMERIC(5,4), deliver_on_time_rate NUMERIC(5,4),
  agent_metrics JSONB,
  PRIMARY KEY (stat_date, shop_id)
);
-- KPI 口径（R2 P2-13）：
--   ship_on_time_rate    = 发货时间 ≤ ship_deadline 的订单数 / 应发货订单数
--                          （应发货=paid 且非发货前买家取消；分母剔除买家责任取消）
--   deliver_on_time_rate = delivered_at ≤ deliver_deadline 的包裹数 / 应签收包裹数
--                          （承运方免责异常进 recon_diffs 复核后剔除）
--   agent_metrics = {"scout":{"reports":n}, ...} 各 agent 当日产出
