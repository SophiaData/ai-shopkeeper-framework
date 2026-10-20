"""发布之后打回必须说清楚「撤不掉」（独立复核发现的 P0-2）。

## 缺陷

商品上架后调 `reject`，得到的是一句：

```
审批状态不允许：ILLEGAL: executed+reject
```

对操作者来说这只是「不行」——**没有任何信息**说明
「商品其实已经在架上了，你要的是下架而不是打回」。

而**下架能力当前不存在**：`PlatformAdapter` 协议里没有 delist，
`stock_circuit.propose_delist` 只在断货（`desired_qty=0`）时建单。

## 本 PR 的边界（按 §0 不臆造外部契约）

**不新增**平台 delist 接口。只做两件确定能做对的事：

1. 明确告知「已发布，打回**不会**撤下商品」，并给出可执行路径
2. 不让调用方误以为状态已经回滚

**这是一条单向棘轮**：只有 publish 没有 delist。它必须在错误信息里
被说破，而不是留给操作者去猜。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.orchestrator.engine import Orchestrator, OrchestratorError
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/1.html"


@pytest.fixture
def published_run(tmp_path):
    store = WorkflowStore(tmp_path / "w.db")
    orch = Orchestrator(store, platform=MockPlatform(), env={"LLM_PROVIDER": "mock"})
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]
    for _ in range(6):
        if orch.store.get_run(rid)["status"] != "awaiting_approval":
            break
        orch.approve(rid, note="ok")
    assert (orch.store.get_run(rid)["context"] or {}).get("publish_result", {}).get("published")
    return orch, store, rid


def test_reject_after_publish_explains_it_cannot_undo(published_run):
    """**核心**：错误信息必须说清「打回不撤下商品」。"""
    orch, store, rid = published_run
    with pytest.raises(OrchestratorError) as exc:
        orch.reject(rid, note="文案写错了")
    msg = str(exc.value)
    assert "不会撤下商品" in msg or "不会" in msg and "撤下" in msg, msg


def test_reject_after_publish_gives_executable_path(published_run):
    """只说不行是不够的 —— 要告诉操作者「那该怎么办」。"""
    orch, store, rid = published_run
    with pytest.raises(OrchestratorError) as exc:
        orch.reject(rid, note="x")
    msg = str(exc.value)
    assert "卖家中心" in msg, "应指出人工下架的路径"
    assert "propose_delist" in msg, "应指出断货场景的既有机制"


def test_reject_does_not_claim_state_rolled_back(published_run):
    """打回失败后状态**不得**被误改（原先会走到 sync_draft_projection）。"""
    orch, store, rid = published_run
    ctx_before = orch.store.get_run(rid)["context"]
    did = ctx_before.get("draft_id")

    with pytest.raises(OrchestratorError):
        orch.reject(rid, note="x")

    assert store.get_draft(did)["status"] == "published", "商品仍是 published"
    approvals = {a["action_type"]: a["status"] for a in store.list_approvals()}
    assert approvals["create_listing"] == "executed", "审批单状态不该被改"


def test_pending_approval_reject_still_works(tmp_path):
    """**反向**：还没执行时打回必须照常工作 —— 别把正常路径封死。"""
    store = WorkflowStore(tmp_path / "w.db")
    orch = Orchestrator(store, platform=MockPlatform(), env={"LLM_PROVIDER": "mock"})
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]

    # 还没批准 → reject 应正常生效
    result = orch.reject(rid, note="文案不合格")
    assert result is not None
    approvals = {a["action_type"]: a["status"] for a in store.list_approvals()}
    assert approvals.get("create_listing") == "rejected"
