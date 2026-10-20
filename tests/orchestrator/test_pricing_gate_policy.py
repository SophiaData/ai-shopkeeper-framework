"""定价偏离闸门必须**真的按策略表裁决**（独立复核发现）。

## 缺陷

`run_pricing` 里：

```python
gate = evaluate_gate_policy(policy, action_type="pricing_override", ...)
...
approval = ctx.workflow.add_approval({...})   # 无条件
```

`gate` 只被写进 `decision_note`，**从不影响是否建单**。于是
`require_approval: False` 毫无作用：把策略阈值放宽到 100%（偏离多少都免审），
流程**照样**停下来等人批。

**实测（修复前）**：策略裁决 `auto_allow` / `require_approval: False`，
仍然建出 `pricing_override: pending` 单并把 run 挂起。

DESIGN §165 写的是「闸门裁决是纯函数，**输入来自规则表**」——
规则表改不动行为，这个函数就只是装饰。

## 边界（本文件两个方向都要钉住）

- 策略**放宽**时：不建单、留 `gate_decisions` 痕、`price_source=policy_auto_allow`
- 策略**默认**时：仍然建单挂起 —— **不会**因此放宽任何涉钱动作
- 策略**缺失**时：默认必审（`evaluate_gate_policy` 对 `None` 返回必审）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/1.html"


def _start(tmp_path: Path, *, threshold_pct: int | None):
    store = WorkflowStore(tmp_path / "w.db")
    if threshold_pct is not None:
        with store._lock, store._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE gate_policies SET threshold_json = ? WHERE action_type = 'pricing_override'",
                (f'{{"price_deviation_pct": {threshold_pct}}}',),
            )
    orch = Orchestrator(store, platform=MockPlatform(), env={"LLM_PROVIDER": "mock"})
    rid = orch.start(
        url=FIXTURE_URL, advance=True, extra_input={"force_price_deviation": True}
    )["id"]
    ctx = orch.store.get_run(rid)["context"] or {}
    return store, orch, rid, ctx


def _pricing_approvals(store: WorkflowStore) -> list[dict]:
    return [a for a in store.list_approvals() if a["action_type"] == "pricing_override"]


def test_relaxed_policy_actually_waives_approval(tmp_path):
    """**核心**：策略说免审，就必须真的不建单。

    这是修复前失败的用例 —— 阈值放到 100% 也照样建单挂起。
    """
    store, orch, rid, ctx = _start(tmp_path, threshold_pct=100)

    assert _pricing_approvals(store) == [], "策略裁决 auto_allow 却仍建了审批单"
    assert (ctx.get("pricing") or {}).get("price_source") == "policy_auto_allow"
    assert orch.store.get_run(rid)["status"] != "failed"


def test_waived_run_advances_past_pricing(tmp_path):
    """免审不能把流程卡住 —— 必须能继续往下走。"""
    store, orch, rid, ctx = _start(tmp_path, threshold_pct=100)
    steps = {s["step_id"]: s["status"] for s in orch.store.get_steps(rid)}
    assert steps.get("pricing") == "succeeded", steps
    # 后续步骤应当已被推进（至少不再停在 pricing）
    assert orch.store.get_run(rid)["current_step"] != "pricing"


def test_waiver_is_recorded_in_gate_decisions(tmp_path):
    """免审**也要留痕** —— 否则事后无法解释「为什么这次没走审批」。"""
    store, orch, rid, _ctx = _start(tmp_path, threshold_pct=100)
    with store._connect() as conn:  # noqa: SLF001
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT decision, reason, approval_id FROM gate_decisions"
            )
        ]
    assert rows, "免审必须写 gate_decisions"
    waived = [r for r in rows if r["approval_id"] is None]
    assert waived, "免审记录应带 approval_id=None（没有审批单）"


def test_default_policy_still_requires_approval(tmp_path):
    """**反向**：默认策略（阈值 10%）下偏离 12% 仍必须建单挂起。

    这条守的是「不能因为放宽了就顺手把默认也放宽」——
    涉钱动作的默认必须是最严的一档。
    """
    store, orch, rid, ctx = _start(tmp_path, threshold_pct=None)

    approvals = _pricing_approvals(store)
    assert len(approvals) == 1, "默认策略下应建 pricing_override 单"
    assert approvals[0]["status"] == "pending"
    assert (ctx.get("pricing") or {}).get("price_source") == "pending_override_approval"
    run = orch.store.get_run(rid)
    assert run["status"] == "awaiting_approval"
    assert run["current_step"] == "pricing"


def test_tight_policy_also_requires_approval(tmp_path):
    """把阈值**收紧**（1%）同样要建单 —— 两个方向都要能改行为。"""
    store, _orch, _rid, _ctx = _start(tmp_path, threshold_pct=1)
    assert len(_pricing_approvals(store)) == 1
