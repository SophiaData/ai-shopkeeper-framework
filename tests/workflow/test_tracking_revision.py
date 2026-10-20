"""物流业务修订绑定新审批；跨连接修订与 claim 互斥。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.alibaba_dropship import ManualAlibabaDropship
from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.domain.approval_state import ApprovalEvent
from shopkeeper.workflow.approvals import ApprovalError, apply_approval_event, claim_for_execute, payload_hash, reap_stale_executing
from shopkeeper.workflow.drafts import DraftError
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.side_effect import make_idempotency_key
from shopkeeper.workflow.store import WorkflowStore
from shopkeeper.workflow.tracking import create_tracking_draft, push_tracking

OID = "ORD-TRACK-REV"
PO = "2026101800001234567"
A = "JT123456789012"
B = "JT123456789013"


@pytest.fixture(autouse=True)
def _offline_tracking(monkeypatch):
    monkeypatch.setenv("TRACKING_LOOKUP_ENABLED", "0")
    monkeypatch.setenv("TTS_WRITE_ENABLED", "0")


def _manual(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = MockPlatform(orders=[{
        "order_id": OID, "status": "AWAITING_SHIPMENT", "currency": "THB",
        "items": [{"sku": "BOX-S", "qty": 1, "unit_cost_cents": 1200, "offer_id": "600123456789"}],
    }])
    fulfillment = ManualAlibabaDropship()
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform, fulfillment=fulfillment)
    client = TestClient(app)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    draft = client.post(f"/api/workflow/orders/{OID}/dropship/draft").json()
    assert client.post(f"/approvals/{draft['approval']['id']}/approve").status_code == 200
    assert client.post(f"/api/workflow/orders/{OID}/dropship/execute").status_code == 200
    recorded = client.post(f"/api/workflow/orders/{OID}/dropship/record-po", json={"alibaba_order_id": PO})
    assert recorded.status_code == 200, recorded.text
    assert fulfillment.place_calls == []
    return client, store, platform


def _draft(client, number=A, carrier="JNT"):
    response = client.post(f"/api/workflow/orders/{OID}/tracking/draft", json={"carrier": carrier, "tracking_no": number})
    assert response.status_code == 200, response.text
    return response.json()


def _approve(client, body):
    response = client.post(f"/approvals/{body['approval']['id']}/approve", json={"note": "synthetic approval"})
    assert response.status_code == 200, response.text


def test_manual_po_changed_tracking_requires_new_approval_and_only_b_pushes(tmp_path):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    second = _draft(client, B)
    assert second["approval"]["id"] != first["approval"]["id"]
    assert second["approval"]["status"] == "pending"
    assert second["draft"]["status"] == "pending_approval"
    old = store.get_approval(first["approval"]["id"])
    assert old["status"] == "stale"
    assert old["payload"] == first["approval"]["payload"]
    assert old["payload_hash"] == first["approval"]["payload_hash"]
    assert client.post(f"/api/workflow/orders/{OID}/tracking/push").status_code == 409
    _approve(client, second)
    pushed = client.post(f"/api/workflow/orders/{OID}/tracking/push")
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["shipment"]["tracking_no"] == B
    assert client.post(f"/api/workflow/orders/{OID}/tracking/push").json()["idempotent_replay"] is True
    assert platform.update_tracking_calls == [(OID, B)]
    writes = [row for row in store.list_external_writes() if row["operation"] == "update_tracking"]
    assert len(writes) == 1
    assert writes[0]["request"]["tracking_no"] == B
    approval = store.get_approval(second["approval"]["id"])
    assert approval["payload"] == second["draft"]["payload"]
    assert payload_hash(approval["payload"]) == approval["payload_hash"]


def test_pending_change_opens_new_pending_without_mutating_original_hash(tmp_path):
    client, store, _ = _manual(tmp_path)
    first = _draft(client)
    second = _draft(client, B)
    assert second["approval"]["id"] != first["approval"]["id"]
    old = store.get_approval(first["approval"]["id"])
    assert old["status"] == "expired"
    assert old["payload"] == first["approval"]["payload"]
    assert old["payload_hash"] == payload_hash(old["payload"])
    assert second["approval"]["payload_hash"] == payload_hash(second["approval"]["payload"])


def test_normalized_same_snapshot_reuses_approval_and_ttl(tmp_path):
    client, store, _ = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    dropship = store.get_dropship_draft(OID)
    dropship["external_po_id"] = "  " + PO + "  "
    store.upsert_dropship_draft(dropship)
    same = _draft(client, "  " + A.lower() + "  ", " J&T Express ")
    assert same["approval"]["id"] == first["approval"]["id"]
    assert same["approval"]["status"] == "approved"
    assert same["approval"]["decide_before"] == first["approval"]["decide_before"]
    assert same["idempotent_replay"] is True
    assert same["draft"]["payload"]["external_po_id"] == PO


def test_external_po_change_is_a_business_revision(tmp_path):
    client, store, _ = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    dropship = store.get_dropship_draft(OID)
    dropship["external_po_id"] = PO[:-1] + "8"
    store.upsert_dropship_draft(dropship)
    second = _draft(client)
    assert second["approval"]["id"] != first["approval"]["id"]
    assert second["approval"]["status"] == "pending"
    assert second["approval"]["payload"]["external_po_id"] == PO[:-1] + "8"


def test_executing_and_executed_cannot_change_but_same_value_replays(tmp_path):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    claim_for_execute(store, first["approval"]["id"])
    assert _draft(client)["draft"]["status"] == "executing"
    assert client.post(f"/api/workflow/orders/{OID}/tracking/draft", json={"carrier": "JNT", "tracking_no": B}).status_code == 409
    assert client.post(f"/api/workflow/orders/{OID}/tracking/push").status_code == 409
    assert platform.update_tracking_calls == []
    apply_approval_event(store, first["approval"]["id"], ApprovalEvent.EXECUTE_OK)
    assert _draft(client)["approval"]["status"] == "executed"
    assert client.post(f"/api/workflow/orders/{OID}/tracking/draft", json={"carrier": "JNT", "tracking_no": B}).status_code == 409


def test_notification_metadata_does_not_change_business_hash_or_identity(tmp_path):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    original_hash = first["approval"]["payload_hash"]
    data = dict(first["approval"]["payload"], notification={"sent": True})
    updated = store.update_approval_payload(first["approval"]["id"], data)
    assert updated["payload_hash"] == original_hash
    assert _draft(client)["approval"]["id"] == first["approval"]["id"]
    _approve(client, first)
    assert client.post(f"/api/workflow/orders/{OID}/tracking/push").status_code == 200
    assert platform.update_tracking_calls == [(OID, A)]


@pytest.mark.parametrize("target", ["draft", "approval"])
def test_tampered_snapshot_never_pushes(tmp_path, target):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    if target == "draft":
        changed = dict(first["draft"])
        changed["payload"] = dict(changed["payload"], tracking_no=B)
        store.upsert_tracking_draft(changed)
    else:
        store.update_approval_payload(first["approval"]["id"], dict(first["approval"]["payload"], tracking_no=B))
    assert client.post(f"/api/workflow/orders/{OID}/tracking/push").status_code == 409
    assert platform.update_tracking_calls == []


def test_expired_approval_still_obeys_existing_gate(tmp_path):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    with store._connect() as conn:
        conn.execute("UPDATE approvals SET decide_before = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), first["approval"]["id"]))
    response = client.post(f"/api/workflow/orders/{OID}/tracking/push")
    assert response.status_code == 409
    assert "过期" in response.json()["detail"]
    assert platform.update_tracking_calls == []


def test_claim_wins_over_revision_across_store_connections(tmp_path, monkeypatch):
    client, store, _ = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    other = WorkflowStore(store.path)
    ready, release = Event(), Event()
    original = other.revise_tracking_draft

    def paused(*args, **kwargs):
        ready.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(other, "revise_tracking_draft", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(create_tracking_draft, other, OID, carrier="JNT", tracking_no=B)
        try:
            assert ready.wait(5)
            claim_for_execute(store, first["approval"]["id"])
        finally:
            release.set()
        with pytest.raises(DraftError):
            future.result(timeout=5)
    assert store.get_tracking_draft(OID)["payload"]["tracking_no"] == A
    assert store.get_tracking_draft(OID)["status"] == "executing"
    assert len([row for row in store.list_approvals() if row["action_type"] == "update_tracking"]) == 1


def test_revision_wins_over_old_claim_across_store_connections(tmp_path, monkeypatch):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    other = WorkflowStore(store.path)
    ready, release = Event(), Event()
    original = store.transition_approval

    def paused(*args, **kwargs):
        if kwargs.get("mark_executing"):
            ready.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "transition_approval", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(push_tracking, store, platform, OID, environ={})
        try:
            assert ready.wait(5)
            second = create_tracking_draft(other, OID, carrier="JNT", tracking_no=B)
        finally:
            release.set()
        with pytest.raises(DraftError):
            future.result(timeout=5)
    assert second["approval"]["status"] == "pending"
    assert platform.update_tracking_calls == []
    # 即使旧审批经合法重审恢复批准，也不能 claim 当前属于 B 的草稿。
    apply_approval_event(store, first["approval"]["id"], ApprovalEvent.RESUBMIT)
    apply_approval_event(store, first["approval"]["id"], ApprovalEvent.APPROVE)
    with pytest.raises(ApprovalError):
        claim_for_execute(store, first["approval"]["id"])


def test_late_validation_failure_cannot_overwrite_new_revision(tmp_path):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    other = WorkflowStore(store.path)

    class RevisionsDuringQuery:
        def query(self, carrier, tracking_no):
            create_tracking_draft(other, OID, carrier="JNT", tracking_no=B)
            return {"ok": False, "code": "TRACKING_NOT_FOUND", "message": "synthetic not found"}

    with pytest.raises(DraftError):
        push_tracking(store, platform, OID, lookup=RevisionsDuringQuery())
    current = store.get_tracking_draft(OID)
    assert current["payload"]["tracking_no"] == B
    assert current["status"] == "pending_approval"
    assert store.get_approval(current["approval_id"])["payload_hash"] == payload_hash(current["payload"])
    assert platform.update_tracking_calls == []


def test_legacy_snapshot_reuses_approval_and_completed_result(tmp_path):
    import json

    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    legacy = dict(first["draft"]["payload"], source="override")
    # 历史快照包含来源元数据，业务字段仍须与原签名一致。
    with store._connect() as conn:
        conn.execute("UPDATE approvals SET payload_json = ?, payload_hash = ? WHERE id = ?",
                     (json.dumps(legacy, ensure_ascii=False), payload_hash(legacy), first["approval"]["id"]))
        conn.execute("UPDATE tracking_drafts SET payload_json = ? WHERE order_id = ?",
                     (json.dumps(legacy, ensure_ascii=False), OID))
    _approve(client, first)
    same = _draft(client)
    assert same["approval"]["id"] == first["approval"]["id"]
    assert same["approval"]["status"] == "approved"
    pushed = client.post(f"/api/workflow/orders/{OID}/tracking/push")
    assert pushed.status_code == 200, pushed.text
    assert _draft(client)["approval"]["id"] == first["approval"]["id"]
    assert client.post(f"/api/workflow/orders/{OID}/tracking/push").json()["idempotent_replay"] is True
    assert platform.update_tracking_calls == [(OID, A)]


def test_reaper_releases_execution_projection_for_manual_reapproval(tmp_path):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    claim_for_execute(store, first["approval"]["id"])
    future = datetime.now(timezone.utc) + timedelta(seconds=1000)
    assert first["approval"]["id"] in reap_stale_executing(store, now=future, grace_seconds=900)
    assert store.get_tracking_draft(OID)["status"] == "execute_failed"
    replacement = _draft(client, B)
    assert replacement["approval"]["status"] == "pending"
    assert replacement["approval"]["id"] != first["approval"]["id"]
    assert platform.update_tracking_calls == []


def test_invalid_tracking_correction_requires_new_approval(tmp_path):
    client, store, platform = _manual(tmp_path)
    first = _draft(client, "BAD")
    _approve(client, first)
    assert client.post(f"/api/workflow/orders/{OID}/tracking/push").status_code == 409
    assert store.get_tracking_draft(OID)["status"] == "validation_failed"
    corrected = _draft(client, B)
    assert corrected["approval"]["status"] == "pending"
    assert corrected["approval"]["id"] != first["approval"]["id"]
    assert platform.update_tracking_calls == []


def test_late_platform_success_cannot_publish_superseded_shipment(tmp_path):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    other = WorkflowStore(store.path)
    replacement_platform = MockPlatform()
    original = platform.update_tracking

    def late_success(order_id, tracking_no, **kwargs):
        reap_stale_executing(other, now=datetime.now(timezone.utc) + timedelta(seconds=1000), grace_seconds=900)
        replacement = create_tracking_draft(other, OID, carrier="JNT", tracking_no=B)
        apply_approval_event(other, replacement["approval"]["id"], ApprovalEvent.APPROVE)
        push_tracking(other, replacement_platform, OID, environ={})
        return original(order_id, tracking_no, **kwargs)

    platform.update_tracking = late_success
    with pytest.raises(DraftError):
        push_tracking(store, platform, OID, environ={})
    assert store.get_tracking_draft(OID)["payload"]["tracking_no"] == B
    assert store.get_tracking_draft(OID)["status"] == "executed"
    assert store.get_shipment_for_order(OID)["tracking_no"] == B
    assert [row["tracking_no"] for row in store.list_shipments(OID)] == [B]
    assert len([row for row in store.list_external_writes() if row["operation"] == "update_tracking"]) == 2


def _preload_external_write(store, approval_id, *, number, status, response):
    snapshot = {"order_id": OID, "carrier": "JNT", "tracking_no": number, "external_po_id": PO}
    key = make_idempotency_key("mock_platform", "update_tracking", OID, number, approval_id)
    claimed = store.claim_external_write(
        action="update_tracking", draft_id=None, request=snapshot,
        idempotency_key=key, adapter="mock_platform", operation="update_tracking",
    )
    assert claimed is not None
    return store.complete_external_write(claimed["id"], status=status, response=response)


def test_confirmed_external_write_replay_finishes_executed_approval(tmp_path):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    _preload_external_write(
        store, first["approval"]["id"], number=A, status="confirmed",
        response={"order_id": OID, "tracking_no": A, "accepted": True, "dry_run": False},
    )
    pushed = client.post(f"/api/workflow/orders/{OID}/tracking/push")
    assert pushed.status_code == 200, pushed.text
    body = pushed.json()
    assert body["idempotent_replay"] is True
    assert body["shipment"]["tracking_no"] == A
    assert platform.update_tracking_calls == []
    assert store.get_approval(first["approval"]["id"])["status"] == "executed"
    assert store.get_tracking_draft(OID)["status"] == "executed"


def test_failed_external_write_replay_finishes_execute_failed(tmp_path):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    _preload_external_write(
        store, first["approval"]["id"], number=A, status="failed",
        response={"code": "TTS_WRITE_BLOCKED", "kind": "policy", "message": "synthetic blocked"},
    )
    pushed = client.post(f"/api/workflow/orders/{OID}/tracking/push")
    assert pushed.status_code == 409, pushed.text
    assert "TTS_WRITE_BLOCKED" in pushed.json()["detail"]
    assert platform.update_tracking_calls == []
    assert store.get_approval(first["approval"]["id"])["status"] == "execute_failed"
    assert store.get_tracking_draft(OID)["status"] == "execute_failed"


def test_pending_external_write_keeps_unknown_state_for_reaper(tmp_path):
    client, store, platform = _manual(tmp_path)
    first = _draft(client)
    _approve(client, first)
    snapshot = {"order_id": OID, "carrier": "JNT", "tracking_no": A, "external_po_id": PO}
    key = make_idempotency_key("mock_platform", "update_tracking", OID, A, first["approval"]["id"])
    assert store.claim_external_write(
        action="update_tracking", draft_id=None, request=snapshot,
        idempotency_key=key, adapter="mock_platform", operation="update_tracking",
    ) is not None
    pushed = client.post(f"/api/workflow/orders/{OID}/tracking/push")
    assert pushed.status_code == 409, pushed.text
    assert "结果未知" in pushed.json()["detail"]
    assert platform.update_tracking_calls == []
    assert store.get_approval(first["approval"]["id"])["status"] == "executing"
    assert store.get_tracking_draft(OID)["status"] == "executing"
    future = datetime.now(timezone.utc) + timedelta(seconds=1000)
    assert first["approval"]["id"] in reap_stale_executing(store, now=future, grace_seconds=900)
    assert store.get_approval(first["approval"]["id"])["status"] == "execute_failed"
    assert store.get_tracking_draft(OID)["status"] == "execute_failed"
