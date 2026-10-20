"""/admin AI 代理页：沿用后台壳，只读，构建产物含入口。"""

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


def _page_agents_source() -> str:
    text = SRC.read_text(encoding="utf-8")
    start = text.index("function pageAgents()")
    end = text.index("function pageSettings()")
    return text[start:end]


def test_agents_page_source_is_readonly_and_styled_like_admin():
    page = _page_agents_source()
    main = SRC.read_text(encoding="utf-8")
    assert '{ id: "agents", label: "AI 代理" }' in main
    assert "/api/admin/agents/stats" in main
    assert "/api/admin/agents/runs/" in main
    assert "缺数据" in page
    assert "grid-agents" in page
    assert "health-dot" in page
    assert "编排 run" in page
    assert "挂起原因" in page
    assert "只读" in page
    assert "data-agent-run" in page
    assert "id=\"agent-from\"" in page
    assert "id=\"agent-to\"" in page
    # 本页不提供发货/退款/付款/真写，也不复用工作流页的审批写按钮
    for forbidden in (
        "btn-run-approve",
        "btn-run-reject",
        "btn-start-run",
        "/ship",
        "/refund",
        "TTS_WRITE_ENABLED",
        "data-act=\"ship\"",
        "data-act=\"refund\"",
    ):
        assert forbidden not in page


def test_admin_agents_route_serves_spa(tmp_path: Path):
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
    page = client.get("/admin/agents")
    assert page.status_code == 200
    assert "text/html" in page.headers.get("content-type", "")
    assert "/admin/assets/" in page.text


def test_built_bundle_contains_agents_page():
    assets = DIST / "assets"
    assert assets.is_dir(), "缺少管理后台构建产物，请在 apps/admin 执行 npm run build"
    blobs = [p.read_text(encoding="utf-8", errors="ignore") for p in assets.glob("*.js")]
    assert blobs, "dist/assets 下没有 js"
    joined = "\n".join(blobs)
    assert "AI 代理" in joined
    assert "/api/admin/agents/stats" in joined
    assert "缺数据" in joined
