"""客服白名单自动回复 stub：免审 send、非白名单必审、退款永不免审、ship/refund 403。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.alibaba_dropship import MockAlibabaDropship
from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.workflow.cs import (
    DEFAULT_AUTO_REPLY_CATEGORIES,
    NEVER_AUTO_CATEGORIES,
    is_whitelist_category,
)
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore


ORDERS = [
    {
        "order_id": "ORD-CS-1",
        "status": "AWAITING_SHIPMENT",
        "currency": "THB",
        "total_amount": "199",
        "items": [{"sku": "BOX-S", "qty": 1, "unit_cost_cents": 1200}],
    }
]


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowStore, MockPlatform]:
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = MockPlatform(orders=list(ORDERS))
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=platform,
        fulfillment=MockAlibabaDropship(),
    )
    return TestClient(app), store, platform


def test_whitelist_helpers():
    assert "logistics_query" in DEFAULT_AUTO_REPLY_CATEGORIES
    assert is_whitelist_category("logistics_query")
    assert is_whitelist_category("tracking_status")
    assert is_whitelist_category("sla_reminder")
    for cat in NEVER_AUTO_CATEGORIES:
        assert not is_whitelist_category(cat)
    # 误配环境变量也不能让退款免审
    assert not is_whitelist_category("refund", {"CS_AUTO_REPLY_CATEGORIES": "refund,logistics_query"})


def test_whitelist_draft_send_without_approval(tmp_path: Path):
    client, store, platform = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200

    inbox = client.post(
        "/api/workflow/cs/inbox",
        json={
            "conversation_id": "conv-wl-1",
            "order_id": "ORD-CS-1",
            "category": "logistics_query",
            "body": "พัสดุถึงไหนแล้วคะ",
            "lang": "th",
        },
    )
    assert inbox.status_code == 200, inbox.text
    message_id = inbox.json()["message"]["id"]

    drafted = client.post(
        "/api/workflow/cs/draft",
        json={"message_id": message_id},
    )
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["whitelist"] is True
    assert body["approval"] is None
    assert body["draft"]["status"] == "ready"
    assert body["draft"]["whitelist"] is True
    assert "จัดส่ง" in body["draft"]["body_th"] or "พัสดุ" in body["draft"]["body_th"]
    draft_id = body["draft"]["id"]

    sent = client.post(f"/api/workflow/cs/drafts/{draft_id}/send")
    assert sent.status_code == 200, sent.text
    out = sent.json()
    assert out["sent_stub"] is True
    assert out["dry_run"] is True
    assert out["draft"]["status"] == "sent_stub"
    assert out["draft"]["sent_stub"] is True
    assert out["external_write"]["status"] == "dry_run"
    assert out["external_write"]["action"] == "send_message"
    assert out["draft"]["result"]["platform_called"] is False
    assert out["draft"]["result"]["dry_run"] is True

    writes = store.list_external_writes(draft_id)
    assert len(writes) >= 1
    assert writes[0]["action"] == "send_message"
    assert writes[0]["status"] == "dry_run"

    listed = client.get("/api/workflow/cs/drafts")
    assert listed.status_code == 200
    assert listed.json()["count"] >= 1


def test_non_whitelist_requires_approval(tmp_path: Path):
    client, store, _platform = _client(tmp_path)

    drafted = client.post(
        "/api/workflow/cs/draft",
        json={
            "conversation_id": "conv-refund-1",
            "category": "complaint",
            "body": "สินค้าเสียหาย",
            "lang": "th",
        },
    )
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["whitelist"] is False
    assert body["draft"]["status"] == "pending_approval"
    assert body["approval"]["action_type"] == "auto_reply"
    assert body["approval"]["status"] == "pending"
    draft_id = body["draft"]["id"]
    approval_id = body["approval"]["id"]

    blocked = client.post(f"/api/workflow/cs/drafts/{draft_id}/send")
    assert blocked.status_code == 409
    assert "未审批" in blocked.json()["detail"]
    draft = store.get_cs_draft(draft_id)
    assert draft is not None
    assert draft["sent_stub"] is False

    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    sent = client.post(f"/api/workflow/cs/drafts/{draft_id}/send")
    assert sent.status_code == 200, sent.text
    assert sent.json()["sent_stub"] is True
    assert sent.json()["draft"]["status"] == "sent_stub"
    assert store.get_approval(approval_id)["status"] == "executed"


def test_reject_blocks_send(tmp_path: Path):
    client, store, _platform = _client(tmp_path)
    drafted = client.post(
        "/api/workflow/cs/draft",
        json={
            "conversation_id": "conv-pm-1",
            "category": "price_match",
            "body": "ขอราคาเท่ากับร้านอื่น",
        },
    )
    assert drafted.status_code == 200
    draft_id = drafted.json()["draft"]["id"]
    approval_id = drafted.json()["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/reject", json={"note": "no"}).status_code == 200

    blocked = client.post(f"/api/workflow/cs/drafts/{draft_id}/send")
    assert blocked.status_code == 409
    assert "拒绝" in blocked.json()["detail"]
    assert store.get_cs_draft(draft_id)["sent_stub"] is False


def test_refund_never_auto_approved(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CS_AUTO_REPLY_CATEGORIES", "refund,logistics_query")
    client, store, _platform = _client(tmp_path)

    drafted = client.post(
        "/api/workflow/cs/draft",
        json={
            "conversation_id": "conv-refund-hard",
            "category": "refund",
            "body": "ขอคืนเงิน",
            "order_id": "ORD-CS-1",
        },
    )
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["whitelist"] is False
    assert body["approval"] is not None
    assert body["approval"]["action_type"] == "auto_reply"
    assert body["draft"]["status"] == "pending_approval"

    blocked = client.post(f"/api/workflow/cs/drafts/{body['draft']['id']}/send")
    assert blocked.status_code == 409
    assert "未审批" in blocked.json()["detail"]
    assert store.get_cs_draft(body["draft"]["id"])["sent_stub"] is False


def test_ship_refund_still_403(tmp_path: Path):
    client, _store, _platform = _client(tmp_path)
    assert client.post("/api/workflow/orders/ORD-CS-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-CS-1/refund").status_code == 403


def test_gate_policy_seeded_for_auto_reply(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf.db")
    policy = store.get_gate_policy("auto_reply")
    assert policy is not None
    assert policy["channel"] == "console"
    threshold = policy.get("threshold") or {}
    assert threshold.get("whitelist_only") is True


def test_cs_tables_migrated(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf.db")
    with store._connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "cs_messages" in tables
    assert "cs_drafts" in tables


def test_workflow_page_mentions_cs(tmp_path: Path, monkeypatch):
    from shopkeeper.workflow.routes import mount_workflow
    from shopkeeper.workflow.service import WorkflowService
    from tests.workflow.env_isolation import isolate_runtime_env

    isolate_runtime_env(monkeypatch, tmp_path / "rt", platform="mock")
    app = FastAPI()
    mount_workflow(
        app,
        WorkflowService.from_env(
            {
                "WORKFLOW_DB_PATH": str(tmp_path / "rt" / "workflow.db"),
                "WORKFLOW_LLM_PROVIDER": "mock",
                "WORKFLOW_PLATFORM": "mock",
            }
        ),
    )
    page = TestClient(app).get("/workflow")
    assert page.status_code == 200
    assert "客服话术" in page.text
    assert "/api/workflow/cs/drafts" in page.text
