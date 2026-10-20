# ai-shopkeeper 数据库表设计（订单系统为核心）

> 版本：**v1.0（已冻结）** ｜ 冻结日期：2026-10-10 ｜ 上游：[DESIGN.md](./DESIGN.md) §4 ｜ 评审：R1~R7 七轮 + R8 冻结终审 PASS（链接见下）
>
> 原则：**平台是钱的真相源**；一切金额快照化、一切外部写幂等化（唯一执行账本）、一切状态变更留痕、台账只追加、PII 按数据类型设守卫。

## 0. 修订记录

### v0.2 → v0.3（回应 R2）

| 条目 | R2 结论 | v0.3 处置 |
|------|---------|-----------|
| P0-1 状态机 | PARTIAL（4 死角） | **已修**：矩阵补 `paid` 列（PAID 可达）、`REFUNDED` 入边写死在矩阵内、rank 规则收窄为"仅约束同向迟到事件"并显式豁免回退边、`PARTIALLY_SHIPPED→DELIVERED` 与 `CANCELLED--refund_completed-->自身` 补齐、ILLEGAL 处置改为"丢弃+告警不阻断"、回退目标改为"从 history 重算最高 rank"（`state_before_refund` 降级为缓存）、补 `return_received` 事件 |
| P0-2 部分退款 | PARTIAL | **已修**：守恒式改可执行三条（C1-C3，见 §4.6）、Σ校验落点=Service+对账线5、`order_refund_items.return_shipment_id` 关联逆向收货、REFUNDED 判据见 §2.1 聚合映射 |
| P0-3 台账 | RESOLVED | 保持；吸收 P2-5：ledger 补 `shop_id`、`settlement_id` |
| P0-4 销售口径 | RESOLVED | 保持；吸收 P2-3：结算基数公式写死（平台补贴不扣减） |
| P0-5 平台侧幂等 | RESOLVED | 保持；吸收 P2-11：operation 举例补 `update_stock/delist` |
| P0-6 审批闸门 | PARTIAL | **已修**：D §4 状态机文本同步（八态）、`entity_id` 与 payload 一致性写进 §4.1 硬规则、pre-flight 阈值落 `gate_policies.threshold` 示例、原子消费 SQL 覆盖执行窗口 + `executing_since` + reaper |
| P0-7 PII | PARTIAL | **已修**：§4.4 重写为**按数据类型设守卫**（所有 JSONB 落库列统一入口），`platform_events.payload` 最小字段集、`external_writes.payload` 收件人只存 `pii_ref`、AAD 绑 `order_id||'|'||field_name`、blob 单独权限、评审清单加"新 JSONB 列必须登记脱敏策略" |
| P0-8 DDL 硬错误 | PARTIAL | **已修**：`tasks`/`tool_call_blobs` 提前消除前向 FK、`platform_fees` 加 PG EXCLUDE 排他约束（SQLite 用写入前校验兜底，§4.5）、缺 FK 的 `shop_id` 全部补 REFERENCES；**DDL 空库建库证据**：见 §0.1 |
| P0-9 批次审批 | RESOLVED | 保持；吸收 P2-4：D §6 标注为原则 1 的有条件例外 |
| P0-10 事件 | PARTIAL | **已修**：rank 收窄（同 P0-1）、dedup_key 时间源统一为 `platform_update_time`、跨 source 重复事件按设计依赖 rank 回填去重（明示） |
| P0-11 库存 | RESOLVED | 保持；吸收 P2-8③：`listing_items.stock` 删除，平台库存权威=`stock_sync_state` |
| P0-12 对账 | RESOLVED | 保持（ledger 补列后逐单调平有字段） |
| R2 新 [P0]-1 状态机 | — | 并入 P0-1 修复 |
| R2 新 [P0]-2 前向 FK | — | **已修**：重排建表顺序，DDL 实测见 §0.1 |
| R2 新 [P0]-3 PII 新旁路 | — | 并入 P0-7 修复 |
| R2 [P1]-1 订单真空期 | — | **已修**：M2 采纳选项①（只读订单同步 + bootstrap accrual + SLA 提醒，不自动履约），bootstrap 台账补记规则见 §4.7 |
| R2 [P1]-2 双执行账本 | — | **已修**：`external_writes` 为**唯一**外部写执行账本；PO 状态机收敛为业务态投影（§2.2），`SUBMITTING/UNKNOWN/attempt` 全部移入 external_writes；approvals 加 `executing_since` + reaper；三者状态映射表见 §4.2 |
| R2 [P1]-3 客服域丢失 | — | **已修**：`conversations`/`messages` 回归（§3.4），自动回复白名单落 `gate_policies(action_type='auto_reply')`；流程纪律：版本迭代必须产出表清单 diff |
| R2 P2 清单 | — | 已吸收：D§4 同步、守恒式、结算基数、platform_fees EXCLUDE、restock/dispute 关联、history 部分唯一索引、approvals 去掉无意义 UNIQUE、external_writes 补 created_at、索引补齐、gate_policies 唯一约束、双库 with_variant 声明、updated_at 应用层维护、internal_number 规则、payout_accounts、KPI 口径定义、D§5/§6/§9 补端点与动作行、M1 Done 补 1688 反查验证 |

### 0.2 v0.3 → v0.4（回应 R3，三态标注）

| R3 条目 | 状态 | 处置 |
|---------|------|------|
| [P0]-1 FULFILLING 无入边（履约链不可达） | **已修** | 事件全集增 `po_submitted`（`aftersale_open` 并入 `refund_requested` 后删除）；矩阵 `PAID --po_submitted--> FULFILLING`；触发点=任一关联 PO 进入 SUBMITTED（§2.2 映射），PO 投影驱动单一路径，不留双轨 |
| [P0]-2 部分退款 qty 盲区 | **已修** | 行级映射改数量式（refund_completed → qty_refunded += Σ refund_items.qty，≥qty 才 refunded，否则 partially_refunded；v0.5 已改绝对重算）；聚合加双侧护栏（rank ≤ REFUNDED 才动作）；恢复"部分退款不迁移主状态"守卫；新增 C6 |
| [P0]-3 rank 豁免与矩阵不互认 | **已修** | 豁免清单按矩阵列名重写为四类；`aftersale_open` 事件删除（并入 refund_requested + 售后窗条件）；`orders.aftersale_deadline` 落列 |
| R3 四条最小放行 ④（CI gate 升级） | **已修** | D §9 M1 gate 改为三断言：格子行为覆盖 + 非初始态有入边 + 非终态有出边 + 全状态从 CREATED 可达（bootstrap 白名单单列）；dedup_key 注释与列名对齐 |
| [P1]-1 PO 后段驱动/记账/留痕 | **已修** | 新增 §2.3「1688 状态回流与成本记账」+ `po_status_history` 表；成本两段（SUBMITTED 时 accrual，实付差额 correction）；entry_key 补冲正形态 |
| P2 高价值批 | **部分修** | 已修：P2-1（映射表函数化+取消竞态守卫）、P2-2（NULL 失配 CHECK+reaper 兜底）、P2-3（external_writes 补 shop_id）、P2-5（行级写入表+restock 权威）、P2-6（C7/C8 折扣与佣金回冲，§4.6）、P2-7（entry_key 冲正形态 + recon_diffs 补 shop_id）、P2-8（settlement_lines 加 line_no）、P2-14（total_cost 语义）、P2-15①③（版本戳/单复数）；未修（M1 并行）：P2-4（乐观锁三类表述）、P2-9（PII 枚举兜底句）、P2-10（角色权限矩阵）、P2-11（KPI gmv/profit 口径）、P2-12（ddl-verify CI 化）、P2-13（平台状态映射表）、P2-16（M2 排期重估） |

### 0.3 v0.4 → v0.5（回应 R4：最小放行两条 + O 批次卫生）

| R4 条目 | 状态 | 处置 |
|---------|------|------|
| 最小放行 1：脚注⁷ 反向句（新 [P0]） | **已修** | 方案甲落地：删除 FULFILLING→PAID；PO 全取消一律 `po_cancelled` 事件走 CANCELLED¹ 链；写死 SUBMITTED 后取消先冲正 goods_cost accrual |
| 最小放行 2：O1/O2 正文化 | **已修** | 聚合行 1 加"至少一行 refunded"、行 1 优先、"先事件后聚合"写死；`qty_refunded` 改绝对重算（禁 `+=`）+ 写时校验阻断 |
| O3 行级 refunding 补写入行 | **已修** | 新增 `refund.state=approved → refunding` 独立行 + rejected 回收规则 |
| O4 文档戳漂移 | **已修** | 43 表口径统一（D §4/§9、S §0.1 注记）；§0.2 P2 编号更正；补 §7 表清单 diff；DESIGN 单数措辞改复数 |
| O5 证据完整性 | **部分修** | 关键查询（orders⋈ledger 聚合）+ psql 版本头/退出码已入日志，§8 补 v0.5 行（后经 R5 判定证据不足，v0.6 重造日志，见 §8）；`scripts/ddl-verify.sh` CI 化仍留 M1 代码阶段 |

### 0.4 v0.5 → v0.6（回应 R5）

| R5 条目 | 状态 | 处置 |
|---------|------|------|
| 存量 [P0]：部分发货+余量取消行永远退不完 | **已修** | 修法甲：聚合行 1 谓词改"每行已结清"（含 partially_refunded 结清分支）+ "至少一行 qty_refunded>0"；行级 `buyer_cancel/po_cancelled` 改绝对重算 + 部分取消不改变行状态；路径级单测样例进 §2.1 与 M1 gate |
| O1 关键查询证据 | **已修** | 日志重造：裸重定向，顺序=版本头→DDL→关键查询文本与结果→退出码；查询改 `SELECT count(*)` 两行恒有输出 |
| O2 `+=` 历史文本冲突 | **已修** | §0.2 历史记录加注 "（v0.5 已改绝对重算）" |
| O3 冲正未入对账规则 | **已修** | §4.7 对账线 po_vs_1688 补 "SUBMITTED 后取消的 PO 须冲正 goods_cost accrual，否则常驻差异单" |
| O4 戳记残留 | **已修** | §0.1 注 "v0.3 时点"；DESIGN 头部与 §4 统一 v0.6/v0.7（时值 v0.5+，后经 R7/R8 推进至冻结）；SCHEMA 评审记录补 R4/R5 |
| O5 "阻断"与事实入库的张力 | **已修** | 行级 refund_completed 写入规则区分两类：本地账错→阻断；平台事实超发→入库+critical+人工处置 |


### 0.5 v0.6 → v0.7（回应 R6）

| R6 条目 | 状态 | 处置 |
|---------|------|------|
| 拍板项：售后部分退款了结无终态出口（存量同族 P0） | **已修（选项①）** | 补 aftersale_resolved 事件：REFUND_REQUESTED --aftersale_resolved--> 回退⁵；rank 豁免清单增第五类；脚注¹⁰ 定义 + 适配器 M2 契约项；路径级单测样例 2 进 gate |
| 文档同步 1：DESIGN §9 M1 gate 补路径级断言 | **已修** | D Done 条件已加路径级断言（SCHEMA §2.1 两条样例路径必须能到 CLOSED） |
| 文档同步 2：DESIGN 版本戳 | **已修** | D 头部/§4 统一 v0.6/v0.7 |
| 文档同步 3：O2/O4 声明与文本对齐 | **已修** | §0.2 历史记录补注；§0.3 O5 行补注 R5 判定；§7 补 diff 行 |
| R6 观察：迟到事件覆盖已结清行 | 已记 | M1 单测项：已结清行的迟到事件只回填、不改 state |
| R6 观察：join 级关键查询证据 | 已记 | M1 CI scripts/ddl-verify.sh 中写死 join 形态 |
### 0.1 DDL 实测证据（R2 [P0]-2 要求）

- **✅ 已执行通过**：2026-10-10，本机 Docker `postgres:15`（daocloud 镜像）空库执行全量 DDL（5 个 sql 块，42 张表（v0.3 时点） + 索引 + btree_gist EXCLUDE 约束），`psql -v ON_ERROR_STOP=1` 退出码 0，零 ERROR。
- 产物：[ddl-verify/ddl.sql](./ddl-verify/ddl.sql)（由本文档 sql 代码块自动抽取）、[ddl-verify/pg15-ddl-verify.log](./ddl-verify/pg15-ddl-verify.log)（完整执行日志）。
- 实测修正记录：本轮共抓出并修复 2 处文档问题——① `disputes.evidence_ref` 前向 FK（`tool_call_blobs` 移入 §3.0）；② `approvals.execution_ref` 环引用 FK（改为应用层保证的普通列）。"文档声称能建"与"日志证明能建"的差距，就在这里。

---

## 1. ER 总览

```
users / shops ─┬─< shop_credentials / payout_accounts
               ├─< listings ──< listing_items ──┐
               │        ├─< listing_versions    │
               │        ├─< inventory_snapshots │
               │        └── stock_sync_state    │
               ├─< orders ─┬─< order_items ─────┘
               │           ├─ order_addresses (1:1 加密)
               │           ├─< order_status_history
               │           ├─< shipments(direction) ─< shipment_items >─ order_items
               │           │        └─< shipment_events
               │           ├─< disputes
               │           ├─< order_refunds(dispute_id) ──< order_refund_items(return_shipment_id)
               │           ├─< purchase_orders(业务态) ──< purchase_order_items
               │           │        └──> suppliers ──< supplier_offers >── products
               │           ├─< platform_settlements ──< settlement_lines
               │           └─< ledger_entries(shop_id, settlement_id, 只追加)
               │
gate_policies ─< approvals(原子消费+执行回执) ──> external_writes(唯一执行账本)
tasks ──< tool_calls ──> tool_call_blobs
conversations ──< messages
penalties / recon_diffs / fx_rates / kpi_daily / platform_fees
platform_events（收件箱，payload=最小字段集）
```

---

## 2. 状态机

### 2.1 订单主状态机（矩阵即法律）

**状态与 rank**：`CREATED(10) PAID(20) FULFILLING(30) PARTIALLY_SHIPPED(35) SHIPPED(40) DELIVERED(50) COMPLETED(60) REFUND_REQUESTED(65伴随) REFUNDED(70) CANCELLED(80) CLOSED(90)`

**事件全集**：`paid / buyer_cancel / po_cancelled / po_submitted / partial_ship / all_shipped / delivered / return_received / refund_requested / refund_rejected / refund_completed / dispute_open / aftersale_resolved / platform_close`（R6 拍板：补 `aftersale_resolved`，处置"售后以部分退款了结"的无终态路径）

**状态 × 事件矩阵**（`—`=ILLEGAL；ILLEGAL 处置=**丢弃 + 写 `recon_diffs(severity='warn')` 告警，不抛错、不阻断事件流**）：

| 状态 \ 事件 | paid | buyer_cancel | po_cancelled | po_submitted | partial_ship | all_shipped | delivered | return_received | refund_requested | refund_rejected | refund_completed | dispute_open | aftersale_resolved | platform_close |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| CREATED | **→PAID** | →CANCELLED | — | — | — | — | — | — | — | — | — | — | — | →CANCELLED |
| PAID | — | →CANCELLED¹ | →CANCELLED¹ | **→FULFILLING⁷** | — | — | — | — | →REFUND_REQUESTED² | — | — | — | — | →CANCELLED |
| FULFILLING | — | →CANCELLED¹ | →CANCELLED¹ | →自身 | →PARTIALLY_SHIPPED | →SHIPPED | — | — | →REFUND_REQUESTED² | — | — | — | — | — |
| PARTIALLY_SHIPPED | — | — | →自身(PO回退) | →自身 | →自身 | →SHIPPED | **→DELIVERED³** | →自身(行级) | →REFUND_REQUESTED² | — | →自身(行级+台账) | — | — | — |
| SHIPPED | — | — | — | — | — | →自身 | →DELIVERED | →自身(行级) | →REFUND_REQUESTED² | — | →自身(行级+台账) | — | — | — |
| DELIVERED | — | — | — | — | — | — | →自身 | →自身(行级) | →REFUND_REQUESTED² | — | →自身(行级+台账) | — | — | →COMPLETED |
| COMPLETED | — | — | — | — | — | — | — | →自身(行级) | →REFUND_REQUESTED²(售后窗内⁸) | — | →自身(行级+台账) | →REFUND_REQUESTED²(争议) | — | →CLOSED |
| REFUND_REQUESTED⁴ | — | — | — | — | — | →自身(仅回填) | →自身(仅回填) | →自身(行级) | →自身 | **→回退⁵** | **→自身(行级推进)；全部行退完→REFUNDED⁹** | — | **→回退⁵(部分退款了结)¹⁰** | — |
| REFUNDED | — | — | — | — | — | — | — | — | — | — | →自身(回填) | — | — | →CLOSED |
| CANCELLED | — | →自身 | →自身 | — | — | — | — | — | — | — | **→自身(仅回填 refunded_at+台账)⁶** | — | — | →CLOSED |
| CLOSED | — | — | — | — | — | — | — | — | →REFUND_REQUESTED²(售后窗内⁸) | — | →自身(行级+台账) | →REFUND_REQUESTED²(争议重开) | — | →自身 |

¹ **取消的钱货处理**：PAID/FULFILLING 取消必须先裁决在途 PO（外部写对账；未发出的直接作废），台账记 `order_cancel_loss`；只有台账闭合（冲正/损失分录完成）才允许 → CLOSED。
² **退款伴随态**：进入前由 OrderService 把"当前状态 + 当时时间"写入 `state_before_refund / state_before_refund_at`（仅作缓存）。**回退与恢复的权威来自 history，不用这个缓存**（见 ⁵）。
³ 整单记签收的站点会发整单 delivered：主状态 →DELIVERED，未发货行由行级状态承载（`cancel_requested`）并触发作废/补偿流程。
⁴ 伴随态期间 `all_shipped/delivered` 事件**只回填时间戳字段、不迁移主状态**；这些事实由 history 保留，供回退时重算。
⁵ **回退（refund_rejected）目标 = 该订单在进入退款态之前达到过的最高 rank 状态，从 `order_status_history` 重算**（`state_before_refund` 列仅是缓存与展示）。回退迁移是合法迁移，**不受 rank 单调约束**（见 rank 规则豁免）。history 必带 reason + source。
⁶ 已支付后取消的订单，平台必然回传退款完成：只回填 `refunded_at` 与台账，主状态保持 CANCELLED。
⁷ **po_submitted 触发点（R3 [P0]-1 修法②，PO 投影驱动单一路径，不留双轨）**：任一关联 PO 进入 SUBMITTED（= 对应审批 executed 且 external_writes CONFIRMED，见 §2.2 映射）→ 若主状态=PAID 则 →FULFILLING。**不存在 FULFILLING→PAID 迁移**：PO 全部取消（含供应商侧）一律发 `po_cancelled` 事件，按矩阵 `FULFILLING→CANCELLED¹` 走取消/退款链（R4 新 [P0] 方案甲落地）。**台账**：PO 进入 SUBMITTED 时已记 `goods_cost` accrual（§2.3），因此任何 SUBMITTED 后的取消必须先冲正该 accrual（`reversal_kind='error'`，`reversal_of=`原分录），损失/余款按 `order_cancel_loss` 闭合后才允许 →CLOSED。
⁸ **售后窗**：`orders.aftersale_deadline`（默认平台规则 T+15，`gate_policies(action_type='aftersale')` 可配）；窗内 `refund_requested` 合法，窗外该事件 ILLEGAL（丢弃+告警）。
⁹ **REFUNDED 判据（修 R3 [P0]-2）**：`refund_completed` 在伴随态只推进行级数量计数（见行级写入表）；主状态 →REFUNDED **仅**由 `recompute_from_lines()` 按聚合表第一行判定（含 rank 护栏），事件本身不直接迁移主状态。

**rank 单调规则（v0.4：豁免清单按矩阵列名写死，修 R3 [P0]-3；v0.7 增第五类）**：rank **只用于一件事**——判定同向事件的"迟到/重复"，迟到者只回填字段、不重复迁移、history 记 `late_event=TRUE`。**矩阵合法性是迁移是否发生的唯一裁判**。以下五类降 rank 迁移显式豁免 rank 谓词（列名与矩阵一一对应）：① `refund_rejected`（回退，目标从 history 重算，见脚注⁵）；② `CLOSED --refund_requested--> REFUND_REQUESTED`（售后窗重开，见脚注⁸）；③ `CLOSED|COMPLETED --dispute_open--> REFUND_REQUESTED`（争议重开）；④ `PARTIALLY_SHIPPED --po_cancelled--> 自身`；⑤ `REFUND_REQUESTED --aftersale_resolved--> 回退⁵`（售后了结恢复前置态，见脚注¹⁰）。**豁免清单之外不存在任何降 rank 的合法迁移。**

**主状态推进的单一权威（修 R2 [P0]-1(b)；v0.4 加护栏，修 R3 [P0]-2）**：主状态只有 `OrderService` 一个写入口，输入两类：
- **事件驱动**：上表矩阵；
- **行聚合**：每次 `order_items` 行变更后执行 `recompute_from_lines()`，映射规则（幂等、可重放）：

| 行级状态聚合结果 | 主状态动作（带护栏） |
|---|---|
| 所有行**已结清**：每行 `state ∈ {refunded, cancelled}`，或 `state ∈ {partially_refunded, refunded}` 且 `qty_refunded = qty_shipped` 且 `qty_shipped + qty_cancelled = qty`（部分发货+余量取消+已发部分退款可终结，R5 存量 [P0] 修法甲）；**且至少一行 `qty_refunded > 0`**（存在 completed 退款），**且当前主状态 rank ≤ REFUNDED(70)** | → REFUNDED（从 CLOSED(90) 不得降级；REFUNDED 判据唯一来源=本行） |
| 所有行 `state='cancelled'`（整行取消，无部分发货残留）且订单已支付，**且当前主状态 rank ≤ CANCELLED(80)** | → CANCELLED（台账闭合后 →CLOSED） |
| 其他 | 维持事件驱动结果，不动作 |

**行 1 优先于行 2**；两类来源在同一入口内串行裁决，顺序固定为**先事件后聚合**（事件迁移完成后才执行 `recompute_from_lines()`），**禁止第二条写路径**。

**守卫（恢复 v0.2 脚注²原文，R3 点名）**：**部分退款不迁移主状态、不提前关单**——部分退款只落行级数量计数与台账，主状态是否 REFUNDED 由聚合护栏唯一裁决。

¹⁰ **aftersale_resolved（R6 拍板，处置"售后以部分退款了结"的终态出口）**：平台售后流程关闭（含部分退款+买家保留余量了结、平台介入裁决关闭）时触发；主状态**回退⁵**恢复前置态（如 DELIVERED），行级数量计数保留（部分退款事实不丢），随后正常走 `platform_close → COMPLETED → CLOSED`。该边为降 rank 边，豁免清单第⑤类。适配器必须在 TikTok 售后单关闭事件上触发本事件（M2 契约项）。

**路径级单测样例（进 M1 CI gate）**：
- `partial_ship(2/3) + 余量取消(1) + refund_completed(已发 2 件) → 聚合行 1（每行已结清）→ REFUNDED；platform_close → CLOSED`（R5 存量 [P0]）
- `delivered + refund_requested + refund_completed(部分退 1/3) + aftersale_resolved → 回退 DELIVERED；platform_close → COMPLETED → CLOSED`（R6 存量同族 [P0]）

**行级状态 × 事件写入表（修 R3 [P2]-5：11 个枚举值全部有写入口）**：

| 行级事件 | 写入规则（OrderService 唯一入口，数量式） |
|---|---|
| `po_submitted` | 行 ∈ {normal,cancel_requested} → `shipped`? 否：保持 normal（未发货不标 shipped） |
| `partial_ship`（shipment_items 落库） | `qty_shipped += 本包qty`；`qty_shipped < qty` → `partially_shipped`，= qty → `shipped` |
| `all_shipped` | 未发货行不存在时全部 `shipped` |
| `delivered` | `qty_shipped > 0` 的行 → `delivered` |
| `return_received` | `qty_refunded ≥ qty` 的行 → `returned`；否则 `partially_refunded` 回退 |
| `refund_requested`（refund.state=requested） | 行 → `refund_requested`（冻结该行后续发货） |
| `refund_rejected` | `refund_requested` 行 → 回 `normal`/`delivered`（按 qty_shipped 判） |
| `refund.state='approved'`（O3 补） | 行 → `refunding`；approved→rejected 回收：`qty_shipped > 0` → `delivered`，否则 → `normal` |
| `refund_completed`（数量式，R3 [P0]-2 核心；O2 绝对重算） | `qty_refunded` **重算为绝对值** `= Σ order_refund_items.qty WHERE refund.state='completed'`（**禁用增量 `+=`**，防跨 source 重复事件双计）；`qty_refunded >= qty` → `refunded`，`0 < qty_refunded < qty` → `partially_refunded`。**写入分两类（R5 O5）**：`qty_refunded ≤ qty_shipped` 违例（本地账错）→ **阻断入库** + `recon_diffs(critical)`；平台回传退款量超过已发（平台事实≠本地账）→ **入库** + `recon_diffs(critical)` + 人工处置（以平台为准修正本地账） |
| `buyer_cancel` / `po_cancelled`（部分取消语义写死，R5 存量 [P0]） | `qty_cancelled` **重算为绝对值** = Σ 已确认取消数量；`qty_shipped = 0 且 qty_cancelled = qty` → 行 `cancelled`；`qty_shipped > 0`（余量取消）→ 行保持 `partially_shipped/shipped/delivered`（结清由 `refund_completed` 数量式判定），行状态不因余量取消而变 `cancelled`；`cancel_requested` 仅在取消申请与确认之间使用 |

**退货再售数量权威（R3 P2-5）**：= `Σ order_refund_items.qty WHERE restock=TRUE`；`shipments.restock_qty` 是该包裹的冗余投影，写入时必须等于上述聚合，违例 → `recon_diffs(warn)`。

**内外状态边界**：`PARTIALLY_SHIPPED`、`REFUND_REQUESTED` 为纯内部状态，不回传平台；回传平台仅两类真实动作：SKU 级发货、退款裁决。平台不支持分批发货的站点，PARTIALLY_SHIPPED 期间逐 SKU 发货、包裹状态以平台为准。

**冷启动**：首次同步存量订单允许 `bootstrap` 入口：按平台当前状态设初态，history 记 `source='bootstrap'`，金额字段全量快照；台账按 §4.7 补记。

**通用规则**：每次迁移写 `order_status_history`（source_event_id 或 actor 必带）。

### 2.2 采购单 PO（业务态投影，修 R2 [P1]-2）

**执行细节（SUBMITTING/UNKNOWN/attempt/重提）全部收敛到 `external_writes`，PO 只保留业务态：**

```
DRAFT → PENDING_APPROVAL → SUBMITTED → PAID → SHIPPED → RECEIVED → DONE
                    │               (CONFIRMED 回填 external_po_id)
                    └─ CANCELLED（买家取消/对账裁决作废）
```

**三者状态映射（审批单 ↔ 执行账本 ↔ PO）**：

| approvals | external_writes | purchase_orders |
|---|---|---|
| pending | —（未执行） | PENDING_APPROVAL |
| approved | PENDING（排队执行） | PENDING_APPROVAL |
| executing | SENT | PENDING_APPROVAL |
| executed | CONFIRMED / RECONCILED | **SUBMITTED**（回填 external_po_id/submitted_at） |
| execute_failed | FAILED / UNKNOWN 未决 | PENDING_APPROVAL（可重批）或 CANCELLED |

| PO 的 `external_po_id` 只在 external_writes `CONFIRMED/RECONCILED` 时回填，执行细节查询一律走 external_writes。

**映射表函数化（修 R3 P2-1）**：`execute_failed` 行的判别写死——`external_writes.state ∈ {FAILED, NON_RETRYABLE}` → PO 回 `PENDING_APPROVAL`（可重批）；`= UNKNOWN` → PO 挂起待对账（不重批）；PO 已 `CANCELLED` 时执行器拒绝外呼（取消与外呼竞态守卫）。`stale/rejected/expired` → PO 回 `DRAFT`。`PENDING` 态孤儿（approved 被撤但写入单未执行）由对账器回收置 `FAILED`。

### 2.3 1688 状态回流与成本记账（新增，修 R3 [P1]-1）

**回流（读）**：对账任务轮询 1688 订单状态（按 `external_po_id`）→ 每次变更落 `po_status_history`（PO 状态变更留痕载体，与 order_status_history 同规）→ 投影 `purchase_orders.state`。

> **勘误（2026-10-20）**：本节原写"按 `request_key`/`external_po_id` 轮询"，有误。`request_key` 是我们自己生成的 ulid（§3.4），只存在于 `external_writes` 账本里，**1688 侧从未见过该值，无法据其反查**。1688 侧唯一可查的键是 `external_po_id`。据此 `external_writes.probe_strategy=query_by_request_id` 在平台侧没有落点，A4 裁决已将该策略标 unavailable，过渡期写 `'none'` 并转人工核对。证据见 [spike/alibaba-order-lookup.md](./spike/alibaba-order-lookup.md) §3.1。

**成本记账两段（R1-R3 扩展到采购侧）**：
- **accrual**：PO 进入 SUBMITTED 时，按 `payable_amount` 记 `goods_cost`（含 `allocated_shipping_cost` 拆分到行）。
- **settlement**：进入 PAID 时按 `paid_amount` 冲正 accrual（`reversal_kind='settlement_reestimate'`，R1 精确对冲）+ 记实付；差额若因供应商改价 → `payout_diff` + `recon_diffs` 检查。
- 到此 M4 的"订单级利润"可判定：`Σ ledger` 含 goods_cost 两段闭合。

留痕表 `po_status_history` 的 DDL 见 §3.4（须后于 `purchase_orders` 与 `external_writes` 建表）。

### 2.4 listing 状态机

```
draft → pending_approval → approved → publishing → publish_unknown? → live
                                │            │              └─(对账裁决)→ live / FAILED(回 approved 可重试)
                                └─ rejected / stale
live → delisted；live → pending_approval(改价/改版重审)
```

---

## 3. DDL

> 建表顺序已消除全部前向 FK；**双库（SQLite 开发/PG 生产）由同一份 SQLAlchemy 元数据生成，JSONB/BYTEA/BIGSERIAL/TIMESTAMPTZ/ARRAY 逐列 `with_variant` 声明**（R2 P2-10）。CI gate：两库空库各跑全量 DDL + 关键查询（见 §8 实测记录）。时间一律 UTC 存储（适配器负责站点本地时间→UTC）；`updated_at` 由应用层（SQLAlchemy onupdate）维护，双库行为一致。

### 3.0 基础域

```sql
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
```

### 3.1 供应商与目录域

```sql
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
```

### 3.2 订单域

```sql
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
```

### 3.3 履约与结算域

```sql
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
```

### 3.4 执行、审批、客服、台账域

```sql
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
```

---

## 4. 关键机制

### 4.1 幂等（一切外部写，唯一账本 external_writes）

1. 生成 `request_key`（ulid），**先落库再外呼**（payload 中 PII 只存 `pii_ref`）；
2. 超时/网络错误 → `UNKNOWN`，**禁止自动重发**；
3. 裁决优先级：平台幂等键（M2 逐接口核对）→ `probe_strategy` 反查（1688 按 request_key 反查为 **M1 技术验证项**）→ `probe_strategy='none'` 转人工；
4. **一致性硬规则（R1 P0-6 修法4）**：执行器外呼前必须校验 `external_writes.target_id`、`approvals.entity_id`、payload 内业务 id 三者一致，不一致 → 拒绝执行 + `recon_diffs(critical)`。

### 4.2 执行账本与状态映射

见 §2.2 映射表。reaper 规则：`executing` 超过执行窗口 → `execute_failed`，关联 external_writes 若已 `SENT` → 转 `UNKNOWN` 进对账流程（进程崩溃不产生悬空写）。

### 4.3 台账冲正三条硬规则（不变）

R1 精确对冲（复制原分录取负，禁重算）；R2 汇兑单列（`fx_gain_loss`）；R3 两段确认（accrual ↔ settlement 成对闭合，对账器检查，缺 → recon_diffs）。

### 4.4 PII 守卫（按数据类型，不按表点名，修 R2 [P0]-3）

**统一入口守卫：任何 JSONB/文本列落库前必须过脱敏管道**，管道按数据类型判定：
1. **JSONB 载荷类**（`platform_events.payload`、`external_writes.payload`、`settlement_lines.raw`、`products.raw_snapshot`、`shipment_events.raw`、`tool_calls.request/response`、`disputes.evidence`）：白名单字段透传（entity/status/amount/timestamps/状态码等业务字段），`recipient/phone/address/evidence` 类字段 → 替换为 `pii_ref`（指向 `order_addresses`）或 HMAC 或 `[REDACTED]`；确需全文 → 加密 blob（`tool_call_blobs` 模式：加密 + retain_until + admin 权限）。
2. **审批载荷类**（`approvals.payload`）：决策依据快照 + `address_ref`，明文 PII 禁入。
3. **消息类**（`messages.body_redacted`）：同规则。
4. **密文规范**：`version(1)||key_id(2)||nonce(12)||ciphertext||tag(16)`，AAD=`{row_key}||'|'||{field_name}`；轮换=双密钥读期→重写→撤旧钥。
5. **评审清单项（流程）**：新增任何含 JSONB/长文本列的表，PR 必须登记该列的脱敏策略，否则评审不通过。

### 4.5 并发与双库纪律（P1-12）

乐观锁 CAS 统一（`version` 列，rowcount=0 重读重试 ≤3 次）；`FOR UPDATE` 仅 PG 可选优化，不构成语义。`platform_fees` 重叠区间：PG EXCLUDE 约束 + SQLite 写入前校验，两侧等价。CI：**两库**空库全量 DDL + 关键查询 + 同批数据校验和比对。

### 4.6 金额守恒式（可执行，接对账线5，修 R2 P2-2/P2-3）

- C1：`orders.item_amount = Σ order_items.subtotal`
- C2：`Σ order_items.discount_share = orders.platform_discount_amount + orders.seller_discount_amount`
- C3（行级）：`qty_shipped + qty_cancelled ≤ qty` 且 `qty_refunded ≤ qty_shipped`（`qty_cancelled` 仅计未发货取消）
- C4（口径）：`settlement_base_amount = item_amount − seller_discount_amount + buyer_shipping_fee`（平台补贴单列对账，不扣减基数）
- C5：`Σ order_refund_items.amount = order_refunds.amount`
- C6（数量守恒，R3 [P0]-2）：每行 `qty_refunded ≤ qty_shipped ≤ qty`，且 `qty_cancelled ≤ qty - qty_shipped`；退款行计数与 refund_items 聚合一致：`order_items.qty_refunded = Σ order_refund_items.qty WHERE refund.state='completed'`
- C7（折扣回冲，R3 P2-6）：部分退款时 `order_refund_items.amount` 按 C4 口径拆分（含 `discount_share` 对应比例回冲），`Σ 退款分录` 与平台结算单逐单平
- C8（佣金回冲，R3 P2-6）：`refund_commission_reversal = 原佣金分录 × (refund_items.amount / settlement_base_amount)`，`reversal_of` 指向原佣金分录（比例冲正也属精确对冲——禁的是按新汇率重算，R1）
- 校验落点：OrderService 写入时校验 + 对账任务每日全量复核（违例 → `recon_diffs(critical)`）。

### 4.7 对账（五条线）与 bootstrap 补账

五条线同 v0.2（platform_vs_local / po_vs_1688 / sla / ledger_vs_settlement / amount_conservation），全部落 `recon_diffs`；修正只走 `ledger_correction` 审批 → 冲正+重记。

**po_vs_1688 线补充（R5 O3）**：SUBMITTED 后被取消的 PO，若其 `goods_cost` accrual 未冲正（`reversal_kind='error'`），对账器生成常驻差异单（`state='open'`），直到冲正落地才收敛——避免"取消的采购"永久挂在利润里。

**bootstrap 订单台账补记规则（R2 [P1]-1）**：真空期订单接入时，按平台快照生成 `basis='accrual'`、`ref_type='bootstrap'` 的补记分录（sale/佣金预估），并触发 SLA 提醒；结算单到达后按 R3 闭合。对账器对 bootstrap 订单同样要求 accrual 存在，第 4 条对账线从第一天起可绿。

---

## 5. 已定决策

1. PO 粒度：一平台单 → N PO（按供应商拆）；合并采购 M5+。
2. 台账本位 CNY，`fx_legs` 两跳（站点币→结算币→CNY，各带 source/fee）。
3. 审批 1:1，"批次"=UI 批量生成/批准（D §6 标注为原则 1 的有条件例外：额度上限、抽检率、责任人，落 `gate_policies.require_approval=false` 初始配置）。
4. PII 密钥：开发期本机 keyring + `key_id` 预留轮换；上云迁 KMS。
5. 订单真空期：M2 选项①（只读同步 + bootstrap accrual + SLA 提醒）。

## 6. 未决问题

1. 起步类目（影响选品官配置）。
2. 项目正式名。
3. 预算上限（M1 期间确认）。

## 7. 表清单 diff（版本迭代纪律：每轮必出）

```
v0.1 (10)  → v0.2 (+18)  users, shop_credentials, payout_accounts, tasks, external_writes,
                         gate_policies, gate_decisions, tool_call_blobs,
                         inventory_snapshots, stock_sync_state, order_refund_items, disputes,
                         penalties, platform_settlements, settlement_lines, recon_diffs,
                         fx_rates, platform_fees
                         （注：approval_policies 系 R1 P1-1 提案名，后并入 gate_policies，未独立成表）  （+ kpi_daily 首次落 DDL）
v0.2 → v0.3 (+0)          无新增；重排依赖、删 listing_items.stock
v0.3 → v0.4 (+1)          po_status_history（R3 [P1]-1 PO 状态留痕）
v0.4 → v0.5 (+0)          无新增；矩阵/聚合/行级写入规则修订
v0.5 → v0.6 (+0)          无新增；谓词修法甲 + 证据补强
v0.6 → v0.7 (+0)          无新增；补 aftersale_resolved 事件（拍板）
现值：43 张表（与 §8 实测一致）
```

## 8. DDL 实测记录

| 时间 | 目标库 | 结果 | 产物 |
|------|--------|------|------|
| 2026-10-10（v0.3） | Docker `postgres:15` 空库 `ddlverify`，`ON_ERROR_STOP=1` | PASS（exit 0，42 tables） | 历史（已被 v0.4 覆盖） |
| **2026-10-10（v0.4）** | Docker `postgres:15.19` 空库 `ddlverify`，`ON_ERROR_STOP=1` | **PASS（exit 0，43 tables，含 po_status_history 与 orders.aftersale_deadline）** | [ddl.sql](./ddl-verify/ddl.sql) ｜ [pg15-ddl-verify.log](./ddl-verify/pg15-ddl-verify.log) |
| **2026-10-10（v0.5）** | 同上（重建库） | PASS（43 tables；**日志为拼装产物，措辞过强**——R5 O1 判定：关键查询标签后无输出，不作为证据） | 历史缺陷在案 |
| **2026-10-10（v0.6）** | 同上（重建库） | **PASS（裸重定向重造：版本头 → 54 条 DDL 逐条输出 → 关键查询真实结果 orders=0/ledger_entries=0 两行 → exit 0）** | [ddl.sql](./ddl-verify/ddl.sql) ｜ [pg15-ddl-verify.log](./ddl-verify/pg15-ddl-verify.log) |
| 待办 | SQLite 空库（SQLAlchemy `with_variant` 方言生成） | M1 代码阶段执行，CI 双库跑（脚本化 `scripts/ddl-verify.sh`，v0.6 日志格式为其模板） | — |

注 1：`CREATE EXTENSION btree_gist` 在托管 PG 需 DBA 预开（R3 P2-12③）。
注 2：各轮产物链接同指一份日志文件（每次重造覆盖），**以最新一轮为准**（R8 L2 清零）；v0.7 未改任何 SQL，v0.6 日志对 v1.0 持续有效（R8 时间戳链佐证）。

## 9. 终审承接项登记（R8 冻结终审，一处收口）

| # | 条目 | 去处 |
|---|------|------|
| L3 | `aftersale_resolved` 与 `refund_rejected` 触发分工（驳回=refund_rejected，关闭了结=aftersale_resolved） | **M2 适配器契约** |
| L4 | `aftersale_resolved` 不写行级；残留 refund_requested/refunding 行按 refund_rejected 回收规则处理 | **M1 单测项**（并入"迟到事件只回填"同族） |
| L6 | 币种一致性校验：`order_items.currency`/`listings.currency` 必须 = `shop.currency`（补 C9 或 Service 校验） | **M1** |
| L7 | 罚分进 `kpi_daily` + 阈值告警（`penalty_points` 列） | **M4** |
| L8 | 幂等键冲突用例（approvals `consumed_by`/ledger `entry_key` 双发测试） | **M1 CI 用例** |
| L5 | 评审文件首行 harness 噪声行清理 | **可选卫生** |
| nit | DDL 注释锚位两处（order_items/ledger_entries）+ po_status_history 域归属表述 | **M1 代码阶段**（SQLAlchemy 元数据生成时重锚） |

## 10. 冻结声明

**v1.0 已冻结（2026-10-10）**：经 R1~R7 七轮独立评审 + R8 冻结终审（PASS），本表结构作为 M1 实现的权威基线。后续变更走变更流程：改 DDL 必须重跑 §8 双库建库验证并更新修订记录。M1 Done 条件以 DESIGN §9 为唯一权威。
