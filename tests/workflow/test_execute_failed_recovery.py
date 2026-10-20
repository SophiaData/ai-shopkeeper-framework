"""执行失败后**恢复路径必须通畅，且必须写在错误信息里**。

## 复核提的疑虑

复核指出：`publish_listing` 失败时把审批单置 `execute_failed`、草稿 decide 回
`approved`，而 `claim_for_execute` 要求状态是 `approved` —— 于是「15 分钟后再重试
必然撞过期」，run 像被永久卡住。

## 我核实的结果：**路径是通畅的**，但**提示缺失**

`execute_failed` 在状态机里的唯一出口是 `resubmit → pending`
（`domain/approval_state.py` 的 MATRIX）。而 `POST /publish` 会走
`ensure_listing_approval`，后者对非 `pending` 的既有单**自动 resubmit**：

```
execute_failed --(再次 ensure)--> pending --(owner 批准)--> approved --(CLAIM)--> executing
```

实测确认这条链路可用（本文件 `test_recovery_path_works`）。

**所以不是死路。** 但有一个真实缺口：失败时抛出的错误只说
「平台上架未执行：xxx」，**没有告诉操作者再点一次就会重开待审单** ——
而界面上草稿显示 `pending`（看起来像「已批准过」），审批单却是 `execute_failed`，
两者不一致会让人不敢重试。

## 本文件钉住两件事

- 恢复链路**真的能用**（防止将来有人把 resubmit 拿掉而无人察觉）
- 错误信息**必须包含可执行的下一步**（否则等于让操作者猜）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.workflow.approvals import (
    ApprovalEvent,
    apply_approval_event,
    claim_for_execute,
    ensure_listing_approval,
)
from shopkeeper.workflow.store import WorkflowStore


@pytest.fixture
def draft(store):
    return store.add_draft(
        kind="listing",
        status="pending",
        title="测试商品",
        body_th="",
        body_en="",
        payload={},
        qa={"ok": True},
        source="orchestrator",
    )


@pytest.fixture
def store(tmp_path):
    return WorkflowStore(tmp_path / "w.db")


def _fail_execution(store: WorkflowStore, draft: dict) -> dict:
    """把审批单推到 execute_failed 的真实路径。"""
    approval = ensure_listing_approval(store, draft)
    apply_approval_event(store, approval["id"], ApprovalEvent.APPROVE, decided_by="owner")
    apply_approval_event(store, approval["id"], ApprovalEvent.CLAIM)
    apply_approval_event(
        store, approval["id"], ApprovalEvent.EXECUTE_FAIL, execution_error="boom"
    )
    return approval


def test_execute_failed_is_the_terminal_of_that_attempt(store, draft):
    approval = _fail_execution(store, draft)
    assert store.get_approval(approval["id"])["status"] == "execute_failed"


def test_direct_reapprove_is_rejected(store, draft):
    """**这条容易被误判成死路**：`execute_failed` 不能直接再 approve。

    但这不意味着卡死 —— 正确入口是 resubmit（下一条用例）。
    """
    from shopkeeper.workflow.approvals import ApprovalError

    approval = _fail_execution(store, draft)
    with pytest.raises(ApprovalError, match="ILLEGAL"):
        apply_approval_event(store, approval["id"], ApprovalEvent.APPROVE, decided_by="owner")


def test_recovery_path_works(store, draft):
    """**核心**：失败后再次 `ensure` → 单子回到 pending → 批准 → 可 CLAIM。

    这正是 `POST /publish` 走的路。若这条断了，失败就是真死路。
    """
    approval = _fail_execution(store, draft)

    reopened = ensure_listing_approval(store, store.get_draft(draft["id"]))
    assert reopened["status"] == "pending", "失败后再次 ensure 应重开待审单"

    apply_approval_event(store, reopened["id"], ApprovalEvent.APPROVE, decided_by="owner")
    claimed = claim_for_execute(store, reopened["id"])
    assert claimed["status"] == "executing", "重新批准后应能原子消费"


def test_recovery_reuses_the_same_approval_row(store, draft):
    """恢复走的是 **resubmit 同一张单**，不是新建一张。

    ⚠️ 这条断言是必需的：`ensure_listing_approval` 在 resubmit 失败时会
    **继续往下走到「新建审批单」**。所以只断言「返回 pending」是**测不出**
    resubmit 是否工作的 —— 我第一版就是这样，把 resubmit 注入成非法事件后
    用例**依然通过**。断言 `id` 不变才真正钉住那条路径。
    """
    approval = _fail_execution(store, draft)

    reopened = ensure_listing_approval(store, store.get_draft(draft["id"]))
    assert reopened["id"] == approval["id"], "应复用同一张单（走 resubmit）而非新建"
    assert len(store.list_approvals()) == 1, "不该多出一张单"


def test_failure_message_tells_the_operator_what_to_do(store, draft):
    """失败信息必须说清「再点一次会重开待审单」+「先确认是否已建成」。

    只说「未执行」等于让操作者猜；而界面此时草稿是 `pending`、
    审批单是 `execute_failed`，不一致会让人不敢重试。
    """
    import inspect

    from shopkeeper.workflow import gate

    src = inspect.getsource(gate.publish_listing)
    assert "execute_failed" in src, "错误信息应提到审批单状态"
    assert "重新" in src or "再点" in src or "再次点击" in src, "应说明如何恢复"
    assert "卖家中心" in src, "应提醒先确认平台侧是否已建成（避免重复上架）"
