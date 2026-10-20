"""阶段 3：草稿 → 审批 → mock create_listing；未审批不调用；只读订单同步。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.serve import create_runtime_app
from shopkeeper.workflow.store import WorkflowStore


def _review_client(tmp_path: Path, platform: MockPlatform) -> tuple[WorkflowStore, MockPlatform, TestClient]:
    store = WorkflowStore(tmp_path / "workflow.db")
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform)
    return store, platform, TestClient(app)


def test_publish_requires_approval_and_records_platform_product_id(tmp_path):
    platform = MockPlatform()
    _store, platform, client = _review_client(tmp_path, platform)

    pending_pub = client.post("/api/workflow/drafts/missing/publish")
    assert pending_pub.status_code == 404

    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "抽屉分隔盒", "specs": "外径 20cm，材质 PP"}]},
    )
    draft = created.json()["drafts"][0]
    assert draft["status"] == "pending"

    before = client.post(f"/api/workflow/drafts/{draft['id']}/publish")
    assert before.status_code == 409
    assert "未审批" in before.json()["detail"]
    assert platform.create_listing_calls == []

    rejected = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "另一盒", "specs": "外径 18cm，材质 PP"}]},
    )
    other = rejected.json()["drafts"][0]
    client.post(f"/api/workflow/drafts/{other['id']}/reject", json={"note": "不要"})
    deny = client.post(f"/api/workflow/drafts/{other['id']}/publish")
    assert deny.status_code == 409
    assert "拒绝" in deny.json()["detail"]
    assert platform.create_listing_calls == []

    approved = client.post(f"/api/workflow/drafts/{draft['id']}/approve", json={"note": "核对过尺寸"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    published = client.post(f"/api/workflow/drafts/{draft['id']}/publish")
    assert published.status_code == 200
    body = published.json()
    assert body["status"] == "published"
    assert body["platform_product_id"]
    assert body["platform_product_id"].startswith("mock-pp-")
    assert len(platform.create_listing_calls) == 1
    assert platform.create_listing_calls[0]["draft_id"] == draft["id"]
    assert platform.create_listing_calls[0]["title_th"]

    again = client.post(f"/api/workflow/drafts/{draft['id']}/publish")
    assert again.status_code == 200
    assert again.json()["platform_product_id"] == body["platform_product_id"]
    assert len(platform.create_listing_calls) == 1

    ship = client.post("/api/workflow/orders/1/ship")
    refund = client.post("/api/workflow/orders/1/refund")
    assert ship.status_code == refund.status_code == 403
    assert platform.update_tracking_calls == []


def test_expired_approval_does_not_call_adapter(tmp_path):
    platform = MockPlatform()
    store, platform, client = _review_client(tmp_path, platform)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "过期盒", "specs": "外径 10cm，材质 PP"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "ok"}).status_code == 200
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with store._connect() as conn:
        conn.execute("UPDATE drafts SET decided_at = ? WHERE id = ?", (old, draft_id))
    expired = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert expired.status_code == 409
    assert "过期" in expired.json()["detail"]
    assert platform.create_listing_calls == []
    assert store.get_draft(draft_id)["status"] == "expired"


def test_readonly_order_sync_with_mock(tmp_path):
    platform = MockPlatform(
        orders=[
            {
                "order_id": "ORD-1",
                "status": "AWAITING_SHIPMENT",
                "item_names": ["抽屉分隔盒"],
                "total_amount": "199",
                "currency": "THB",
            }
        ]
    )
    store, platform, client = _review_client(tmp_path, platform)
    synced = client.post("/api/workflow/orders/sync", json={"since": ""})
    assert synced.status_code == 200
    body = synced.json()
    assert body["synced"] == 1
    assert body["orders"][0]["order_id"] == "ORD-1"
    assert "只读" in body["note"]
    assert platform.get_orders_calls == [""]
    listed = client.get("/api/workflow/orders/synced")
    assert listed.status_code == 200
    assert listed.json()["orders"][0]["order_id"] == "ORD-1"
    assert store.list_synced_orders()[0]["status"] == "AWAITING_SHIPMENT"
    assert client.post("/api/workflow/orders/ORD-1/ship").status_code == 403
    assert platform.update_tracking_calls == []


def test_dry_run_platform_does_not_claim_platform_product_id(tmp_path):
    from shopkeeper.adapters.tiktok_shop import TikTokDryRunPlatform, OfflineTikTokPlatform

    platform = TikTokDryRunPlatform(OfflineTikTokPlatform())
    _store, platform, client = _review_client(tmp_path, platform)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "干跑盒", "specs": "外径 12cm，材质 PP"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "ok"}).status_code == 200
    result = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "dry_run"
    assert body["dry_run"] is True
    assert body["published"] is False
    assert body["platform_product_id"] is None
    assert len(platform.create_listing_calls) == 1


def test_runtime_mock_platform_e2e(tmp_path, monkeypatch):
    from tests.workflow.env_isolation import isolate_runtime_env

    isolate_runtime_env(monkeypatch, tmp_path / "rt", platform="mock")
    client = TestClient(create_runtime_app(start_scheduler=False))
    status = client.get("/api/workflow/status").json()
    assert status["publish_enabled"] is True
    assert status["platform"] == "mock_platform"
    assert status["ship_enabled"] is False

    page = client.get("/workflow")
    assert page.status_code == 200
    assert "审批收件箱" in page.text
    assert "请求上架" in page.text

    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "抽屉分隔盒", "specs": "外径 20cm，材质 PP"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    assert client.post(f"/api/workflow/drafts/{draft_id}/publish").status_code == 409
    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "ok"}).status_code == 200
    published = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert published.status_code == 200
    assert published.json()["platform_product_id"].startswith("mock-pp-")
