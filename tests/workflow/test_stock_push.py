"""库存同步缓冲 stock-push：缓冲系数、未审批拒绝、mock 推送成功。"""

from __future__ import annotations

from pathlib import Path

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.adapters.tiktok_shop import OfflineTikTokPlatform
from shopkeeper.errors import AdapterError
from shopkeeper.workflow.drafts import DraftError, decide_draft
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.stock import compute_buffered_qty, push_stock
from shopkeeper.workflow.store import WorkflowStore


def _client(tmp_path: Path, platform: MockPlatform | None = None) -> tuple[WorkflowStore, MockPlatform, TestClient]:
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = platform or MockPlatform()
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform)
    return store, platform, TestClient(app)


def _approved_listing(client: TestClient, *, publish: bool = True) -> str:
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "收纳盒", "specs": "PP 材质"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "ok"}).status_code == 200
    if publish:
        assert client.post(f"/api/workflow/drafts/{draft_id}/publish").status_code == 200
    return draft_id


def test_compute_buffered_qty():
    assert compute_buffered_qty(100, 0.8) == 80
    assert compute_buffered_qty(10, 0.8) == 8
    assert compute_buffered_qty(1, 0.8) == 0
    assert compute_buffered_qty(100, 0.8, daily_cap=50) == 50
    assert compute_buffered_qty(0, 0.8) == 0
    with pytest.raises(DraftError, match="不能为负"):
        compute_buffered_qty(-1, 0.8)
    with pytest.raises(DraftError, match="buffer_factor"):
        compute_buffered_qty(10, 0.0)
    with pytest.raises(DraftError, match="buffer_factor"):
        compute_buffered_qty(10, 1.2)


def test_stock_push_success_mock(tmp_path: Path):
    store, platform, client = _client(tmp_path)
    listing_id = _approved_listing(client)

    pushed = client.post(
        f"/api/workflow/listings/{listing_id}/stock-push",
        json={"desired_qty": 100, "buffer_factor": 0.8},
    )
    assert pushed.status_code == 200, pushed.text
    body = pushed.json()
    assert body["pushed_qty"] == 80
    assert body["desired_qty"] == 100
    assert body["buffer_factor"] == 0.8
    assert body["stock_sync_state"]["listing_id"] == listing_id
    assert body["stock_sync_state"]["last_pushed_at"]
    assert body["stock_sync_state"]["last_error"] == ""
    assert len(platform.update_stock_calls) == 1
    assert platform.update_stock_calls[0]["quantity"] == 80
    assert platform.stock_by_listing[listing_id] == 80

    state = client.get(f"/api/workflow/listings/{listing_id}/stock-sync")
    assert state.status_code == 200
    assert state.json()["desired_qty"] == 100
    assert state.json()["buffer_factor"] == 0.8

    # DESIGN §5 路径别名
    alias = client.post(
        f"/listings/{listing_id}/stock-push",
        json={"desired_qty": 50, "buffer_factor": 0.5},
    )
    assert alias.status_code == 200
    assert alias.json()["pushed_qty"] == 25
    assert platform.stock_by_listing[listing_id] == 25

    assert client.post("/api/workflow/orders/1/ship").status_code == 403
    assert client.post("/api/workflow/orders/1/refund").status_code == 403
    assert platform.update_tracking_calls == []


def test_stock_push_rejects_unapproved(tmp_path: Path):
    store, platform, client = _client(tmp_path)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "收纳盒", "specs": "PP"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]

    blocked = client.post(
        f"/api/workflow/listings/{draft_id}/stock-push",
        json={"desired_qty": 20, "buffer_factor": 0.8},
    )
    assert blocked.status_code == 409
    assert "未审批" in blocked.json()["detail"]
    assert platform.update_stock_calls == []
    assert store.get_stock_sync_state(draft_id) is None

    assert client.post(f"/api/workflow/drafts/{draft_id}/reject", json={"note": "no"}).status_code == 200
    rejected = client.post(
        f"/api/workflow/listings/{draft_id}/stock-push",
        json={"desired_qty": 20},
    )
    assert rejected.status_code == 409
    assert "拒绝" in rejected.json()["detail"]
    assert platform.update_stock_calls == []


def test_stock_push_gate_adapter_blocked(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf.db")
    draft = store.add_draft(
        kind="listing",
        status="pending",
        title="box",
        body_th="th",
        body_en="en",
        payload={},
        qa={"ok": True, "issues": []},
        source="test",
        degraded=False,
    )
    decide_draft(store, draft["id"], "approve", "ok")
    with pytest.raises(DraftError, match="TTS_WRITE_BLOCKED|库存推送未执行"):
        push_stock(
            store,
            OfflineTikTokPlatform("offline"),
            draft["id"],
            desired_qty=40,
            buffer_factor=0.8,
        )
    state = store.get_stock_sync_state(draft["id"])
    assert state is not None
    assert "TTS_WRITE_BLOCKED" in state["last_error"]
    assert state["last_pushed_at"] is None


def test_stock_push_records_adapter_failure(tmp_path: Path):
    platform = MockPlatform(fail=AdapterError.policy("TTS_WRITE_BLOCKED", "blocked", action="update_stock"))
    store, _p, client = _client(tmp_path, platform=platform)
    listing_id = _approved_listing(client, publish=False)
    failed = client.post(
        f"/api/workflow/listings/{listing_id}/stock-push",
        json={"desired_qty": 10, "buffer_factor": 1.0},
    )
    assert failed.status_code == 409
    assert "TTS_WRITE_BLOCKED" in failed.json()["detail"]
    state = store.get_stock_sync_state(listing_id)
    assert state is not None
    assert "TTS_WRITE_BLOCKED" in state["last_error"]


# --------------------------------------------------------------------------
# P2-04 断货熔断**接线**验证（此前熔断模块没有任何生产调用方）
# --------------------------------------------------------------------------


def test_stock_out_alerts_and_proposes_delist(tmp_path: Path):
    """推 0 必须**告警 + 生成下架建议**，不能安静地推 0。

    这是 P2-04 的原始缺口：`push_stock` 在 `desired_qty=0` 时安静地推 0 ——
    供应商断货了卖家不会知道，商品继续按原价挂着直到有人下单才发现发不出货。
    """
    store, platform, client = _client(tmp_path)
    listing_id = _approved_listing(client)

    pushed = client.post(
        f"/api/workflow/listings/{listing_id}/stock-push",
        json={"desired_qty": 0, "buffer_factor": 0.8},
    )
    assert pushed.status_code == 200, pushed.text
    body = pushed.json()

    assert body["pushed_qty"] == 0
    circuit = body["stock_circuit"]
    assert circuit["action"] == "delist_listing"
    assert circuit["status"] == "pending", "必须是待审，不能自动执行"

    # 审批队列里确实有这张单
    approvals = [a for a in store.list_approvals() if a["action_type"] == "delist_listing"]
    assert len(approvals) == 1
    assert approvals[0]["status"] == "pending"
    assert approvals[0]["executed_at"] is None, "下架是涉钱动作，不能自动执行"


def test_stock_ok_does_not_propose_delist(tmp_path: Path):
    """有货时不该产生下架建议 —— 否则审批队列会被正常商品刷满。"""
    store, platform, client = _client(tmp_path)
    listing_id = _approved_listing(client)

    pushed = client.post(
        f"/api/workflow/listings/{listing_id}/stock-push",
        json={"desired_qty": 100, "buffer_factor": 0.8},
    )
    assert pushed.status_code == 200
    assert "stock_circuit" not in pushed.json() or not pushed.json().get("stock_circuit")
    assert [a for a in store.list_approvals() if a["action_type"] == "delist_listing"] == []


def test_repeated_stock_out_does_not_pile_up_approvals(tmp_path: Path):
    """同一商品连续断货**只建一张**待审单 —— 幂等，避免刷满审批队列。"""
    store, platform, client = _client(tmp_path)
    listing_id = _approved_listing(client)

    for _ in range(3):
        r = client.post(
            f"/api/workflow/listings/{listing_id}/stock-push",
            json={"desired_qty": 0, "buffer_factor": 0.8},
        )
        assert r.status_code == 200

    approvals = [a for a in store.list_approvals() if a["action_type"] == "delist_listing"]
    assert len(approvals) == 1, f"应只有一张待审单，实际 {len(approvals)} 张"


def test_circuit_failure_does_not_break_push(tmp_path: Path, monkeypatch):
    """熔断只是**附加能力**：它出错不能让推送接口报错。

    库存已经推出去了，若因为「提醒没发出去」就让接口 500，
    调用方会以为推送失败而重试 —— 重试会真的再推一次。
    """
    from shopkeeper.workflow import review_routes

    def boom(*args, **kwargs):
        raise RuntimeError("模拟告警系统故障")

    monkeypatch.setattr(review_routes, "propose_delist", boom)

    store, platform, client = _client(tmp_path)
    listing_id = _approved_listing(client)
    pushed = client.post(
        f"/api/workflow/listings/{listing_id}/stock-push",
        json={"desired_qty": 0, "buffer_factor": 0.8},
    )

    assert pushed.status_code == 200, "熔断故障不该让推送失败"
    assert pushed.json()["pushed_qty"] == 0
    assert "error" in pushed.json()["stock_circuit"], "故障要如实记录，不静默吞掉"

def test_stock_a_to_b_to_a_does_not_replay_old_success(tmp_path: Path):
    _store, platform, client = _client(tmp_path)
    listing_id = _approved_listing(client)

    first = client.post(f"/api/workflow/listings/{listing_id}/stock-push", json={"desired_qty": 100, "buffer_factor": 0.8})
    assert first.status_code == 200 and first.json()["pushed_qty"] == 80
    time.sleep(0.002)
    second = client.post(f"/api/workflow/listings/{listing_id}/stock-push", json={"desired_qty": 50, "buffer_factor": 0.8})
    assert second.status_code == 200 and second.json()["pushed_qty"] == 40
    time.sleep(0.002)
    third = client.post(f"/api/workflow/listings/{listing_id}/stock-push", json={"desired_qty": 100, "buffer_factor": 0.8})
    assert third.status_code == 200, third.text
    assert third.json()["pushed_qty"] == 80
    assert third.json()["idempotent_replay"] is False
    assert platform.stock_by_listing[listing_id] == 80
    assert len(platform.update_stock_calls) == 3
