"""草稿队列：质检通过才能审核，审核不会上架、发货或退款。"""

from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.workflow.drafts import decide_draft, generate_drafts
from shopkeeper.workflow.providers import ChatProvider, MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore


def _stack(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    provider = MockChatProvider()
    app = FastAPI()
    mount_review(app, store=store, provider=provider)
    return store, provider, TestClient(app)


def test_mock_listing_can_be_approved_without_publishing(tmp_path):
    store, provider, client = _stack(tmp_path)
    created = generate_drafts(store, provider, "listing", [{"name": "抽屉分隔盒", "specs": "外径 20cm，材质 PP"}])
    draft = created[0]
    assert draft["status"] == "pending"
    assert draft["qa"]["ok"] is True
    approved = decide_draft(store, draft["id"], "approve", "核对过尺寸")
    assert approved["status"] == "approved"
    assert approved["published"] is False
    assert approved["sent"] is False


def test_failed_qa_and_refund_language_cannot_be_approved(tmp_path):
    store, provider, _client = _stack(tmp_path)
    banned = generate_drafts(store, provider, "listing", [{"name": "防水盒", "specs": "供应商写了100%防水"}])[0]
    assert banned["status"] == "needs_revision"
    with pytest.raises(Exception, match="待审"):
        decide_draft(store, banned["id"], "approve")
    refund = generate_drafts(store, provider, "customer_reply", [{"name": "买家", "message": "我要退款"}])[0]
    assert refund["qa"]["escalate"] is True
    with pytest.raises(Exception, match="待审"):
        decide_draft(store, refund["id"], "approve")
    rejected = decide_draft(store, refund["id"], "reject", "去卖家中心处理")
    assert rejected["status"] == "rejected"


def test_http_review_allows_approve_and_refuses_publish_ship_refund(tmp_path):
    _store, _provider, client = _stack(tmp_path)
    generated = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "video_script", "items": [{"name": "真空袋", "specs": "带单向阀"}]},
    )
    assert generated.status_code == 200
    draft_id = generated.json()["drafts"][0]["id"]
    approved = client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "可以拍"})
    assert approved.status_code == 200
    assert approved.json()["published"] is False
    # 非 listing：固定 403。listing 未审批：409。默认离线平台审批后也因写通道拒绝而 409。
    assert client.post(f"/api/workflow/drafts/{draft_id}/publish").status_code == 403
    listing = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "抽屉分隔盒", "specs": "外径 20cm，材质 PP"}]},
    )
    listing_id = listing.json()["drafts"][0]["id"]
    assert client.post(f"/api/workflow/drafts/{listing_id}/publish").status_code == 409
    assert client.post(f"/api/workflow/drafts/{listing_id}/approve", json={"note": "ok"}).status_code == 200
    assert client.post(f"/api/workflow/drafts/{listing_id}/publish").status_code == 409
    assert client.post("/api/workflow/orders/1/ship").status_code == 403
    assert client.post("/api/workflow/orders/1/refund").status_code == 403
    blocked = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "防水盒", "specs": "100%防水"}]},
    )
    bad_id = blocked.json()["drafts"][0]["id"]
    assert client.post(f"/api/workflow/drafts/{bad_id}/approve", json={}).status_code == 409


def test_second_approve_does_not_overwrite_and_http_conflicts(tmp_path):
    store, _provider, client = _stack(tmp_path)
    created = generate_drafts(store, MockChatProvider(), "listing", [{"name": "抽屉分隔盒", "specs": "外径 20cm，材质 PP"}])
    draft_id = created[0]["id"]
    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "第一次"}).status_code == 200
    again = client.post(f"/api/workflow/drafts/{draft_id}/reject", json={"note": "想改成退回"})
    assert again.status_code == 409
    kept = store.get_draft(draft_id)
    assert kept["status"] == "approved"
    assert kept["decision_note"] == "第一次"
    assert store.decide(draft_id, "rejected", "直接再写一次") is None
    assert store.get_draft(draft_id)["status"] == "approved"


def test_concurrent_decide_lets_only_one_writer_win(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    draft = store.add_draft(
        kind="listing",
        status="pending",
        title="并发",
        body_th="กล่อง",
        body_en="box",
        payload={},
        qa={"ok": True, "issues": []},
        source="mock",
        degraded=False,
    )
    barrier = threading.Barrier(2)
    outcomes: list = []

    def once() -> None:
        barrier.wait()
        outcomes.append(store.decide(draft["id"], "approved", "并发"))

    threads = [threading.Thread(target=once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([item for item in outcomes if item and item["status"] == "approved"]) == 1
    assert len([item for item in outcomes if item is None]) == 1
    assert store.get_draft(draft["id"])["status"] == "approved"


def test_reject_from_needs_revision_and_outbound_redaction(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    seen: dict[str, str] = {}

    class RecordingProvider(ChatProvider):
        name = "openai"
        degraded = False

        def complete(self, system: str, user: str):
            seen["user"] = user
            from shopkeeper.models import AdapterResult

            return AdapterResult.success('{"title_th":"กล่อง","title_en":"box","body_th":"เนื้อหา","body_en":"body"}', self.name)

    created = generate_drafts(
        store,
        RecordingProvider(),
        "customer_reply",
        [{"name": "抽屉分隔盒", "message": "请打 13812345678，一件代发", "specs": "尺寸 20cm"}],
    )[0]
    assert "13812345678" not in seen["user"]
    assert "抽屉分隔盒" in seen["user"]
    assert created["status"] == "needs_revision"
    rejected = decide_draft(store, created["id"], "reject", "改完再来")
    assert rejected["status"] == "rejected"
