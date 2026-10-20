"""多平台订单最小切片：platform 维度、Shopee CSV 只读导入、工作台过滤。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.shopee_orders import ShopeeOrderImportError, parse_shopee_orders_csv
from shopkeeper.workflow import migrate as M
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService
from shopkeeper.workflow.store import WorkflowStore


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
        "WORKFLOW_PLATFORM": "mock",
        "WORKFLOW_LLM_PROVIDER": "mock",
        "WORKFLOW_SCHEDULER": "0",
        "WORKFLOW_WORKER_INLINE": "0",
        "TTS_WRITE_ENABLED": "0",
        "TRACKING_LOOKUP_ENABLED": "0",
        "FEISHU_NOTIFY": "0",
        "ALIBABA_DROPSHIP_MODE": "manual",
        "TIKTOK_SHOPS": "default",
    }


def test_synced_orders_are_scoped_by_platform_and_shop(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    store.upsert_synced_orders(
        [{"order_id": "shared", "status": "PAID"}],
        source="tiktok_shop", shop_id="default", platform="tiktok",
    )
    store.upsert_synced_orders(
        [{"order_id": "shared", "status": "READY_TO_SHIP"}],
        source="shopee_csv", shop_id="default", platform="shopee",
    )
    assert len(store.list_synced_orders()) == 2
    assert [o["platform"] for o in store.list_synced_orders(platform="tiktok")] == ["tiktok"]
    assert [o["platform"] for o in store.list_synced_orders(platform="shopee")] == ["shopee"]
    assert store.get_synced_order("shared", platform="shopee")["status"] == "READY_TO_SHIP"
    with pytest.raises(ValueError):
        store.get_synced_order("shared")

    store.replace_synced_orders([], source="shopee_csv", shop_id="default", platform="shopee")
    assert [o["platform"] for o in store.list_synced_orders()] == ["tiktok"]


def test_migration_v11_backfills_platform_and_allows_same_order_id(tmp_path: Path) -> None:
    db = tmp_path / "v10.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    M._ensure_version_table(conn)
    for version in sorted(v for v in M.MIGRATIONS if v <= 10):
        M.MIGRATIONS[version](conn)
    M.set_schema_version(conn, 10)
    conn.execute(
        "INSERT INTO synced_orders (order_id, status, payload_json, source, synced_at, shop_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("legacy-1", "PAID", "{}", "tiktok_shop", "2026-10-19T00:00:00+00:00", "default"),
    )
    conn.commit()
    assert M.apply_migrations(conn, backup=False) == 11
    row = conn.execute("SELECT platform FROM synced_orders WHERE order_id='legacy-1'").fetchone()
    assert row["platform"] == "tiktok"
    conn.execute(
        "INSERT INTO synced_orders (order_id, status, payload_json, source, synced_at, shop_id, platform) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy-1", "READY_TO_SHIP", "{}", "shopee_csv", "2026-10-19T00:00:00+00:00", "default", "shopee"),
    )
    conn.commit()
    assert conn.execute("SELECT count(*) FROM synced_orders WHERE order_id='legacy-1'").fetchone()[0] == 2
    conn.close()


def test_shopee_csv_parser_groups_items_and_rejects_missing_header() -> None:
    csv_text = (
        "order_id,status,item_name,item_sku,quantity,currency,buyer_username\n"
        "S1,READY_TO_SHIP,Storage Box,SKU-1,2,THB,alice\n"
        "S1,READY_TO_SHIP,Storage Lid,SKU-2,1,THB,alice\n"
    )
    orders = parse_shopee_orders_csv(csv_text, shop_id="my-shop")
    assert len(orders) == 1
    assert orders[0]["platform"] == "shopee"
    assert orders[0]["shop_id"] == "my-shop"
    assert len(orders[0]["items"]) == 2
    with pytest.raises(ShopeeOrderImportError):
        parse_shopee_orders_csv("status,item_name\nPAID,x")


def test_shopee_import_api_and_platform_filter(tmp_path: Path) -> None:
    service = WorkflowService.from_env(_env(tmp_path))
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    csv_text = "order_id,status,item_name,item_sku,quantity,currency\nS-API,PAID,Box,SKU-1,1,THB\n"
    imported = client.post(
        "/api/workflow/orders/import/shopee",
        json={"csv_text": csv_text, "shop_id": "default"},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["platform"] == "shopee"
    assert imported.json()["imported"] == 1

    shopee = client.get("/api/workflow/orders/synced?shop_id=default&platform=shopee")
    assert shopee.status_code == 200
    assert [o["platform"] for o in shopee.json()["orders"]] == ["shopee"]
    tiktok = client.get("/api/workflow/orders/synced?shop_id=default&platform=tiktok")
    assert tiktok.json()["orders"] == []
    blocked = client.post(
        "/api/workflow/orders/sync",
        json={"shop_id": "default", "platform": "shopee"},
    )
    assert blocked.status_code == 409

def test_orchestrator_tick_ignores_shopee_readonly_orders(tmp_path: Path) -> None:
    from shopkeeper.adapters.alibaba_dropship import MockAlibabaDropship
    from shopkeeper.adapters.mock import MockPlatform
    from shopkeeper.orchestrator.engine import Orchestrator

    store = WorkflowStore(tmp_path / "workflow.db")
    store.upsert_synced_orders(
        [{"order_id": "SHOPEE-ORCH", "status": "READY_TO_SHIP"}],
        source="shopee_csv", shop_id="default", platform="shopee",
    )
    orch = Orchestrator(store, platform=MockPlatform(), fulfillment=MockAlibabaDropship())
    assert orch.tick_order_fulfillment(limit=5) == []
