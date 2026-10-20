"""/admin 编排运行页：沿用后台壳，只读时间线，构建产物含入口。"""

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


def _page_runs_source() -> str:
    text = SRC.read_text(encoding="utf-8")
    start = text.index("function shortTime(")
    end = text.index("async function loadProfitPage()")
    return text[start:end]


def test_runs_page_source_is_readonly_and_styled_like_admin():
    page = _page_runs_source()
    main = SRC.read_text(encoding="utf-8")
    assert '{ id: "runs", label: "编排运行" }' in main
    assert "/api/admin/runs" in main
    assert "runs?run=" in main
    assert "orch-run-banner" in page
    assert "orch-run-health" in page
    assert "orch-run-timeline" in page
    assert "health-dot" in page
    assert "grid-4" in page
    assert "banner" in page
    assert "等待人工审批" in page
    assert "缺数据" in page
    assert "去审批队列" in page
    assert "data-orch-run" in page
    assert "data-orch-approval" in page
    assert "error-data" in page
    assert "短视频脚本" in page
    assert "达人建联" in page
    assert "只读" in page
    assert "商品" in page
    assert "耗时" in page
    assert "停在" in page
    for forbidden in (
        "btn-run-approve",
        "btn-run-reject",
        "btn-start-run",
        "btn-demo-orch",
        "/ship",
        "/refund",
        "TTS_WRITE_ENABLED",
        "data-act=\"ship\"",
        "data-act=\"refund\"",
    ):
        assert forbidden not in page, forbidden


def test_admin_runs_route_serves_spa(tmp_path: Path):
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
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    page = client.get("/admin/runs")
    assert page.status_code == 200
    assert "text/html" in page.headers.get("content-type", "")
    assert "/admin/assets/" in page.text


def test_built_bundle_contains_runs_page():
    assets = DIST / "assets"
    assert assets.is_dir(), "缺少管理后台构建产物，请在 apps/admin 执行 npm run build"
    blobs = [p.read_text(encoding="utf-8", errors="ignore") for p in assets.glob("*.js")]
    assert blobs, "dist/assets 下没有 js"
    joined = "\n".join(blobs)
    assert "编排运行" in joined
    assert "/api/admin/runs" in joined
    assert "去审批队列" in joined
    assert "orch-run-timeline" in joined
