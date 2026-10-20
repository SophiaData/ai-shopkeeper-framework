"""缺失质检必须失败，不能默认通过（独立复核发现的 P1-3）。

## 缺陷

`run_human_approval` 里：

```python
qa = (run.get("context") or {}).get("qa") or {"ok": True, "issues": []}
```

缺 `qa` 时**伪造一条「通过」的记录**，并作为草稿的 `qa` 字段存库。
下游（审批页、报告）看到的是「质检通过」，而实际上**根本没跑过质检**。

触发面不只是「手工改库」：

- `_next_ready_step` 按「第一个非 succeeded 的步」线性扫描，只要 `qa` 步不是
  `succeeded`（例如被 `retry_step` 置回 `pending`）而后面的步依赖恰好都满足，
  `human_approval` 就会被选中执行
- 旧版本写下的 run 行本身就可能没有 `qa` 键

**默认通过在这里是最坏的默认**：它把「没检查」记成「检查过且合格」。

## ⚠️ 位置也很关键（我第一版踩了）

校验必须放在 `existing_approval_id` 早退**之前**。我第一版放在后面，
复现用例走的正是早退分支（已有审批单 → 直接 `STEP_AWAITING_APPROVAL`），
校验**根本没机会执行** —— 「修了但没生效」。本文件有一条用例专门钉这个顺序。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/1.html"


def _orch(tmp_path: Path):
    store = WorkflowStore(tmp_path / "w.db")
    orch = Orchestrator(store, platform=MockPlatform(), env={"LLM_PROVIDER": "mock"})
    return orch, store


def _drop_qa_and_rerun(orch, store, rid: str) -> dict:
    """抹掉 context 里的 qa，把 human_approval 置回 pending，再跑一步。"""
    with store._lock, store._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT context_json FROM orch_runs WHERE id = ?", (rid,)
        ).fetchone()
        ctx = json.loads(row["context_json"])
        ctx.pop("qa", None)
        conn.execute(
            "UPDATE orch_runs SET context_json = ?, status = 'running', "
            "current_step = 'human_approval', finished_at = NULL WHERE id = ?",
            (json.dumps(ctx, ensure_ascii=False), rid),
        )
        conn.execute(
            "UPDATE orch_steps SET status = 'pending', error_json = NULL "
            "WHERE run_id = ? AND step_id = 'human_approval'",
            (rid,),
        )
    orch.advance(rid, max_steps=1)
    return [s for s in orch.store.get_steps(rid) if s["step_id"] == "human_approval"][0]


def test_missing_qa_fails_instead_of_defaulting_open(tmp_path):
    """**核心**：缺 `qa` 时必须失败并给出 `ORCH_QA_MISSING`。"""
    orch, store = _orch(tmp_path)
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]

    step = _drop_qa_and_rerun(orch, store, rid)

    assert step["status"] == "failed", f"应为失败，实际 {step['status']}"
    assert (step["error"] or {}).get("code") == "ORCH_QA_MISSING"


def test_guard_runs_before_approval_early_return(tmp_path):
    """**顺序用例**：已有审批单时也必须先校验 qa。

    我第一版把校验放在 `existing_approval_id` 早退**之后**，
    于是复现走的正是早退分支，校验没机会执行 —— 「修了但没生效」。
    这条用例专门钉住顺序。
    """
    orch, store = _orch(tmp_path)
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]
    ctx = orch.store.get_run(rid)["context"] or {}
    assert ctx.get("approval_id"), "前置条件：此时已存在审批单（会触发早退）"

    step = _drop_qa_and_rerun(orch, store, rid)

    assert step["status"] == "failed", "早退分支也不得绕过 qa 校验"
    assert (step["error"] or {}).get("code") == "ORCH_QA_MISSING"


def test_no_fake_qa_is_written_to_draft(tmp_path):
    """缺 `qa` 时**不得**新建带假 qa 的草稿。"""
    orch, store = _orch(tmp_path)
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]
    before = len([d for d in store.list_drafts() if d["kind"] == "listing"])

    _drop_qa_and_rerun(orch, store, rid)

    after = len([d for d in store.list_drafts() if d["kind"] == "listing"])
    assert after == before, "失败路径不该新增 listing 草稿"


def test_real_qa_still_works(tmp_path):
    """**反向**：有真实 qa 时正常流程不受影响。"""
    orch, store = _orch(tmp_path)
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]

    assert orch.store.get_run(rid)["status"] == "awaiting_approval"
    listings = [d for d in store.list_drafts() if d["kind"] == "listing"]
    assert len(listings) == 1
    assert (listings[0].get("qa") or {}).get("ok") is True
