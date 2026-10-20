"""工作台 /workflow 利润区块：只读入口、缺数据文案、无资金按钮。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.routes import PAGE, mount_workflow
from shopkeeper.workflow.service import WorkflowService
from shopkeeper.workflow.store import WorkflowStore
from tests.workflow.env_isolation import isolate_runtime_env


def _page_client(tmp_path: Path, monkeypatch) -> TestClient:
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
    return TestClient(app)


def test_workflow_page_has_profit_section(tmp_path, monkeypatch):
    client = _page_client(tmp_path, monkeypatch)
    page = client.get("/workflow")
    assert page.status_code == 200
    text = page.text
    assert 'id="profit"' in text
    assert ">利润<" in text or ">利润</h2>" in text
    assert "/api/workflow/profit" in text
    assert "缺数据" in text
    assert "占位费率" in text
    assert "refresh-profit" in text
    assert "profit-from" in text
    assert "profit-to" in text
    assert "profit-shop" in text
    # 不做资金动作
    assert "提现" in text  # 页脚说明「无…提现按钮」
    assert "付款/退款/提现" in text or "不做付款、退款或提现" in text
    for forbidden in ("id=\"withdraw\"", "id=\"payout\"", "data-act='withdraw'", "data-act='refund'"):
        assert forbidden not in text
    neighbor = client.get("/workflow/profit")
    assert neighbor.status_code == 200
    assert "location.hash='profit'" in neighbor.text
    assert "/api/workflow/profit" in neighbor.text


def test_page_template_mentions_placeholder_rates_and_missing():
    assert "占位费率" in PAGE
    assert "缺数据" in PAGE
    assert "本页无付款/退款/提现按钮" in PAGE
    assert "renderProfitReport" in PAGE
    assert "moneyCell" in PAGE


def test_profit_api_still_read_only_from_mounted_workflow(tmp_path: Path, monkeypatch):
    """挂载完整工作台后，利润 API 仍只读；发货/退款 403。"""
    monkeypatch.setenv("TIKTOK_SHOPS", "default")
    store = WorkflowStore(tmp_path / "workflow.db")
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=MockPlatform(),
    )
    client = TestClient(app)
    store.upsert_synced_order(
        {
            "order_id": "UI-1",
            "status": "PAID",
            "total_amount": "40",
            "currency": "THB",
            "paid_at": "2026-10-12",
        },
        source="test",
        shop_id="default",
    )
    body = client.get(
        "/api/workflow/profit",
        params={"shop_id": "default", "from": "2026-10-01", "to": "2026-10-31"},
    ).json()
    assert body["read_only"] is True
    assert body["rates"]["placeholder"] is True
    assert body["orders"][0]["purchase_cost"] == "缺数据"
    assert client.post("/api/workflow/orders/UI-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/UI-1/refund").status_code == 403
