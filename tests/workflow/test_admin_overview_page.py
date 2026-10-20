"""/admin 总览：健康横幅与组件卡只读现有信号；一键演示入口；不编造指标。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.admin import __file__ as admin_pkg_file
from shopkeeper.workflow.admin_routes import REQUIRED_HEALTH_IDS, build_overview
from shopkeeper.workflow.auth import create_admin_user
from shopkeeper.workflow.migrate import CURRENT_SCHEMA_VERSION
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService

SRC = Path(__file__).resolve().parents[2] / "apps" / "admin" / "src" / "main.js"
DIST = Path(admin_pkg_file).resolve().parent / "dist"


def _service(tmp_path: Path) -> WorkflowService:
    return WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "WORKFLOW_PLATFORM": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
        }
    )


def test_overview_facts_from_existing_signals(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("LLM_DAILY_BUDGET_USD", raising=False)
    monkeypatch.setenv("LLM_CHANNEL", "mock")
    monkeypatch.setenv("ORCHESTRATOR_AUTORUN", "0")
    service = _service(tmp_path)
    overview = build_overview(service)
    ids = [c["id"] for c in overview["components"]]
    assert list(REQUIRED_HEALTH_IDS) == ids
    for card in overview["components"]:
        assert "latency_ms" not in card
    budget = next(c for c in overview["components"] if c["id"] == "llm_budget")
    assert budget["status"] == "unlimited"
    assert budget["remaining_usd"] is None
    assert budget["tag"] == "不限额"
    fact_ids = [f["id"] for f in overview["facts"]]
    assert fact_ids == ["db_migrate", "publish_enabled", "autorun", "llm_channel", "worker_queue"]
    facts = {f["id"]: f for f in overview["facts"]}
    assert facts["db_migrate"]["ok"] is True
    assert f"v{CURRENT_SCHEMA_VERSION}" in facts["db_migrate"]["value"]
    assert facts["autorun"]["value"] == "off"
    assert facts["llm_channel"]["value"] == "mock"
    assert "publish_enabled" in overview["banner"]
    assert "autorun" in overview["banner"]
    assert "LLM 通道" in overview["banner"]
    tiktok = next(c for c in overview["components"] if c["id"] == "tiktok")
    assert tiktok["tag"] == "mock"


def test_overview_llm_budget_remaining_when_limit_set(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "2.5")
    service = _service(tmp_path)
    overview = build_overview(service)
    budget = next(c for c in overview["components"] if c["id"] == "llm_budget")
    assert budget["status"] == "recorded"
    assert budget["limit_usd"] == 2.5
    assert budget["spent_usd"] == 0.0
    assert budget["remaining_usd"] == 2.5


def test_overview_api_requires_login_and_returns_facts(tmp_path: Path):
    service = _service(tmp_path)
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    assert client.get("/api/admin/overview").status_code == 401
    login = client.post("/api/auth/login", json={"username": "owner1", "password": "password123"})
    assert login.status_code == 200
    body = client.get("/api/admin/overview").json()
    assert body["system_ok"] is True
    assert {c["id"] for c in body["components"]} == set(REQUIRED_HEALTH_IDS)
    assert body["facts"]


def test_overview_and_workflows_page_source_has_demo_and_health():
    main = SRC.read_text(encoding="utf-8")
    start = main.index("function pageOverview()")
    end = main.index("async function loadApprovals")
    overview = main[start:end]
    wf_start = main.index("function pageWorkflows()")
    wf_end = main.index("function healthDotClass")
    workflows = main[wf_start:wf_end]
    assert "一键 mock 演示" in overview
    assert "btn-demo-orch" in overview
    assert "banner-facts" in overview
    assert "health-dot" in overview
    assert "/healthz" in overview
    assert "btn-demo-orch" in workflows
    assert "/api/admin/demo-orch" in main
    assert "workflows?run=" in main
    assert "只读账号不能触发演示" in overview
    # 本切片不改报表/利润页写路径
    reports = main[main.index("function pageReports()") : main.index("function pageWorkflows()")]
    assert "btn-demo-orch" not in reports
    assert "/api/admin/demo-orch" not in reports


def test_admin_overview_route_serves_spa(tmp_path: Path):
    service = _service(tmp_path)
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    page = client.get("/admin/overview")
    assert page.status_code == 200
    assert "text/html" in page.headers.get("content-type", "")


def test_built_bundle_contains_overview_demo():
    assets = DIST / "assets"
    assert assets.is_dir(), "缺少管理后台构建产物，请在 apps/admin 执行 npm run build"
    blobs = [p.read_text(encoding="utf-8", errors="ignore") for p in assets.glob("*.js")]
    assert blobs, "dist/assets 下没有 js"
    joined = "\n".join(blobs)
    assert "一键 mock 演示" in joined
    assert "/api/admin/demo-orch" in joined
    assert "banner-facts" in joined
