"""/admin 完整利润控制台：沿用后台壳，复用利润 API，只读。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.admin import __file__ as admin_pkg_file
from shopkeeper.workflow.auth import create_admin_user
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService

SRC = Path(__file__).resolve().parents[2] / "apps" / "admin" / "src" / "main.js"
DIST = Path(admin_pkg_file).resolve().parent / "dist"


def _page_profit_source() -> str:
    text = SRC.read_text(encoding="utf-8")
    start = text.index("function pageProfit()")
    end = text.index("function pageWorkflows()")
    return text[start:end]


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowService]:
    service = WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "WORKFLOW_PLATFORM": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
        }
    )
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    create_admin_user(service.store, username="view1", password="password123", role="viewer")
    app = FastAPI()
    mount_workflow(app, service)
    return TestClient(app), service


def test_profit_page_source_is_readonly_and_styled_like_admin():
    page = _page_profit_source()
    main = SRC.read_text(encoding="utf-8")
    assert '{ id: "profit", label: "利润" }' in main
    assert 'label: "运营"' in main
    assert "/api/workflow/profit" in page
    assert "/api/workflow/shops" in main
    assert "缺数据" in page
    assert "PRICING_*" in page
    assert "占位费率" in page
    assert "alert_only" in page
    assert "id=\"profit-shop\"" in page
    assert "id=\"profit-from\"" in page
    assert "id=\"profit-to\"" in page
    assert "id=\"profit-console-form\"" in page
    assert "profit-summary-cards" in page
    assert "订单明细" in page
    assert "按日" in page
    assert "按周" in page
    assert "grid-4" in page
    assert "banner" in page
    assert "不自动改价" in page
    assert "不建审批" in page
    assert "不调预算" in page
    for forbidden in (
        "btn-run-approve",
        "btn-run-reject",
        "btn-start-run",
        "ad-spend-form",
        "btn-ads-pull",
        "/ship",
        "/refund",
        "TTS_WRITE_ENABLED",
        "data-act=\"ship\"",
        "data-act=\"refund\"",
        "id=\"withdraw\"",
        "id=\"payout\"",
    ):
        assert forbidden not in page


def test_admin_profit_and_dashboard_routes_serve_spa(tmp_path: Path):
    client, _service = _client(tmp_path)
    for path in ("/admin/profit", "/dashboard/profit"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "text/html" in page.headers.get("content-type", "")
        assert "/admin/assets/" in page.text


def test_viewer_reads_profit_api_and_never_unlocks_fulfillment(tmp_path: Path):
    client, service = _client(tmp_path)
    store = service.store
    store.upsert_synced_order(
        {
            "order_id": "ADMIN-P1",
            "status": "PAID",
            "total_amount": "40",
            "currency": "THB",
            "paid_at": "2026-10-12",
        },
        source="test",
        shop_id="default",
    )

    login = client.post("/api/auth/login", json={"username": "view1", "password": "password123"})
    assert login.status_code == 200

    body = client.get(
        "/api/workflow/profit",
        params={"shop_id": "default", "from": "2026-10-01", "to": "2026-10-31"},
    ).json()
    assert body["read_only"] is True
    assert body["alert_only"] is True
    assert body["rates"]["placeholder"] is True
    assert body["orders"][0]["purchase_cost"] == "缺数据"
    assert body["orders"][0]["shipping"] == "缺数据"
    assert "PRICING" in (body["rates"].get("note") or "") or body["rates"]["placeholder"] is True
    assert client.get("/api/workflow/ad-spend", params={"shop_id": "default"}).status_code == 200

    bad = client.get("/api/workflow/profit", params={"from": "not-a-date"})
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "PROFIT_DATE_INVALID"

    denied = client.post(
        "/api/workflow/ad-spend",
        json={"shop_id": "default", "spend_date": "2026-10-12", "amount_thb": "10"},
    )
    assert denied.status_code == 403
    assert client.post("/api/workflow/orders/ADMIN-P1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ADMIN-P1/refund").status_code == 403


def test_built_bundle_contains_profit_page():
    assets = DIST / "assets"
    assert assets.is_dir(), "缺少管理后台构建产物，请在 apps/admin 执行 npm run build"
    blobs = [p.read_text(encoding="utf-8", errors="ignore") for p in assets.glob("*.js")]
    assert blobs, "dist/assets 下没有 js"
    joined = "\n".join(blobs)
    assert "利润" in joined
    assert "/api/workflow/profit" in joined
    assert "缺数据" in joined
    assert "PRICING_*" in joined
    assert "alert_only" in joined
    assert "/dashboard/profit" in joined
