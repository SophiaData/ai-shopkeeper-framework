"""物流回填 stub：未审批拒绝、非法单号拒绝、审批后 mock 成功、live TTS_WRITE_BLOCKED。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.alibaba_dropship import MockAlibabaDropship
from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.adapters.tiktok_shop import OfflineTikTokPlatform
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore
from shopkeeper.workflow.tracking import validate_tracking


ORDERS = [
    {
        "order_id": "ORD-TRK-1",
        "status": "AWAITING_SHIPMENT",
        "currency": "THB",
        "total_amount": "199",
        "items": [{"sku": "BOX-S", "qty": 1, "unit_cost_cents": 1200}],
    }
]


def _client(
    tmp_path: Path,
    *,
    platform: MockPlatform | OfflineTikTokPlatform | None = None,
) -> tuple[TestClient, WorkflowStore, MockPlatform | OfflineTikTokPlatform, MockAlibabaDropship]:
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = platform or MockPlatform(orders=list(ORDERS))
    if isinstance(platform, MockPlatform) and not platform._orders:
        platform._orders = list(ORDERS)
    fulfillment = MockAlibabaDropship()
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=platform,
        fulfillment=fulfillment,
    )
    return TestClient(app), store, platform, fulfillment


def _dropship_executed(client: TestClient) -> dict:
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    drafted = client.post("/api/workflow/orders/ORD-TRK-1/dropship/draft")
    assert drafted.status_code == 200, drafted.text
    approval_id = drafted.json()["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ds"}).status_code == 200
    executed = client.post("/api/workflow/orders/ORD-TRK-1/dropship/execute")
    assert executed.status_code == 200, executed.text
    return executed.json()


def test_validate_tracking_stub():
    ok = validate_tracking("JNT", "JT123456789012")
    assert ok["ok"] is True
    assert ok["carrier"] == "JNT"
    bad_carrier = validate_tracking("UNKNOWN", "JT123456789012")
    assert bad_carrier["ok"] is False
    assert bad_carrier["code"] == "TRACKING_INVALID"
    bad_no = validate_tracking("JNT", "BAD")
    assert bad_no["ok"] is False
    assert bad_no["code"] == "TRACKING_INVALID"


def test_tracking_rejects_unapproved(tmp_path: Path):
    client, store, platform, _f = _client(tmp_path)
    ds = _dropship_executed(client)
    assert ds["adapter"]["tracking_no"]
    assert ds["adapter"]["carrier"] == "JNT"

    drafted = client.post("/api/workflow/orders/ORD-TRK-1/tracking/draft")
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["draft"]["status"] == "pending_approval"
    assert body["approval"]["action_type"] == "update_tracking"
    assert body["approval"]["status"] == "pending"
    assert body["draft"]["payload"]["tracking_no"] == ds["adapter"]["tracking_no"]

    blocked = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert blocked.status_code == 409
    assert "未审批" in blocked.json()["detail"]
    assert platform.update_tracking_calls == []
    assert store.get_shipment_for_order("ORD-TRK-1") is None
    assert client.post("/api/workflow/orders/ORD-TRK-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-TRK-1/refund").status_code == 403


def test_tracking_rejects_invalid_number(tmp_path: Path):
    client, store, platform, _f = _client(tmp_path)
    _dropship_executed(client)

    drafted = client.post(
        "/api/workflow/orders/ORD-TRK-1/tracking/draft",
        json={"carrier": "JNT", "tracking_no": "NOT-A-VALID-JT"},
    )
    assert drafted.status_code == 200, drafted.text
    approval_id = drafted.json()["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "bad"}).status_code == 200

    rejected = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert rejected.status_code == 409
    assert "TRACKING_INVALID" in rejected.json()["detail"]
    assert platform.update_tracking_calls == []
    assert store.get_shipment_for_order("ORD-TRK-1") is None
    draft = store.get_tracking_draft("ORD-TRK-1")
    assert draft is not None
    assert draft["status"] == "validation_failed"


def test_tracking_push_success_mock(tmp_path: Path):
    client, store, platform, _f = _client(tmp_path)
    assert isinstance(platform, MockPlatform)
    ds = _dropship_executed(client)
    tracking_no = ds["adapter"]["tracking_no"]

    drafted = client.post("/api/workflow/orders/ORD-TRK-1/tracking/draft")
    assert drafted.status_code == 200
    approval_id = drafted.json()["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200

    pushed = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert pushed.status_code == 200, pushed.text
    out = pushed.json()
    assert out["draft"]["status"] == "executed"
    assert out["shipment"]["order_id"] == "ORD-TRK-1"
    assert out["shipment"]["carrier"] == "JNT"
    assert out["shipment"]["tracking_no"] == tracking_no
    assert out["shipment"]["tracking_validated"] is True
    assert out["validation"]["format_ok"] is True
    assert out["validation"]["lookup_status"] == "skipped"
    assert out["validation"]["lookup_confirmed"] is False
    assert out["validation"]["queried"] is False
    assert "不能把格式通过当成承运方已确认" in out["note"]
    assert "付款仍关闭" in out["note"]
    assert out["adapter"]["accepted"] is True
    assert len(platform.update_tracking_calls) == 1
    assert platform.update_tracking_calls[0] == ("ORD-TRK-1", tracking_no)
    assert platform.tracking_by_order["ORD-TRK-1"] == tracking_no

    got = client.get("/api/workflow/orders/ORD-TRK-1/tracking")
    assert got.status_code == 200
    assert got.json()["approval"]["status"] == "executed"
    assert got.json()["shipment"]["tracking_no"] == tracking_no

    assert client.post("/api/workflow/orders/ORD-TRK-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-TRK-1/refund").status_code == 403


def test_tracking_live_tts_write_blocked(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf.db")
    platform = OfflineTikTokPlatform("offline")
    fulfillment = MockAlibabaDropship()
    # Offline 平台无订单；先用 mock 代发落库再换 live 测 push
    mock = MockPlatform(orders=list(ORDERS))
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=mock,
        fulfillment=fulfillment,
    )
    client = TestClient(app)
    ds = _dropship_executed(client)
    tracking_no = ds["adapter"]["tracking_no"]
    drafted = client.post("/api/workflow/orders/ORD-TRK-1/tracking/draft")
    approval_id = drafted.json()["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200

    # 换 OfflineTikTok（未放行 TTS_WRITE）再 push
    app2 = FastAPI()
    mount_review(
        app2,
        store=store,
        provider=MockChatProvider(),
        platform=platform,
        fulfillment=fulfillment,
    )
    live = TestClient(app2)
    failed = live.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert failed.status_code == 409
    assert "TTS_WRITE_BLOCKED" in failed.json()["detail"]
    assert store.get_shipment_for_order("ORD-TRK-1") is None
    draft = store.get_tracking_draft("ORD-TRK-1")
    assert draft is not None
    assert draft["status"] == "execute_failed"
    assert tracking_no  # 代发结果确有单号


def test_gate_policy_seeded_for_update_tracking(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf.db")
    policy = store.get_gate_policy("update_tracking")
    assert policy is not None
    assert policy["require_approval"] is True

class _RecordingPlatform(MockPlatform):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.delivery_kwargs = []

    def update_tracking(self, order_id, tracking_no, **kwargs):
        self.delivery_kwargs.append(dict(kwargs))
        return super().update_tracking(order_id, tracking_no)


def test_tracking_provider_is_frozen_in_approved_snapshot(tmp_path: Path, monkeypatch) -> None:
    platform = _RecordingPlatform(orders=list(ORDERS))
    client, store, platform, _f = _client(tmp_path, platform=platform)
    _dropship_executed(client)
    monkeypatch.setenv("TIKTOK_SHIPPING_PROVIDER_ID", "provider-env")
    drafted = client.post(
        "/api/workflow/orders/ORD-TRK-1/tracking/draft",
        json={"shipping_provider_id": "provider-approved"},
    )
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["approval"]["payload"]["shipping_provider_id"] == "provider-approved"
    approval_id = drafted.json()["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    monkeypatch.setenv("TIKTOK_SHIPPING_PROVIDER_ID", "provider-after-approval")
    pushed = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert pushed.status_code == 200, pushed.text
    assert platform.delivery_kwargs[-1]["shipping_provider_id"] == "provider-approved"
