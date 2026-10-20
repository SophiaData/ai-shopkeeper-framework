# 编排器 vs LangGraph（及可选 CrewAI）对照评估

> 上游权威：[DESIGN.md](./DESIGN.md) §3.1 编排器、§2.1 闸门前置 ｜ 实现：`src/shopkeeper/orchestrator/`
> 生成：2026-10-13 ｜ 关闭：2026-10-16 ｜ 状态：**已拍板——保留自研，不迁移**（HANDOFF_PLAN P2-13 关闭） ｜ 不引入新运行时依赖
> 纪律：只陈述现状差异与取舍建议；不把「业界常见」写成已接通能力。LangGraph / CrewAI 能力描述依据其公开文档与常见用法（推断处已标注），本仓库未做装库实测。

---

## 0. 结论速览

| 问题 | 结论 |
|------|------|
| 现有编排器是否应全量换成 LangGraph？ | **不建议。** 自研已覆盖本店关键路径：声明式 DAG、SQLite 检查点、八态人审挂起、日预算熔断、生成类重试。 |
| 短期（M3–M4 真店闭环） | **保留自研。** 零新依赖，契约与 `approvals` / `TTS_WRITE_*` / mock 先行对齐。 |
| 中期（框架腿、多 Agent 协作复杂化） | **可局部引入概念或适配层**（例如把某条旁路调研图用 LangGraph 原型），主路径仍走 `orch_*` + 八态闸门。 |
| CrewAI | **仅作可选对照。** 角色/任务编排强，检查点与涉钱闸门弱于本仓库需求；不建议作主运行时。 |
| 新依赖 | **默认不引入** `langgraph` / `langchain*` / `crewai`，除非出现「自研无法以小改动覆盖、且有新鲜证据」的强理由（见 §5）。 |

---

## 0.1 关闭记录（2026-10-16）

本评估于 2026-10-16 **正式关闭**（HANDOFF_PLAN `P2-13` 要求「落地或关闭」）。结论：
**保持自研，不迁移 LangGraph / CrewAI，不引入新编排运行时依赖。**

关闭前**逐条核实**了 §0 结论所依赖的「现有能力」，避免结论建立在过期描述上：

| 结论依赖的能力 | 核实方式 | 结果 |
|---|---|---|
| 声明式 DAG | `orchestrator/definition.py` 存在且登记 `PIPELINE_V1` | ✅ |
| SQLite 检查点 | `orchestrator/store.py` 有 `orch_runs` | ✅ |
| 八态人审挂起 | 编排器挂起路径接 `approvals` 八态 | ✅ |
| 日预算熔断 | `orchestrator/budget.py` 存在 | ✅ |
| 生成类重试 | `definition.py` 的 `RetryPolicy(max_retries, 指数退避)`，按步骤分别配置（采集 0 次、其余 1–2 次） | ✅ |

「零新依赖」也已核实：`pyproject.toml` 中**没有** `langgraph` / `langchain*` / `crewai`。

**重新打开的条件**（与 §5 一致，且必须同时满足）：自研用小改动无法表达的编排需求已有
失败证据、适配层不破坏闸门前置与离线门、owner 接受传递依赖与双平台锁定成本。
未满足则本议题不再重复评估。

---

## 1. 现有编排器能力（代码事实）

| 能力 | 落点 | 行为摘要 |
|------|------|----------|
| 声明式流水线 | `definition.py` → `PIPELINE_V1` / `listing_to_care_v1` | Scout→定价→Stylist→QA→短视频脚本→达人建联草稿→人审→上架→订单→代发→物流→客服；`depends_on` 线性 DAG。建联只入库待审，不发送 |
| Run / Step 状态机 | `store.py` | Run：`pending/running/awaiting_approval/suspended/budget_paused/succeeded/failed/cancelled`；Step：含 `awaiting_approval` / `suspended` |
| 检查点 / 断点续跑 | `orch_runs` / `orch_steps` / `orch_llm_usage`（工作台 migrate ≥3） | 状态与 context JSON 落 SQLite；`resume` / `advance` 从 DB 继续 |
| 人审闸门 | `engine.approve/reject` + `steps.run_human_approval` 等 | 挂起写八态 `approvals`；批准后续跑；拒绝 → run `failed`；不绕过闸门 |
| 预算熔断 | `budget.py` + `llm.complete_json` | `LLM_DAILY_BUDGET_USD`；超限 → step `suspended` / run `budget_paused` + 告警 |
| 重试 | `RetryPolicy` + `llm` 内 `call_generation` | 生成类可指数退避；执行类（上架/代发/物流）`max_retries=0`（对齐 DESIGN §3.3） |
| QA 打回 | `engine._apply_qa_rewrite` | 重置 stylist/qa 为 pending，保留 `ai_trace` 历史 |
| 自动跑 | `scheduler.py` | `ORCHESTRATOR_AUTORUN=0` 默认关 |
| API | `routes.py` | `/api/orchestrator/runs*`：创建、列表、详情、approve/reject/resume/advance/cancel、step retry |

持久化边界（实现约定）：**权威状态只在工作台 SQLite 的 `orch_*` 行**；进程内对象可丢。步骤若在 `running` 时进程退出，`resume` 会把残留 `running` 收回收敛为可重入的 `pending`（见 `OrchestratorStore.recover_stale_running_steps`），再由 handler 幂等重入。业务副作用（草稿、审批单、上架）仍以工作台表与闸门原子 CLAIM 为准，不另建 LangGraph 式 thread checkpoint blob。

---

## 2. 与 LangGraph 的差异对照

| 维度 | 本仓库 `orchestrator/` | LangGraph（公开能力，推断标注） | 对本店的含义 |
|------|------------------------|----------------------------------|--------------|
| 状态机模型 | 显式 run/step 枚举 + 线性 `depends_on`；QA 打回为引擎内特判 | 图（节点/边/条件边）；状态常为 TypedDict / 可合并 channel（推断） | 当前流水线近线性，自研足够；复杂分支增多时图 DSL 更直观 |
| 检查点 | SQLite 行级 `orch_runs`/`orch_steps`，与工作台同库同锁 | `Checkpointer`（Memory/Sqlite/Postgres 等）按 thread 存图状态（推断） | 自研已与 `approvals`/drafts 同事务边界；换 LangGraph 要重做与八态闸门的接合 |
| 人审闸门 | 一等公民：`awaiting_approval` + SCHEMA 八态 + 飞书卡片 | `interrupt` / human-in-the-loop 可挂起图（推断） | 能力可对标，但本店闸门规则、原子 CLAIM、`payload_hash`、stale 重审已绑自研表 |
| 预算熔断 | 日预算 USD 记账在 `orch_llm_usage`，超限专用 run 态 | 无一等「店长日预算」；需自建节点或回调（推断） | 自研已贴业务；迁出后仍要自写同等逻辑 |
| 重试 | 按 `StepDef.kind` 区分生成/执行；执行类不自动重试 | RetryPolicy / 节点级重试可配（推断） | 语义已对齐 DESIGN §3.3；换栈不减少业务规则代码 |
| 可观测 | step `ai_trace`、rationale、用量行 | Studio / 事件流生态更强（推断） | 中期若要可视化调试可评估旁路接入，非替换主路径的理由 |
| 依赖面 | 仅本包 + 已有 httpx/pydantic/SQLite | 通常拉入 `langgraph` 及常见配套（版本与传递依赖需锁定） | **与「mock 先行、CI 离线门、Windows+Linux」冲突成本高** |

---

## 3. 可选：CrewAI 对照（非主候选）

| 维度 | CrewAI（推断） | 相对本编排器 |
|------|----------------|--------------|
| 模型 | Crew / Agent / Task 角色协作 | 适合「多角色讨论」原型；不天然等价于持久化电商流水线 |
| 检查点与恢复 | 弱于 LangGraph / 自研行级状态（推断） | 真店断点续跑、审批挂起不匹配 |
| 人审 / 涉钱 | 无 SCHEMA 八态、无 `TTS_WRITE_*` 接合 | 仍须外包一层本仓库闸门 → 双运行时 |
| 建议 | **不引入为运行时**；若做调研 Agent 沙盘可另仓试验，不进主 CI 依赖 | |

---

## 4. 迁移成本（粗估，非日历）

| 方案 | 触及面 | 主要风险 | 相对成本 |
|------|--------|----------|----------|
| A. 短期保留自研 | 文档 + 小缺口修补 | 图 DSL 表达力上限 | 低 |
| B. 中期局部引入 | 单条旁路 flow 适配器；主 API 仍 `/api/orchestrator` | 两套状态模型并存；检查点双写 | 中 |
| C. 全量替换为 LangGraph | `orchestrator/*`、migrate、路由、单测、DESIGN/SCHEMA 对齐 | 闸门/预算/幂等重写；依赖与离线 CI；回归面大 | **高，不建议** |

全量替换还须重做：八态审批挂起语义、`budget_paused`、QA rewrite、`ORCHESTRATOR_AUTORUN`、与 `WorkflowStore` 同锁写入。这些不是「换图引擎」能自动继承的。

---

## 5. 建议与「强理由才加依赖」门槛

**建议（拍板口径）**

1. **短期：保留自研** `src/shopkeeper/orchestrator/`，继续以 `orch_*` 为唯一检查点家。
2. **中期：局部引入**仅当出现清晰痛点——例如多条件分支图、需要标准 interrupt 可视化，且可用薄适配层把 LangGraph thread 映射回 `orch_runs.id`，**主涉钱路径仍只认八态闸门**。
3. **不建议全量替换**；CrewAI 不作主路径候选。

**引入 `langgraph`（或同类）的强理由门槛（须同时满足）**

- 自研用「小改动」无法表达的编排需求已写入 DESIGN/M1-PLAN 或 §12，且有失败证据（测试或真店事故）；
- 适配层证明不破坏闸门前置 / mock 契约 / `-m "not real"` 离线门；
- owner 接受传递依赖与 Windows+Linux 锁定成本，并在 DESIGN 修订记录明示。

未满足则保持零新编排运行时依赖。

---

## 6. 本评估附带的低风险加固（非迁移）

与本文同期的极小实现（详见代码与 `tests/orchestrator/`）：

1. **持久化边界**：`resume` 时回收崩溃残留的 step `running` → `pending`，并写 `ai_trace` 事件，避免静默双意。
2. **执行步幂等注释 + publish 短路径**：`publish` 等 execution handler 标明须可重入；若 context 已有成功上架结果则直接 `succeeded`，降低断点续跑重复外呼风险（仍受 `TTS_WRITE_*` 与闸门约束）。

不解锁 `TTS_WRITE_*`，不打开发货/退款/付款。

---

## 7. 验收

```text
PYTHONPATH=src python3 -m pytest -m "not real" tests/orchestrator/
```

期望：全绿。本文本身无运行时依赖变更；`pyproject` / `requirements` 不得因此新增 langgraph/crewai。
