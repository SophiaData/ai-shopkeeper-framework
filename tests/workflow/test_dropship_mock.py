"""1688 代发 mock：草稿 → 八态审批 → execute；未审批 409；不付款；发货/退款仍 403。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.alibaba_dropship import MockAlibabaDropship
from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.models import AdapterResult
from shopkeeper.side_effect import make_idempotency_key
from shopkeeper.workflow.approvals import payload_hash
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore


ORDERS = [
    {
        "order_id": "ORD-DS-1",
        "status": "AWAITING_SHIPMENT",
        "currency": "THB",
        "total_amount": "199",
        "items": [
            {"sku": "BOX-S", "qty": 2, "unit_cost_cents": 1200},
            {"sku": "BOX-L", "quantity": 1},
        ],
    }
]


def _client(
    tmp_path: Path,
    *,
    fulfillment: MockAlibabaDropship | None = None,
) -> tuple[TestClient, WorkflowStore, MockAlibabaDropship]:
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = MockPlatform(orders=list(ORDERS))
    fulfillment = fulfillment or MockAlibabaDropship()
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=platform,
        fulfillment=fulfillment,
    )
    return TestClient(app), store, fulfillment


def test_dropship_draft_approve_execute_mock(tmp_path: Path):
    client, store, fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200

    drafted = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft")
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["draft"]["order_id"] == "ORD-DS-1"
    assert body["draft"]["status"] == "pending_approval"
    assert body["approval"]["action_type"] == "dropship_order"
    assert body["approval"]["status"] == "pending"
    assert body["draft"]["payload"]["supplier_id"] == "mock-supplier-overseas"
    assert body["draft"]["payload"]["warehouse_type"] == "overseas_warehouse"
    assert body["draft"]["payload"]["qty_total"] == 3
    assert body["draft"]["payload"]["paid"] is False
    assert body["draft"]["payload"]["auto_pay"] is False
    assert len(fulfillment.build_calls) == 1
    assert fulfillment.place_calls == []

    approval_id = body["approval"]["id"]
    blocked = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert blocked.status_code == 409
    assert "未审批" in blocked.json()["detail"]
    assert fulfillment.place_calls == []

    approved = client.post(f"/approvals/{approval_id}/approve", json={"note": "首单审"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    executed = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert executed.status_code == 200, executed.text
    out = executed.json()
    assert out["draft"]["status"] == "executed"
    assert out["draft"]["external_po_id"]
    assert out["paid"] is False
    assert out["adapter"]["paid"] is False
    assert out["adapter"]["dry_run"] is True
    assert out["adapter"]["payment_ref"] is None
    assert len(fulfillment.place_calls) == 1
    assert out["draft"]["external_po_id"] in fulfillment.placed

    got = client.get("/api/workflow/orders/ORD-DS-1/dropship")
    assert got.status_code == 200
    assert got.json()["draft"]["external_po_id"] == out["draft"]["external_po_id"]
    assert got.json()["approval"]["status"] == "executed"

    assert client.post("/api/workflow/orders/ORD-DS-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-DS-1/refund").status_code == 403


def test_dropship_execute_without_draft_404(tmp_path: Path):
    client, _store, fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    missing = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert missing.status_code == 404
    assert "草稿不存在" in missing.json()["detail"]
    assert fulfillment.place_calls == []


def test_dropship_draft_unknown_order_404(tmp_path: Path):
    client, _store, _f = _client(tmp_path)
    missing = client.post("/api/workflow/orders/NOPE/dropship/draft")
    assert missing.status_code == 404
    assert "订单不存在" in missing.json()["detail"]


def test_dropship_rejected_cannot_execute(tmp_path: Path):
    client, _store, fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    drafted = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft")
    approval_id = drafted.json()["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/reject", json={"note": "no"}).status_code == 200
    blocked = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert blocked.status_code == 409
    assert "拒绝" in blocked.json()["detail"]
    assert fulfillment.place_calls == []


def test_gate_policy_seeded_for_dropship(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf.db")
    policy = store.get_gate_policy("dropship_order")
    assert policy is not None
    assert policy["require_approval"] is True


def _preload_place_write(store, approval_id, *, status, response):
    key = make_idempotency_key(
        "mock_alibaba_dropship", "place_dropship", "ORD-DS-1", approval_id
    )
    claimed = store.claim_external_write(
        action="dropship_place", draft_id=None, request={"order_id": "ORD-DS-1"},
        idempotency_key=key, adapter="mock_alibaba_dropship", operation="place_dropship",
    )
    assert claimed is not None
    return store.complete_external_write(claimed["id"], status=status, response=response)


def test_confirmed_place_write_replay_finishes_executed_approval(tmp_path: Path):
    client, store, fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    body = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft").json()
    approval_id = body["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    _preload_place_write(
        store, approval_id, status="confirmed",
        response={
            "order_id": "ORD-DS-1", "external_po_id": "mock-po-replayed",
            "carrier": "JNT", "tracking_no": "JT123456789012",
            "paid": False, "dry_run": True,
        },
    )
    executed = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert executed.status_code == 200, executed.text
    assert executed.json()["idempotent_replay"] is True
    assert fulfillment.place_calls == []
    assert store.get_approval(approval_id)["status"] == "executed"
    assert store.get_dropship_draft("ORD-DS-1")["status"] == "executed"


def test_failed_place_write_replay_finishes_execute_failed(tmp_path: Path):
    client, store, fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    body = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft").json()
    approval_id = body["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    _preload_place_write(
        store, approval_id, status="failed",
        response={"code": "DROPSHIP_PLACE_FAILED", "kind": "execution", "message": "synthetic"},
    )
    executed = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert executed.status_code == 409, executed.text
    assert fulfillment.place_calls == []
    assert store.get_approval(approval_id)["status"] == "execute_failed"
    assert store.get_dropship_draft("ORD-DS-1")["status"] == "execute_failed"


def _changed_build(fulfillment, *, supplier_id="changed-supplier"):
    def build(order):
        body = dict(order or {})
        return AdapterResult.success(
            {
                "order_id": str(body.get("order_id") or ""),
                "supplier_id": supplier_id,
                "warehouse_type": "overseas_warehouse",
                "items": [{"platform_sku": "CHANGED", "qty": 1, "unit_cost_cents": 999}],
                "qty_total": 1,
                "currency": "THB",
                "external_request_id": f"changed-{body.get('order_id')}",
                "external_po_id": None,
                "paid": False,
                "auto_pay": False,
            },
            getattr(fulfillment, "name", "mock_alibaba_dropship"),
        )

    return build


def test_approved_dropship_payload_cannot_be_replaced(tmp_path: Path):
    client, store, fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    body = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft").json()
    approval_id = body["approval"]["id"]
    original_payload = body["draft"]["payload"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200

    fulfillment.build_dropship_draft = _changed_build(fulfillment)
    blocked = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft")
    assert blocked.status_code == 409, blocked.text
    assert "冻结" in blocked.json()["detail"]
    approval = store.get_approval(approval_id)
    assert approval["status"] == "approved"
    assert approval["payload"] == original_payload
    assert payload_hash(approval["payload"]) == approval["payload_hash"]
    assert store.get_dropship_draft("ORD-DS-1")["payload"] == original_payload

    executed = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert executed.status_code == 200, executed.text
    assert fulfillment.place_calls[-1]["supplier_id"] == original_payload["supplier_id"]


def test_tampered_draft_cannot_execute_under_old_approval(tmp_path: Path):
    client, store, fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    body = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft").json()
    approval_id = body["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    tampered = dict(body["draft"])
    tampered["payload"] = dict(tampered["payload"], supplier_id="tampered-supplier")
    store.upsert_dropship_draft(tampered)

    blocked = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert blocked.status_code == 409, blocked.text
    assert "不一致" in blocked.json()["detail"]
    assert fulfillment.place_calls == []
    assert store.get_approval(approval_id)["status"] == "approved"


def test_pending_dropship_refresh_updates_payload_and_hash(tmp_path: Path):
    client, store, fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    body = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft").json()
    approval_id = body["approval"]["id"]
    fulfillment.build_dropship_draft = _changed_build(fulfillment)
    refreshed = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["approval"]["id"] == approval_id
    approval = store.get_approval(approval_id)
    assert approval["payload"]["supplier_id"] == "changed-supplier"
    assert payload_hash(approval["payload"]) == approval["payload_hash"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    executed = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert executed.status_code == 200, executed.text
    assert fulfillment.place_calls[-1]["supplier_id"] == "changed-supplier"


def test_pending_snapshot_cas_rejects_stale_writer(tmp_path: Path):
    """pending 刷新必须是真 CAS：拿旧 hash 的第二个写者不能覆盖先到者。"""
    client, store, _fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    body = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft").json()
    approval_id = body["approval"]["id"]
    original = store.get_approval(approval_id)
    other = WorkflowStore(store.path)

    winner_payload = dict(original["payload"], supplier_id="winner-supplier")
    winner = other.update_pending_approval_snapshot(
        approval_id, winner_payload, hash_value=payload_hash(winner_payload),
        expected_hash=original["payload_hash"],
    )
    assert winner is not None

    stale_payload = dict(original["payload"], supplier_id="stale-supplier")
    stale = store.update_pending_approval_snapshot(
        approval_id, stale_payload, hash_value=payload_hash(stale_payload),
        expected_hash=original["payload_hash"],
    )
    assert stale is None
    current = store.get_approval(approval_id)
    assert current["payload"]["supplier_id"] == "winner-supplier"
    assert payload_hash(current["payload"]) == current["payload_hash"]


def test_executed_dropship_replay_revalidates_snapshot(tmp_path: Path):
    """即使 draft 已 executed，重复 execute 也必须先核对批准快照，不能直接回放脏草稿。"""
    client, store, fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    body = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft").json()
    approval_id = body["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    first = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert first.status_code == 200, first.text
    assert fulfillment.place_calls

    tampered = store.get_dropship_draft("ORD-DS-1")
    assert tampered["status"] == "executed"
    tampered["payload"] = dict(tampered["payload"], supplier_id="tampered-after-execute")
    store.upsert_dropship_draft(tampered)

    blocked = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert blocked.status_code == 409, blocked.text
    assert "不一致" in blocked.json()["detail"]
    assert len(fulfillment.place_calls) == 1

def test_unpaid_or_cancelled_order_cannot_create_dropship_draft(tmp_path: Path):
    client, store, fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    for status in ("UNPAID", "CANCELLED"):
        store.upsert_synced_order(
            {"order_id": "ORD-DS-1", "status": status, "items": ORDERS[0]["items"]},
            source="fixture", shop_id="default",
        )
        rejected = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft")
        assert rejected.status_code == 409, (status, rejected.text)
        assert "不可代发" in rejected.json()["detail"]
    assert fulfillment.build_calls == []


def test_cancelled_after_approval_blocks_execute(tmp_path: Path):
    client, store, fulfillment = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    drafted = client.post("/api/workflow/orders/ORD-DS-1/dropship/draft")
    assert drafted.status_code == 200
    approval_id = drafted.json()["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "先批"}).status_code == 200
    store.upsert_synced_order(
        {"order_id": "ORD-DS-1", "status": "CANCELLED", "items": ORDERS[0]["items"]},
        source="fixture", shop_id="default",
    )
    denied = client.post("/api/workflow/orders/ORD-DS-1/dropship/execute")
    assert denied.status_code == 409
    assert "执行前复核失败" in denied.json()["detail"]
    assert fulfillment.place_calls == []
