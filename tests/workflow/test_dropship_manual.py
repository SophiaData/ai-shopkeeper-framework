"""P2-02：1688 人工下单。可复制链接与正文；回填单号；不付款、不发货、不退款。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.alibaba_dropship import (
    ManualAlibabaDropship,
    MockAlibabaDropship,
    build_fulfillment,
    parse_alibaba_order_id,
)
from shopkeeper.adapters.mock import MockPlatform
from datetime import datetime, timedelta, timezone

from shopkeeper.workflow.admin_routes import build_settings_view
from shopkeeper.workflow.approvals import claim_for_execute, reap_stale_executing
from shopkeeper.workflow.dropship import execute_dropship
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore


PHONE = "0812345678"
ADDRESS = "123 Sukhumvit Bangkok"
PO = "2026101600001234567"
PO_OTHER = "2026101600001234568"
OFFER = "600123456789"
OFFER_URL = f"https://detail.1688.com/offer/{OFFER}.html"

ORDERS = [
    {
        "order_id": "ORD-MAN-1",
        "status": "AWAITING_SHIPMENT",
        "currency": "THB",
        "supplier_name": "",
        "items": [
            {
                "sku": "BOX-S",
                "qty": 2,
                "unit_cost_cents": 1200,
                "cost_currency": "CNY",
                "offer_id": OFFER,
                "spec": "大号",
            },
            {
                "sku": "BOX-L",
                "quantity": 1,
                "url": "https://detail.1688.com/offer/700123456789.html?spm=fake",
            },
        ],
        "recipient_address": {
            "name": "Jane",
            "phone": PHONE,
            "full_address": ADDRESS,
        },
    }
]


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowStore, ManualAlibabaDropship]:
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = MockPlatform(orders=list(ORDERS))
    fulfillment = ManualAlibabaDropship()
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=platform,
        fulfillment=fulfillment,
    )
    return TestClient(app), store, fulfillment


def _sync_and_draft(client: TestClient) -> dict:
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    drafted = client.post("/api/workflow/orders/ORD-MAN-1/dropship/draft")
    assert drafted.status_code == 200, drafted.text
    return drafted.json()


def _payload_blob(store: WorkflowStore) -> str:
    conn = sqlite3.connect(store.path)
    try:
        drafts = " ".join(
            row[0] or ""
            for row in conn.execute("SELECT payload_json, result_json FROM dropship_drafts")
        )
        approvals = " ".join(
            row[0] or "" for row in conn.execute("SELECT payload_json FROM approvals")
        )
    finally:
        conn.close()
    return drafts + " " + approvals


def test_parse_order_id_rejects_payment_links():
    assert parse_alibaba_order_id(f"  {PO}  ") == PO
    assert parse_alibaba_order_id(
        f"https://trade.1688.com/order/new_step_order_detail.htm?orderId={PO}"
    ) == PO
    assert parse_alibaba_order_id("12345") is None
    assert parse_alibaba_order_id(PO[:15]) is None
    assert parse_alibaba_order_id(f"https://openapi.alipay.com/gateway.do?orderId={PO}") is None
    assert parse_alibaba_order_id(f"https://evil.example/order?orderId={PO}") is None


def test_unrecognized_mode_cannot_pay():
    assert isinstance(build_fulfillment({}), MockAlibabaDropship)
    assert isinstance(build_fulfillment({"ALIBABA_DROPSHIP_MODE": "manual"}), ManualAlibabaDropship)
    assert isinstance(build_fulfillment({"ALIBABA_DROPSHIP_MODE": "Manual"}), ManualAlibabaDropship)
    for raw in ("pay", "live", "1", "fast"):
        adapter = build_fulfillment({"ALIBABA_DROPSHIP_MODE": raw})
        assert isinstance(adapter, MockAlibabaDropship)
        assert adapter.manual_only is False


def test_place_dropship_order_always_refuses():
    adapter = ManualAlibabaDropship()
    refused = adapter.place_dropship_order(
        {"order_id": "ORD-MAN-1", "paid": True, "auto_pay": True, "payment_url": "https://pay.example/checkout"}
    )
    assert refused.ok is False
    assert refused.data is None
    assert refused.error is not None
    assert refused.error.code == "DROPSHIP_MANUAL_NO_AUTOPAY"
    assert len(adapter.place_calls) == 1


def test_source_has_no_pay_api_or_write_switch():
    root = Path(__file__).resolve().parents[2]
    blobs = [
        (root / "src/shopkeeper/adapters/alibaba_dropship.py").read_text(encoding="utf-8"),
        (root / "src/shopkeeper/workflow/dropship.py").read_text(encoding="utf-8"),
    ]
    for text in blobs:
        for needle in (
            "alibaba.alipay",
            "protocolPay",
            "fastCreateOrder",
            "preparePay",
            "import httpx",
            "TTS_WRITE_ENABLED=1",
            'TTS_WRITE_ENABLED = "1"',
        ):
            assert needle not in text


def test_manual_sheet_copy_links_and_no_stored_pii(tmp_path: Path):
    client, store, fulfillment = _client(tmp_path)
    body = _sync_and_draft(client)
    payload = body["draft"]["payload"]
    assert payload["mode"] == "manual"
    assert payload["paid"] is False
    assert payload["auto_pay"] is False
    assert payload["payment_url"] is None
    assert payload["called_alibaba"] is False
    assert payload["supplier_id"] == "待人工确认"
    assert payload["qty_total"] == 3
    assert payload["external_po_id"] is None
    copy = payload["manual_order"]["copy_text"]
    assert "不自动付款" in copy
    assert "BOX-S × 2" in copy
    assert "BOX-L × 1" in copy
    assert OFFER_URL in copy
    assert "https://detail.1688.com/offer/700123456789.html" in copy
    assert "spm=fake" not in copy
    assert "1200 分（CNY）" in copy
    assert "缺数据" in copy
    assert "成本：1000" not in copy
    assert payload["items"][1]["unit_cost_cents"] is None
    assert PHONE not in copy
    assert ADDRESS not in copy
    assert fulfillment.place_calls == []

    stored = _payload_blob(store)
    assert PHONE not in stored
    assert ADDRESS not in stored
    assert "Jane" not in stored

    preview = client.get("/api/workflow/orders/ORD-MAN-1/dropship/manual-sheet")
    assert preview.status_code == 200, preview.text
    sheet = preview.json()
    assert sheet["ready_to_order"] is False
    assert sheet["paid"] is False
    assert sheet["auto_pay"] is False
    assert sheet["payment_url"] is None
    assert "审批通过前不要" in sheet["note"]
    assert PHONE in sheet["copy_text"]
    assert ADDRESS in sheet["copy_text"]
    assert OFFER_URL in sheet["copy_text"]
    hosts = {link["url"].split("/")[2] for link in sheet["links"]}
    assert hosts <= {"detail.1688.com", "s.1688.com", "trade.1688.com", "m.1688.com", "air.1688.com"}
    blob = preview.text.lower()
    for banned in ("alipay", "cashier", "protocolpay", "preparepay", "fastcreateorder"):
        assert banned not in blob
    assert _payload_blob(store).count(PHONE) == 0


def test_missing_qty_does_not_guess(tmp_path: Path):
    _client_unused, _store, fulfillment = _client(tmp_path)
    del _client_unused, _store
    refused = fulfillment.build_dropship_draft(
        {"order_id": "ORD-QTY", "items": [{"sku": "BOX-S", "offer_id": OFFER}]}
    )
    assert refused.ok is False
    assert refused.error is not None
    assert refused.error.code == "DROPSHIP_QTY_REQUIRED"
    fractional = fulfillment.build_dropship_draft(
        {"order_id": "ORD-QTY", "items": [{"sku": "BOX-S", "qty": 2.5, "offer_id": OFFER}]}
    )
    assert fractional.ok is False
    assert fractional.error is not None
    assert fractional.error.code == "DROPSHIP_QTY_REQUIRED"
    empty = fulfillment.build_dropship_draft({"order_id": "ORD-QTY", "items": []})
    assert empty.ok is False
    assert empty.error is not None
    assert empty.error.code == "DROPSHIP_ITEMS_REQUIRED"


def test_record_requires_approval_then_keeps_unpaid(tmp_path: Path):
    client, store, fulfillment = _client(tmp_path)
    body = _sync_and_draft(client)
    approval_id = body["approval"]["id"]

    blocked = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": PO},
    )
    assert blocked.status_code == 409
    assert "未审批" in blocked.json()["detail"]
    assert fulfillment.place_calls == []

    early = client.post("/api/workflow/orders/ORD-MAN-1/dropship/execute")
    assert early.status_code == 409
    assert fulfillment.place_calls == []

    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "可下单"}).status_code == 200
    opened = client.post("/api/workflow/orders/ORD-MAN-1/dropship/execute")
    assert opened.status_code == 200, opened.text
    opened_body = opened.json()
    assert opened_body["draft"]["status"] == "awaiting_manual_po"
    assert opened_body["draft"]["external_po_id"] is None
    assert opened_body["paid"] is False
    assert opened_body["auto_pay"] is False
    assert opened_body["payment_url"] is None
    assert opened_body["called_alibaba"] is False
    assert opened_body["approval"]["status"] == "approved"
    assert "BOX-S × 2" in opened_body["manual_order"]["copy_text"]
    assert fulfillment.place_calls == []
    assert store.list_external_writes() == []

    ready = client.get("/api/workflow/orders/ORD-MAN-1/dropship/manual-sheet")
    assert ready.json()["ready_to_order"] is True

    pay_link = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": f"https://openapi.alipay.com/gateway.do?orderId={PO}"},
    )
    assert pay_link.status_code == 409
    assert "DROPSHIP_PO_INVALID" in pay_link.json()["detail"]
    assert "alipay" not in pay_link.text.lower()
    assert store.get_dropship_draft("ORD-MAN-1")["external_po_id"] is None

    extra = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": PO, "paid": True, "auto_pay": True},
    )
    assert extra.status_code == 422
    assert store.get_dropship_draft("ORD-MAN-1")["external_po_id"] is None

    before_writes = len(store.list_external_writes())
    recorded = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": f"https://trade.1688.com/order/new_step_order_detail.htm?orderId={PO}"},
    )
    assert recorded.status_code == 200, recorded.text
    out = recorded.json()
    assert out["paid"] is False
    assert out["auto_pay"] is False
    assert out["payment_url"] is None
    assert out["called_alibaba"] is False
    assert out["alibaba_order_id"] == PO
    assert out["draft"]["status"] == "executed"
    assert out["draft"]["external_po_id"] == PO
    assert out["approval"]["status"] == "executed"
    assert out["order_detail_url"] == (
        f"https://trade.1688.com/order/new_step_order_detail.htm?orderId={PO}"
    )
    assert "alipay" not in recorded.text.lower()
    assert out["idempotent_replay"] is False
    assert len(store.list_external_writes()) == before_writes
    assert fulfillment.place_calls == []
    result = out["draft"]["result"]
    assert result["tracking_no"] is None
    assert result["carrier"] is None
    assert result["paid"] is False

    again = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": PO},
    )
    assert again.status_code == 200, again.text
    assert again.json()["idempotent_replay"] is True
    assert again.json()["paid"] is False
    assert again.json()["alibaba_order_id"] == PO

    conflict = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": PO_OTHER},
    )
    assert conflict.status_code == 409
    assert "DROPSHIP_PO_CONFLICT" in conflict.json()["detail"]
    assert store.get_dropship_draft("ORD-MAN-1")["external_po_id"] == PO

    tracking = client.post("/api/workflow/orders/ORD-MAN-1/tracking/draft")
    assert tracking.status_code == 409
    assert client.post("/api/workflow/orders/ORD-MAN-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-MAN-1/refund").status_code == 403
    assert PHONE not in _payload_blob(store)


def test_rejected_cannot_record(tmp_path: Path):
    client, _store, fulfillment = _client(tmp_path)
    body = _sync_and_draft(client)
    approval_id = body["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/reject", json={"note": "no"}).status_code == 200
    blocked = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": PO},
    )
    assert blocked.status_code == 409
    assert "拒绝" in blocked.json()["detail"]
    assert fulfillment.place_calls == []


def test_mock_draft_is_not_a_real_po(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = MockPlatform(orders=list(ORDERS))
    mock = MockAlibabaDropship()
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=platform,
        fulfillment=mock,
    )
    client = TestClient(app)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    assert client.post("/api/workflow/orders/ORD-MAN-1/dropship/draft").status_code == 200
    sheet = client.get("/api/workflow/orders/ORD-MAN-1/dropship/manual-sheet")
    assert sheet.status_code == 409
    assert "DROPSHIP_NOT_MANUAL" in sheet.json()["detail"]
    recorded = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": PO},
    )
    assert recorded.status_code == 409
    assert "DROPSHIP_NOT_MANUAL" in recorded.json()["detail"]


def test_swapping_to_mock_adapter_does_not_invent_po(tmp_path: Path):
    client, store, _manual = _client(tmp_path)
    body = _sync_and_draft(client)
    approval_id = body["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    mock = MockAlibabaDropship()
    opened = execute_dropship(store, mock, "ORD-MAN-1")
    assert opened["draft"]["status"] == "awaiting_manual_po"
    assert opened["draft"]["external_po_id"] is None
    assert opened["paid"] is False
    assert mock.place_calls == []


def test_settings_show_dropship_unpaid(monkeypatch):
    class _Service:
        def status(self):
            return {"tiktok_mode": "mock", "publish_enabled": False, "tts_write_armed": False}

    monkeypatch.setenv("ALIBABA_DROPSHIP_MODE", "pay")
    view = build_settings_view(_Service())
    platform = next(group for group in view["groups"] if group["id"] == "platform")
    pay = next(item for item in platform["items"] if item["name"] == "付款")
    dropship = next(item for item in platform["items"] if item["name"] == "1688 代发")
    assert pay["value"].startswith("禁用")
    assert dropship["value"] == "mock（不付款）"
    monkeypatch.setenv("ALIBABA_DROPSHIP_MODE", "manual")
    again = build_settings_view(_Service())
    platform = next(group for group in again["groups"] if group["id"] == "platform")
    dropship = next(item for item in platform["items"] if item["name"] == "1688 代发")
    assert dropship["value"] == "人工下单（不付款）"


def test_reaper_releases_manual_dropship_draft_for_reapproval(tmp_path: Path):
    """record-po 在 claim 后崩溃：reaper 必须同时放回草稿，否则永远卡在 awaiting_manual_po。"""
    client, store, _manual = _client(tmp_path)
    body = _sync_and_draft(client)
    approval_id = body["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    opened = client.post("/api/workflow/orders/ORD-MAN-1/dropship/execute")
    assert opened.status_code == 200, opened.text
    assert store.get_dropship_draft("ORD-MAN-1")["status"] == "awaiting_manual_po"

    # 模拟 record-po 刚 claim 完进程就挂掉：审批 executing，草稿停在原状态。
    claim_for_execute(store, approval_id)
    assert store.get_approval(approval_id)["status"] == "executing"
    assert store.get_dropship_draft("ORD-MAN-1")["status"] == "executing"

    future = datetime.now(timezone.utc) + timedelta(seconds=1000)
    assert approval_id in reap_stale_executing(store, now=future, grace_seconds=900)
    assert store.get_approval(approval_id)["status"] == "execute_failed"
    assert store.get_dropship_draft("ORD-MAN-1")["status"] == "execute_failed"

    # 同单同值重开：必须换新审批身份；旧执行迟到时不能再写回新草稿。
    again = client.post("/api/workflow/orders/ORD-MAN-1/dropship/draft")
    assert again.status_code == 200, again.text
    new_approval_id = again.json()["approval"]["id"]
    assert new_approval_id != approval_id
    assert again.json()["approval"]["status"] == "pending"
    assert store.get_approval(approval_id)["status"] == "execute_failed"
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "旧单"}).status_code == 409
    assert client.post(f"/approvals/{new_approval_id}/approve", json={"note": "ok"}).status_code == 200
    recorded = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": PO},
    )
    assert recorded.status_code == 200, recorded.text
    assert store.get_dropship_draft("ORD-MAN-1")["status"] == "executed"
    assert _manual.place_calls == []


def test_record_po_rejects_tampered_draft_snapshot(tmp_path: Path):
    """manual 回填也必须核对草稿↔批准快照；不能拿旧批准回填被改写的内容。"""
    client, store, _manual = _client(tmp_path)
    body = _sync_and_draft(client)
    approval_id = body["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    opened = client.post("/api/workflow/orders/ORD-MAN-1/dropship/execute")
    assert opened.status_code == 200, opened.text
    assert store.get_dropship_draft("ORD-MAN-1")["status"] == "awaiting_manual_po"

    tampered = store.get_dropship_draft("ORD-MAN-1")
    tampered["payload"] = dict(tampered["payload"], supplier_id="tampered-supplier")
    store.upsert_dropship_draft(tampered)

    blocked = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": PO},
    )
    assert blocked.status_code == 409, blocked.text
    assert "不一致" in blocked.json()["detail"]
    assert store.get_dropship_draft("ORD-MAN-1")["external_po_id"] is None
    assert store.get_approval(approval_id)["status"] == "approved"


def test_record_po_replay_revalidates_snapshot(tmp_path: Path):
    """已回填同号回放也必须先核对批准快照，不能拿脏草稿直接回放。"""
    client, store, _manual = _client(tmp_path)
    body = _sync_and_draft(client)
    approval_id = body["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    assert client.post("/api/workflow/orders/ORD-MAN-1/dropship/execute").status_code == 200
    recorded = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": PO},
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["idempotent_replay"] is False

    tampered = store.get_dropship_draft("ORD-MAN-1")
    tampered["payload"] = dict(tampered["payload"], supplier_id="tampered-after-record")
    store.upsert_dropship_draft(tampered)

    replay = client.post(
        "/api/workflow/orders/ORD-MAN-1/dropship/record-po",
        json={"alibaba_order_id": PO},
    )
    assert replay.status_code == 409, replay.text
    assert "不一致" in replay.json()["detail"]

def test_service_owns_fulfillment_and_mount_does_not_rebuild(tmp_path: Path, monkeypatch) -> None:
    from shopkeeper.workflow import routes
    from shopkeeper.workflow.service import WorkflowService

    env = {
        "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
        "WORKFLOW_PLATFORM": "mock",
        "WORKFLOW_LLM_PROVIDER": "mock",
        "WORKFLOW_SCHEDULER": "0",
        "ALIBABA_DROPSHIP_MODE": "manual",
        "TTS_WRITE_ENABLED": "0",
        "TRACKING_LOOKUP_ENABLED": "0",
        "FEISHU_NOTIFY": "0",
    }
    service = WorkflowService.from_env(env)
    assert isinstance(service.fulfillment, ManualAlibabaDropship)

    import shopkeeper.adapters.alibaba_dropship as alibaba_dropship

    def _must_not_rebuild(_env=None):
        raise AssertionError("mount_workflow must use service.fulfillment")

    monkeypatch.setattr(alibaba_dropship, "build_fulfillment", _must_not_rebuild)
    app = FastAPI()
    routes.mount_workflow(app, service)
    assert app is not None
    assert service.orchestrator is not None
    assert service.orchestrator.fulfillment is service.fulfillment
