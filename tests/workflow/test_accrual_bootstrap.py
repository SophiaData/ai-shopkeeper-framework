"""真空期台账 bootstrap + R3 两段闭合；发货/退款仍 403；不打开 TTS 真写。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.workflow.accrual import (
    net_accrual_for_order,
    order_amount_cents,
    run_close_check,
)
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore


def _client(tmp_path: Path, orders: list[dict] | None = None) -> tuple[TestClient, WorkflowStore, MockPlatform]:
    platform = MockPlatform(orders=orders or [])
    store = WorkflowStore(tmp_path / "workflow.db")
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform)
    return TestClient(app), store, platform


def test_empty_ledger_bootstrap_balanced(tmp_path: Path):
    client, store, _platform = _client(tmp_path, orders=[])

    status_before = client.get("/api/workflow/orders/accrual/status")
    assert status_before.status_code == 200
    assert status_before.json()["bootstrapped"] is False
    assert status_before.json()["accrual_entry_count"] == 0

    boot = client.post("/api/workflow/orders/accrual/bootstrap", json={})
    assert boot.status_code == 200
    body = boot.json()
    assert body["bootstrapped"] is True
    assert body["bootstrap"]["orders_considered"] == 0
    assert body["bootstrap"]["entries_created"] == 0
    assert body["close_check"]["balanced"] is True
    assert body["close_check"]["opening"] == 0
    assert body["close_check"]["period_delta"] == 0
    assert body["close_check"]["closing"] == 0
    assert body["recon_diffs"] == []
    assert "不改订单" in body["note"]
    assert store.list_ledger_entries() == []


def test_bootstrap_close_passes_and_idempotent(tmp_path: Path):
    orders = [
        {
            "order_id": "ORD-A",
            "status": "AWAITING_SHIPMENT",
            "total_amount": "200",
            "currency": "THB",
        },
        {
            "order_id": "ORD-B",
            "status": "PAID",
            "settlement_base_amount": 15000,
            "currency": "THB",
        },
        {
            "order_id": "ORD-X",
            "status": "CANCELLED",
            "total_amount": "999",
            "currency": "THB",
        },
    ]
    client, store, _platform = _client(tmp_path, orders=orders)

    synced = client.post("/api/workflow/orders/sync", json={"since": ""})
    assert synced.status_code == 200
    assert synced.json()["synced"] == 3

    boot = client.post("/api/workflow/orders/accrual/bootstrap", json={})
    assert boot.status_code == 200
    body = boot.json()
    assert body["bootstrap"]["orders_considered"] == 2
    assert "ORD-A" in body["bootstrap"]["order_ids"]
    assert "ORD-B" in body["bootstrap"]["order_ids"]
    assert "ORD-X" not in body["bootstrap"]["order_ids"]
    # sale + commission × 2 open orders
    assert body["bootstrap"]["entries_created"] == 4
    assert body["close_check"]["balanced"] is True
    assert body["close_check"]["period_delta"] == 0
    assert body["close_check"]["opening"] == body["close_check"]["closing"]
    assert body["recon_diffs"] == []

    expected_net = net_accrual_for_order(orders[0]) + net_accrual_for_order(orders[1])
    assert body["close_check"]["opening"] == expected_net
    assert order_amount_cents(orders[0]) == 20000

    again = client.post("/api/workflow/orders/accrual/bootstrap", json={})
    assert again.status_code == 200
    assert again.json()["bootstrap"]["entries_created"] == 0
    assert again.json()["bootstrap"]["entries_skipped_idempotent"] == 4
    assert again.json()["close_check"]["balanced"] is True
    assert len(store.list_ledger_entries(basis="accrual")) == 4

    status = client.get("/api/workflow/orders/accrual/status")
    assert status.status_code == 200
    assert status.json()["bootstrapped"] is True
    assert status.json()["close_check"]["balanced"] is True
    assert status.json()["read_only"] is True


def test_intentional_mismatch_writes_recon_diff(tmp_path: Path):
    orders = [
        {
            "order_id": "ORD-DIFF",
            "status": "AWAITING_SHIPMENT",
            "total_amount": "100",
            "currency": "THB",
        }
    ]
    client, store, platform = _client(tmp_path, orders=orders)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    boot = client.post("/api/workflow/orders/accrual/bootstrap", json={})
    assert boot.json()["close_check"]["balanced"] is True
    opening = boot.json()["close_check"]["opening"]

    # 故意改平台侧金额（只读缓存），不改台账 → 期末偏离期初
    platform._orders = [
        {
            "order_id": "ORD-DIFF",
            "status": "AWAITING_SHIPMENT",
            "total_amount": "180",
            "currency": "THB",
        }
    ]
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200

    close = run_close_check(store)
    assert close["balanced"] is False
    assert close["opening"] == opening
    assert close["period_delta"] == 0
    assert close["closing"] != opening
    assert close["diff_amount"] == close["closing"] - close["expected_closing"]
    assert len(close["recon_diffs"]) == 1
    diff = close["recon_diffs"][0]
    assert diff["recon_line"] == "ledger_vs_settlement"
    assert diff["state"] == "open"
    assert diff["expected"] == opening
    assert diff["actual"] == close["closing"]

    # 差异只进列表，不改订单缓存
    synced = store.list_synced_orders()
    assert len(synced) == 1
    assert order_amount_cents(synced[0]) == 18000

    status = client.get("/api/workflow/orders/accrual/status")
    assert status.status_code == 200
    assert status.json()["close_check"]["balanced"] is False
    assert len(status.json()["recon_diffs"]) >= 1


def test_period_settlement_keeps_equation(tmp_path: Path):
    """期间写入 settlement 冲正后，期初+Δ 仍须等于期末。"""
    orders = [
        {
            "order_id": "ORD-S",
            "status": "COMPLETED",
            "settlement_base_amount": 10000,
            "currency": "THB",
        }
    ]
    client, store, _platform = _client(tmp_path, orders=orders)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    boot = client.post("/api/workflow/orders/accrual/bootstrap", json={})
    opening = boot.json()["close_check"]["opening"]
    assert boot.json()["close_check"]["balanced"] is True

    # 模拟结算到达：冲正全部 accrual 净额
    store.insert_ledger_entry(
        {
            "shop_id": "local",
            "entry_key": "settlement:order=ORD-S:close",
            "order_id": "ORD-S",
            "entry_type": "payout_diff",
            "currency": "THB",
            "amount": -opening,
            "base_amount": -opening,
            "basis": "settlement",
            "reversal_kind": "settlement_reestimate",
            "ref_type": "settlement",
            "ref_id": "mock-set-1",
        }
    )
    # 平台侧该单已结清 → 期末 0
    store.replace_synced_orders([], source="mock_platform")
    close = run_close_check(store)
    assert close["opening"] == opening
    assert close["period_delta"] == -opening
    assert close["closing"] == 0
    assert close["balanced"] is True
    assert close["recon_diffs"] == []


def test_ship_refund_still_403_after_accrual(tmp_path: Path):
    client, _store, _platform = _client(
        tmp_path,
        orders=[{"order_id": "ORD-1", "status": "PAID", "total_amount": "50", "currency": "THB"}],
    )
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    assert client.post("/api/workflow/orders/accrual/bootstrap", json={}).status_code == 200

    ship = client.post("/api/workflow/orders/ORD-1/ship")
    refund = client.post("/api/workflow/orders/ORD-1/refund")
    assert ship.status_code == 403
    assert refund.status_code == 403
    assert "发货" in ship.json()["detail"]
    assert "退款" in refund.json()["detail"]
    # 未调用任何 TTS 真写：MockPlatform 无 create_listing / update_tracking
    assert _platform.create_listing_calls == []
    assert _platform.update_tracking_calls == []
