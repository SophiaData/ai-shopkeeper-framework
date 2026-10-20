"""审批闸门的两处**永久卡死**回归（独立复核发现）。

两处都**不需要异常**，用正常的 API 调用就能触发 —— 这比崩溃更危险，
因为线上不会报错，只是那张草稿再也发不出去了，而且**没有任何线索**。

- **F1**：发布失败后再点一次发布（`run_side_effect` 回放失败结果），
  审批单被 claim 成 `executing`、草稿被投影成 `publishing`，
  然后回滚被 `if not outcome.replayed` 跳过 → 双双卡死。
- **F2**：新加的 reaper 只把审批单从 `executing` 收回，
  **没把草稿从 `publishing` 放回去** → 它宣称要救的那个场景其实没救成。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.workflow.approvals import reap_stale_executing
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore


class _FailingPlatform(MockPlatform):
    """`create_listing` 永远失败 —— 用来制造「发布失败」。"""

    name = "failing_platform"

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def create_listing(self, payload: dict):  # type: ignore[override]
        self.calls += 1
        from shopkeeper.errors import AdapterError
        from shopkeeper.models import AdapterResult

        return AdapterResult.failure(
            AdapterError.execution("TTS_API_ERROR", "模拟平台拒绝上架"),
            adapter=self.name,
        )


def _client(tmp_path: Path, platform):
    store = WorkflowStore(tmp_path / "workflow.db")
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform)
    return store, TestClient(app)


def _approved_draft(client: TestClient) -> str:
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "收纳盒", "specs": "PP 材质"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "ok"}).status_code == 200
    return draft_id


# --------------------------------------------------------------------------
# F1：失败后再点一次发布 → 双双卡死
# --------------------------------------------------------------------------


def test_republish_after_failure_does_not_wedge(tmp_path):
    """**核心**：发布失败后再次发布，不能让草稿卡在 `publishing`。

    原实现里回滚被 `if not outcome.replayed:` 挡住 —— 第二次点击时
    `run_side_effect` 发现同幂等键已是 `failed`，**直接回放结果、不再调用 fn()**，
    于是走 `replayed` 分支、跳过回滚，留下：
        草稿 `publishing` + 审批单 `executing`
    之后每次发布都报「未审批，不能上架」，重新审批也不行。
    """
    platform = _FailingPlatform()
    store, client = _client(tmp_path, platform)
    draft_id = _approved_draft(client)

    first = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert first.status_code >= 400, "第一次应失败"

    second = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert second.status_code >= 400

    draft = store.get_draft(draft_id)
    assert draft is not None
    assert draft["status"] != "publishing", (
        f"草稿卡在 publishing —— 永久无法发布（实际 {draft['status']}）"
    )

    approvals = [a for a in store.list_approvals() if a["entity_id"] == draft_id]
    assert approvals, "应有审批单"
    assert approvals[0]["status"] != "executing", (
        f"审批单卡在 executing —— claim_for_execute 会永远拒绝（实际 {approvals[0]['status']}）"
    )


def test_republish_after_failure_does_not_call_platform_again(tmp_path):
    """卡死修复不能变成「失败后重试时重复外呼」—— 幂等仍须生效。"""
    platform = _FailingPlatform()
    store, client = _client(tmp_path, platform)
    draft_id = _approved_draft(client)

    client.post(f"/api/workflow/drafts/{draft_id}/publish")
    after_first = platform.calls
    client.post(f"/api/workflow/drafts/{draft_id}/publish")

    assert platform.calls == after_first, "回放失败结果时不该再次外呼"


def test_draft_recoverable_after_failed_publish(tmp_path):
    """失败后应能重新走一遍（草稿回到可操作状态，而不是死住）。"""
    platform = _FailingPlatform()
    store, client = _client(tmp_path, platform)
    draft_id = _approved_draft(client)

    client.post(f"/api/workflow/drafts/{draft_id}/publish")
    client.post(f"/api/workflow/drafts/{draft_id}/publish")

    draft = store.get_draft(draft_id)
    assert draft is not None
    assert draft["status"] in {"approved", "pending", "needs_revision", "rejected"}, (
        f"草稿应回到可操作状态，实际 {draft['status']}"
    )


# --------------------------------------------------------------------------
# F2：reaper 必须把草稿一并放回
# --------------------------------------------------------------------------


def test_reaper_releases_draft_too(tmp_path):
    """reaper 只放审批单、不放草稿 = 它宣称要救的场景其实没救成。

    审批单与草稿是**两行状态**：把审批单从 `executing` 收回、草稿仍停在
    `publishing`，下一次发布依然报「未审批，不能上架」。
    """
    platform = _FailingPlatform()
    store, client = _client(tmp_path, platform)
    draft_id = _approved_draft(client)

    # 人为把两者都推到卡死状态（模拟执行中途进程死掉）
    client.post(f"/api/workflow/drafts/{draft_id}/publish")
    client.post(f"/api/workflow/drafts/{draft_id}/publish")

    approvals = [a for a in store.list_approvals() if a["entity_id"] == draft_id]
    aid = approvals[0]["id"]

    # 把 executing_since 推早，让 reaper 认为它卡住了
    with store._lock, store._connect() as conn:  # noqa: SLF001 - 测试需直接构造卡死态
        conn.execute(
            "UPDATE approvals SET status='executing', executing_since=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), aid),
        )
        conn.execute(
            "UPDATE drafts SET status='publishing' WHERE id=?", (draft_id,)
        )

    reaped = reap_stale_executing(store)
    assert reaped, "卡死的审批单应被收回"

    assert store.get_approval(aid)["status"] == "execute_failed"
    draft = store.get_draft(draft_id)
    assert draft is not None
    assert draft["status"] != "publishing", (
        f"草稿仍卡在 publishing —— reaper 没真正解开（实际 {draft['status']}）"
    )


def test_reaper_does_not_release_recent_executing(tmp_path):
    """刚进入 executing 的单子不该被误判 —— 误判会造成重复执行的风险。"""
    platform = _FailingPlatform()
    store, client = _client(tmp_path, platform)
    draft_id = _approved_draft(client)
    client.post(f"/api/workflow/drafts/{draft_id}/publish")

    approvals = [a for a in store.list_approvals() if a["entity_id"] == draft_id]
    aid = approvals[0]["id"]
    with store._lock, store._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE approvals SET status='executing', executing_since=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), aid),
        )

    assert reap_stale_executing(store) == [], "刚执行的单子不该被收回"
    assert store.get_approval(aid)["status"] == "executing"


def test_reaper_is_idempotent(tmp_path):
    """重复跑 reaper 无副作用（第二次没有可收回的）。"""
    platform = _FailingPlatform()
    store, client = _client(tmp_path, platform)
    draft_id = _approved_draft(client)
    client.post(f"/api/workflow/drafts/{draft_id}/publish")

    approvals = [a for a in store.list_approvals() if a["entity_id"] == draft_id]
    aid = approvals[0]["id"]
    with store._lock, store._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE approvals SET status='executing', executing_since=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), aid),
        )

    assert len(reap_stale_executing(store)) == 1
    assert reap_stale_executing(store) == []


def test_reaper_lands_on_failed_not_approved(tmp_path):
    """收回到 `execute_failed` 而**不是** `approved` —— 不能自动绕过人工闸门。

    执行到一半的单子**可能已在平台生效**，自动回到「可执行」会重复上架。
    """
    platform = _FailingPlatform()
    store, client = _client(tmp_path, platform)
    draft_id = _approved_draft(client)
    client.post(f"/api/workflow/drafts/{draft_id}/publish")

    approvals = [a for a in store.list_approvals() if a["entity_id"] == draft_id]
    aid = approvals[0]["id"]
    with store._lock, store._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE approvals SET status='executing', executing_since=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), aid),
        )

    reap_stale_executing(store)
    assert store.get_approval(aid)["status"] == "execute_failed"
    assert store.get_approval(aid)["status"] != "approved"
