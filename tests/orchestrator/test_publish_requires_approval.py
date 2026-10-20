"""上架必须持有**已批准**的审批单（独立复核发现的 P0-1）。

## 缺陷

`run_publish` 里有一段：

```python
if draft["status"] == "pending":
    decide_draft(ctx.workflow, draft_id, "approve",
                 note="orchestrator resume after approval")
```

注释写的是「orchestrator resume after approval」，但**没有任何代码验证这件事** ——
它假设「草稿还是 pending 就一定是人工批准后 resume 进来的」。

而 `publish` 步只要被**选中**就会进这个分支，于是存在这条路径：

1. `retry_step`（公开端点 `POST /runs/{id}/steps/{sid}/retry`）把任意步置回 `pending`
2. 依赖步恰好都 `succeeded` → `_next_ready_step` 选中 `publish`
3. `run_publish` 看到草稿是 `pending`，**自动批准并上架**
4. 审批单**全程是 `pending`**，却被消费成 `executed`

**实测（修复前）**：审批单 `create_listing: pending` 的状态下，
平台确实收到 `create_listing` 调用，商品上架。

**教训**：闸门不能只活在 DAG 的某一个节点里 —— 涉钱动作的守卫必须紧贴外呼本身。
这与本项目反复出现的「docstring/注释承诺未被代码强制」是同一类问题。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/1.html"


class _CountingPlatform(MockPlatform):
    """记录平台写调用次数 —— 这是「商品是否真的上架了」的唯一可信证据。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: dict[str, int] = {}

    def create_listing(self, payload):  # type: ignore[override]
        self.calls["create_listing"] = self.calls.get("create_listing", 0) + 1
        return super().create_listing(payload)


def _orch(tmp_path: Path) -> tuple[Orchestrator, WorkflowStore, _CountingPlatform]:
    store = WorkflowStore(tmp_path / "w.db")
    platform = _CountingPlatform()
    return (
        Orchestrator(store, platform=platform, env={"LLM_PROVIDER": "mock"}),
        store,
        platform,
    )


def _force_publish_selectable(orch: Orchestrator, rid: str) -> None:
    """把状态摆成「publish 是下一个可执行步」——模拟 retry_step 之后的样子。

    这是复现脚本用的最小构造，不是绕过修复：修复后即使这样摆，
    publish 也必须因为**没有 approved 审批单**而拒绝执行。
    """
    orch.retry_step(rid, "human_approval")
    orch.store.update_step(rid, "human_approval", status="succeeded", error=None, output={})
    orch.store.update_run(rid, status="running")


def _drive(orch: Orchestrator, rid: str, *, rounds: int = 40) -> None:
    for _ in range(rounds):
        if orch.store.get_run(rid)["status"] != "running":
            break
        orch.advance(rid, max_steps=1)


def test_publish_refuses_without_approved_approval(tmp_path):
    """**核心用例**：审批单仍是 pending 时，绝不能上架。"""
    orch, store, platform = _orch(tmp_path)
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]

    assert [(a["action_type"], a["status"]) for a in store.list_approvals()] == [
        ("create_listing", "pending")
    ]
    assert platform.calls == {}, "批准前就不该有任何平台写调用"

    _force_publish_selectable(orch, rid)
    _drive(orch, rid)

    assert platform.calls.get("create_listing", 0) == 0, (
        f"审批单未批准却上架了商品，平台调用 {platform.calls}"
    )
    still_pending = [
        a for a in store.list_approvals() if a["action_type"] == "create_listing"
    ]
    assert [a["status"] for a in still_pending] == ["pending"], "审批单不该被消费"


def test_publish_twice_started_pending_always_blocks(tmp_path):
    """反复驱动也挡得住（不能靠「第一次恰好没选中」过关）。"""
    orch, store, platform = _orch(tmp_path)
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]
    _force_publish_selectable(orch, rid)

    for _ in range(3):
        _drive(orch, rid)
        assert platform.calls.get("create_listing", 0) == 0


def test_approved_flow_still_publishes(tmp_path):
    """**反向**：正常批准后必须仍能上架 —— 别把功能锁死。"""
    orch, store, platform = _orch(tmp_path)
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]
    assert platform.calls == {}

    for _ in range(6):
        if orch.store.get_run(rid)["status"] != "awaiting_approval":
            break
        orch.approve(rid, note="ok")

    assert platform.calls.get("create_listing", 0) == 1, "批准后应恰好上架一次"
    assert orch.store.get_run(rid)["status"] == "succeeded"


def test_publish_guard_reads_approval_row_not_draft_status(tmp_path):
    """守卫必须查**审批单**，不能只看草稿状态 —— 这正是原先的缺陷所在。

    直接把审批单改成 rejected，草稿仍是 pending：
    守卫要按审批单拒绝，而不是被草稿的 pending 骗过去。
    """
    orch, store, platform = _orch(tmp_path)
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]

    with store._lock, store._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE approvals SET status = 'rejected' WHERE action_type = 'create_listing'"
        )

    _force_publish_selectable(orch, rid)
    _drive(orch, rid)

    assert platform.calls.get("create_listing", 0) == 0, "审批被拒后不得上架"
