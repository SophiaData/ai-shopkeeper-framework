"""八态审批闸门：适配现有 draft API + approvals 端点 + 飞书回调骨架。"""

from __future__ import annotations

import json
import time
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.workflow.approvals import evaluate_gate_policy
from shopkeeper.workflow.feishu import compute_signature
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore


def _client(tmp_path: Path, platform: MockPlatform | None = None) -> tuple[WorkflowStore, MockPlatform, TestClient]:
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = platform or MockPlatform()
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform)
    return store, platform, TestClient(app)


def test_gate_policies_seeded(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf.db")
    policies = {p["action_type"]: p for p in store.list_gate_policies()}
    assert policies["create_listing"]["require_approval"] is True
    assert policies["update_stock"]["require_approval"] is False
    assert policies["update_price"]["threshold"]["price_delta_pct"] == 5
    auto = evaluate_gate_policy(policies["update_price"], action_type="update_price", context={"price_delta_pct": 3})
    assert auto["require_approval"] is False
    need = evaluate_gate_policy(policies["update_price"], action_type="update_price", context={"price_delta_pct": 9})
    assert need["require_approval"] is True


def test_unapproved_cannot_publish_approved_can(tmp_path: Path):
    store, platform, client = _client(tmp_path)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "收纳盒", "specs": "PP"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    assert created.json()["drafts"][0]["payload"].get("approval_id")
    approval_id = created.json()["drafts"][0]["payload"]["approval_id"]

    blocked = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert blocked.status_code == 409
    assert "未审批" in blocked.json()["detail"]
    assert platform.create_listing_calls == []
    assert store.get_approval(approval_id)["status"] == "pending"

    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "ok"}).status_code == 200
    assert store.get_approval(approval_id)["status"] == "approved"

    published = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["platform_product_id"].startswith("mock-pp-")
    assert store.get_approval(approval_id)["status"] == "executed"
    assert store.get_draft(draft_id)["status"] == "published"
    assert len(platform.create_listing_calls) == 1

    assert client.post("/api/workflow/orders/1/ship").status_code == 403
    assert client.post("/api/workflow/orders/1/refund").status_code == 403


def test_approvals_rest_endpoints(tmp_path: Path):
    store, _platform, client = _client(tmp_path)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "盒", "specs": "x"}]},
    )
    approval_id = created.json()["drafts"][0]["payload"]["approval_id"]
    listed = client.get("/api/workflow/approvals")
    assert listed.status_code == 200
    assert any(a["id"] == approval_id for a in listed.json()["approvals"])
    assert any(p["action_type"] == "create_listing" for p in listed.json()["policies"])

    ok = client.post(f"/approvals/{approval_id}/approve", json={"note": "console"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved"
    draft_id = created.json()["drafts"][0]["id"]
    assert store.get_draft(draft_id)["status"] == "approved"


def test_feishu_callback_signature_and_approve(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "test-encrypt-key")
    monkeypatch.delenv("FEISHU_CALLBACK_ALLOW_INSECURE", raising=False)
    store, _platform, client = _client(tmp_path)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "盒", "specs": "x"}]},
    )
    approval_id = created.json()["drafts"][0]["payload"]["approval_id"]
    draft_id = created.json()["drafts"][0]["id"]

    # challenge
    challenge_body = json.dumps({"type": "url_verification", "challenge": "c-1", "token": ""}).encode()
    ts, nonce = str(int(time.time())), "n1"
    sig = compute_signature(ts, nonce, "test-encrypt-key", challenge_body)
    ch = client.post(
        "/feishu/approval-callback",
        content=challenge_body,
        headers={
            "content-type": "application/json",
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": sig,
        },
    )
    assert ch.status_code == 200
    assert ch.json()["challenge"] == "c-1"

    # bad signature
    bad = client.post(
        "/feishu/approval-callback",
        content=challenge_body,
        headers={
            "content-type": "application/json",
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": "deadbeef",
        },
    )
    assert bad.status_code == 401

    # mock approve event
    event_body = json.dumps(
        {"event_type": "approval.approve", "approval_id": approval_id, "note": "feishu-ok"}
    ).encode()
    sig2 = compute_signature(ts, nonce, "test-encrypt-key", event_body)
    ev = client.post(
        "/feishu/approval-callback",
        content=event_body,
        headers={
            "content-type": "application/json",
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": sig2,
        },
    )
    assert ev.status_code == 200, ev.text
    assert ev.json()["handled"] is True
    assert store.get_approval(approval_id)["status"] == "approved"
    assert store.get_draft(draft_id)["status"] == "approved"
    # 环境无真写默认
    assert os.environ.get("TTS_WRITE_ENABLED", "") not in {"1", "true", "True"}
