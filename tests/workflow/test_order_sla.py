"""发货 SLA 本地提醒：超时/临期可测；不履约；发货仍 403。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.workflow.order_sla import classify_order_sla, evaluate_orders_sla
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.serve import create_runtime_app
from shopkeeper.workflow.store import WorkflowStore

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


def test_classify_overdue_due_soon_and_ok():
    overdue = classify_order_sla(
        {
            "order_id": "O1",
            "status": "AWAITING_SHIPMENT",
            "ship_deadline": (NOW - timedelta(hours=3)).isoformat(),
        },
        now=NOW,
    )
    assert overdue["level"] == "overdue"
    assert overdue["needs_shipment"] is True
    assert overdue["hours_left"] < 0
    assert "超时" in overdue["message"]

    due = classify_order_sla(
        {
            "order_id": "O2",
            "status": "PAID",
            "ship_deadline": (NOW + timedelta(hours=2)).isoformat(),
        },
        now=NOW,
    )
    assert due["level"] == "due_soon"
    assert "临期" in due["message"]

    ok = classify_order_sla(
        {
            "order_id": "O3",
            "status": "AWAITING_SHIPMENT",
            "paid_at": (NOW - timedelta(hours=1)).isoformat(),
        },
        now=NOW,
        ship_window=timedelta(hours=48),
    )
    assert ok["level"] == "ok"
    assert ok["hours_left"] > 40

    shipped = classify_order_sla(
        {"order_id": "O4", "status": "SHIPPED", "ship_deadline": (NOW - timedelta(hours=1)).isoformat()},
        now=NOW,
    )
    assert shipped["level"] == "n/a"
    assert shipped["needs_shipment"] is False


def test_evaluate_sorts_overdue_first():
    result = evaluate_orders_sla(
        [
            {
                "order_id": "soon",
                "status": "AWAITING_SHIPMENT",
                "ship_deadline": (NOW + timedelta(hours=1)).isoformat(),
            },
            {
                "order_id": "late",
                "status": "AWAITING_SHIPMENT",
                "ship_deadline": (NOW - timedelta(hours=1)).isoformat(),
            },
            {"order_id": "done", "status": "DELIVERED", "ship_deadline": (NOW - timedelta(days=1)).isoformat()},
        ],
        now=NOW,
    )
    assert result["overdue"] == 1
    assert result["due_soon"] == 1
    assert result["ok"] == 0
    assert [r["order_id"] for r in result["reminders"]] == ["late", "soon"]
    assert "本地" in result["note"]
    assert "飞书" in result["note"]


def test_api_sla_after_sync_and_ship_still_403(tmp_path: Path):
    platform = MockPlatform(
        orders=[
            {
                "order_id": "ORD-LATE",
                "status": "AWAITING_SHIPMENT",
                "item_names": ["抽屉分隔盒"],
                "total_amount": "199",
                "currency": "THB",
                "ship_deadline": (NOW - timedelta(hours=5)).isoformat(),
            },
            {
                "order_id": "ORD-SOON",
                "status": "PAID",
                "ship_deadline": (NOW + timedelta(hours=3)).isoformat(),
            },
            {
                "order_id": "ORD-OK",
                "status": "AWAITING_SHIPMENT",
                "paid_at": (NOW - timedelta(hours=2)).isoformat(),
            },
        ]
    )
    store = WorkflowStore(tmp_path / "workflow.db")
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform)
    client = TestClient(app)

    # 用固定 now 覆盖 classify：同步后直接改 payload 再调 SLA 接口会用真实 now。
    # 因此把截止时间写得相对「测试运行时刻」足够极端，避免 flaky。
    far_past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    near_future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    far_future_paid = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    platform._orders = [
        {
            "order_id": "ORD-LATE",
            "status": "AWAITING_SHIPMENT",
            "ship_deadline": far_past,
        },
        {
            "order_id": "ORD-SOON",
            "status": "PAID",
            "ship_deadline": near_future,
        },
        {
            "order_id": "ORD-OK",
            "status": "AWAITING_SHIPMENT",
            "paid_at": far_future_paid,
        },
    ]

    synced = client.post("/api/workflow/orders/sync", json={"since": ""})
    assert synced.status_code == 200
    body = synced.json()
    assert body["synced"] == 3
    assert "sla" in body
    assert body["sla"]["overdue"] >= 1
    assert body["sla"]["due_soon"] >= 1

    sla = client.get("/api/workflow/orders/sla")
    assert sla.status_code == 200
    data = sla.json()
    levels = {r["order_id"]: r["level"] for r in data["orders"]}
    assert levels["ORD-LATE"] == "overdue"
    assert levels["ORD-SOON"] == "due_soon"
    assert levels["ORD-OK"] == "ok"
    assert any(r["order_id"] == "ORD-LATE" for r in data["reminders"])

    listed = client.get("/api/workflow/orders/synced")
    assert listed.status_code == 200
    assert listed.json()["sla"]["overdue"] >= 1

    assert client.post("/api/workflow/orders/ORD-LATE/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-LATE/refund").status_code == 403
    assert platform.update_tracking_calls == []


def test_workflow_page_shows_sla_section(tmp_path, monkeypatch):
    from tests.workflow.env_isolation import isolate_runtime_env

    isolate_runtime_env(monkeypatch, tmp_path / "rt", platform="mock")
    client = TestClient(create_runtime_app(start_scheduler=False))
    page = client.get("/workflow")
    assert page.status_code == 200
    assert "发货 SLA" in page.text
    assert "/api/workflow/orders/sla" in page.text or "刷新发货 SLA" in page.text
