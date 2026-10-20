"""M4 利润看板最小切片：订单级利润、缺数据标注、按店/日汇总、只读 API / CLI。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.pricing.config import PricingConfig
from shopkeeper.workflow.profit import (
    MISSING,
    build_profit_report,
    compute_order_profit,
    profit_csv,
)
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowStore]:
    store = WorkflowStore(tmp_path / "workflow.db")
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=MockPlatform(),
    )
    return TestClient(app), store


def _cfg(**overrides: float | int | str) -> PricingConfig:
    base = PricingConfig(
        commission_pct=5.0,
        payment_fee_pct=2.0,
        ads_pct=0.0,
        fx_rate=5.0,
        fx_source="test:FX",
    )
    return PricingConfig(**{**base.__dict__, **overrides})


def test_complete_order_profit_math():
    order = {
        "order_id": "ORD-OK",
        "shop_id": "default",
        "status": "AWAITING_SHIPMENT",
        "settlement_base_amount": 20000,  # 200 THB
        "currency": "THB",
        "paid_at": "2026-10-10T08:00:00+07:00",
        "shipping_cny": 200,
    }
    dropship = {
        "order_id": "ORD-OK",
        "status": "pending_approval",
        "payload": {
            "items": [{"sku": "A", "qty": 2, "unit_cost_cents": 1000}],
        },
        "result": None,
    }
    line = compute_order_profit(order, dropship=dropship, config=_cfg())
    assert line["data_status"] == "ok"
    assert line["revenue"] == 20000
    assert line["platform_commission"] == 1000  # 5%
    assert line["payment_fee"] == 400  # 2%
    assert line["ads"] == 0
    assert line["purchase_cost_cny"] == 2000
    assert line["purchase_cost"] == 10000  # 2000 * fx5
    assert line["shipping_cny"] == 200
    assert line["shipping"] == 1000
    # 20000 - 1000 - 400 - 0 - 10000 - 1000 = 7600
    assert line["gross_profit"] == 7600
    assert line["gross_margin_pct"] == 38.0
    assert line["rates_placeholder"] is True


def test_missing_cost_marked_not_fabricated():
    order = {
        "order_id": "ORD-MISS",
        "shop_id": "default",
        "status": "PAID",
        "total_amount": "100",
        "currency": "THB",
        "paid_at": "2026-10-11",
    }
    line = compute_order_profit(order, dropship=None, config=_cfg())
    assert line["data_status"] == "incomplete"
    assert line["revenue"] == 10000
    assert line["platform_commission"] == 500
    assert line["purchase_cost"] == MISSING
    assert line["shipping"] == MISSING
    assert line["gross_profit"] == MISSING
    assert line["gross_margin_pct"] == MISSING
    assert "purchase_cost" in line["missing_fields"]
    assert "shipping" in line["missing_fields"]


def test_build_report_filters_shop_and_dates_and_aggregates(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf.db")
    store.replace_synced_orders(
        [
            {
                "order_id": "A",
                "status": "PAID",
                "settlement_base_amount": 10000,
                "currency": "THB",
                "paid_at": "2026-10-01T10:00:00Z",
                "shipping_cny": 100,
            },
            {
                "order_id": "B",
                "status": "PAID",
                "settlement_base_amount": 20000,
                "currency": "THB",
                "paid_at": "2026-10-08T10:00:00Z",
                "shipping_cny": 100,
            },
        ],
        source="test",
        shop_id="default",
    )
    store.replace_synced_orders(
        [
            {
                "order_id": "C",
                "status": "PAID",
                "settlement_base_amount": 5000,
                "currency": "THB",
                "paid_at": "2026-10-08T12:00:00Z",
                "shipping_cny": 50,
            },
        ],
        source="test",
        shop_id="th_home",
    )
    for oid, cost in (("A", 800), ("B", 1500), ("C", 400)):
        store.upsert_dropship_draft(
            {
                "order_id": oid,
                "status": "pending_approval",
                "payload": {"items": [{"qty": 1, "unit_cost_cents": cost}]},
                "approval_id": None,
                "external_po_id": None,
                "result": None,
            }
        )

    report = build_profit_report(
        store,
        shop_id="default",
        date_from="2026-10-05",
        date_to="2026-10-10",
        config=_cfg(),
    )
    assert report["order_count"] == 1
    assert report["orders"][0]["order_id"] == "B"
    assert report["rates"]["placeholder"] is True
    assert report["read_only"] is True
    assert len(report["by_day"]) == 1
    assert report["by_day"][0]["key"] == "2026-10-08"
    assert report["by_day"][0]["revenue"] == 20000
    assert report["by_week"]
    assert report["totals"]["data_status"] == "ok"

    all_shops = build_profit_report(store, config=_cfg())
    assert all_shops["order_count"] == 3
    shop_ids = {row["shop_id"] for row in all_shops["by_shop"]}
    assert shop_ids == {"default", "th_home"}


def test_api_profit_and_invalid_date(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TIKTOK_SHOPS", "default,th_home")
    client, store = _client(tmp_path)
    store.upsert_synced_order(
        {
            "order_id": "API-1",
            "status": "PAID",
            "total_amount": "50",
            "currency": "THB",
            "paid_at": "2026-10-12",
        },
        source="test",
        shop_id="default",
    )

    bad = client.get("/api/workflow/profit", params={"from": "not-a-date"})
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "PROFIT_DATE_INVALID"

    ok = client.get(
        "/api/workflow/profit",
        params={"shop_id": "default", "from": "2026-10-01", "to": "2026-10-31"},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["order_count"] == 1
    assert body["orders"][0]["purchase_cost"] == MISSING
    assert body["rates"]["placeholder"] is True
    assert "缺数据" in body["note"]
    assert body["read_only"] is True

    # 发货/退款仍 403；本切片不解锁真写
    assert client.post("/api/workflow/orders/API-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/API-1/refund").status_code == 403


def test_accrual_ledger_used_for_revenue_and_commission(tmp_path: Path):
    store = WorkflowStore(tmp_path / "acc.db")
    store.upsert_synced_order(
        {
            "order_id": "LEDGER-1",
            "status": "PAID",
            "settlement_base_amount": 10000,
            "currency": "THB",
            "paid_at": "2026-10-09",
            "shipping_cny": 0,
        },
        source="test",
        shop_id="default",
    )
    store.insert_ledger_entry(
        {
            "shop_id": "default",
            "entry_key": "sale:order=LEDGER-1:bootstrap",
            "order_id": "LEDGER-1",
            "entry_type": "sale",
            "currency": "THB",
            "amount": 10000,
            "base_currency": "CNY",
            "base_amount": 10000,
            "basis": "accrual",
            "ref_type": "bootstrap",
            "ref_id": "LEDGER-1",
            "memo": "test",
        }
    )
    store.insert_ledger_entry(
        {
            "shop_id": "default",
            "entry_key": "platform_commission:order=LEDGER-1:bootstrap",
            "order_id": "LEDGER-1",
            "entry_type": "platform_commission",
            "currency": "THB",
            "amount": -500,
            "base_currency": "CNY",
            "base_amount": -500,
            "basis": "accrual",
            "ref_type": "bootstrap",
            "ref_id": "LEDGER-1",
            "memo": "test",
        }
    )
    store.upsert_dropship_draft(
        {
            "order_id": "LEDGER-1",
            "status": "executed",
            "payload": {"items": [{"qty": 1, "unit_cost_cents": 500}], "shipping_cny": 0},
            "approval_id": None,
            "external_po_id": "po-1",
            "result": {"items": [{"qty": 1, "unit_cost_cents": 500}]},
        }
    )
    report = build_profit_report(store, shop_id="default", config=_cfg())
    line = report["orders"][0]
    assert line["sources"]["revenue"] == "accrual_ledger"
    assert line["sources"]["platform_commission"] == "accrual_ledger"
    assert line["revenue"] == 10000
    assert line["platform_commission"] == 500
    assert line["data_status"] == "ok"


def test_cli_profit_export_csv(tmp_path: Path, monkeypatch):
    from shopkeeper.cli import main

    db = tmp_path / "cli.db"
    store = WorkflowStore(db)
    store.upsert_synced_order(
        {
            "order_id": "CLI-1",
            "status": "PAID",
            "total_amount": "80",
            "currency": "THB",
            "paid_at": "2026-10-13",
        },
        source="test",
    )
    out = tmp_path / "profit.csv"
    monkeypatch.setenv("WORKFLOW_DB_PATH", str(db))
    code = main(
        [
            "profit-export",
            "--db",
            str(db),
            "--from",
            "2026-10-01",
            "--to",
            "2026-10-31",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "CLI-1" in text
    assert MISSING in text
    assert "rates_placeholder" in text
    # 无写文件时 profit_csv 也可生成
    report = build_profit_report(store, config=_cfg())
    csv_text = profit_csv(report)
    assert csv_text.startswith("\ufeff") or "order_id" in csv_text
