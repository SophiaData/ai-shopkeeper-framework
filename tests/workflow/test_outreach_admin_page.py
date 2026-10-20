"""/admin 达人建联页：治理侧栏、待审草稿、批准只改状态、发送固定拒绝。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.admin import __file__ as admin_pkg_file
from shopkeeper.adapters.mock import MockPlatform, MockProductSource
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.workflow.auth import create_admin_user
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService

SRC = Path(__file__).resolve().parents[2] / "apps" / "admin" / "src" / "main.js"
DIST = Path(admin_pkg_file).resolve().parent / "dist"
FIXTURE_URL = "https://detail.1688.com/offer/600123456789.html"
CONSTRAINT = "不自动发送；不外呼 TikTok；不打开 TTS_WRITE_*；批准只改状态，不发送私信、不寄样、不付款。"


def _page_source() -> str:
    text = SRC.read_text(encoding="utf-8")
    start = text.index("function pageOutreach()")
    end = text.index("function pageSettings()")
    return text[start:end]


def _service(tmp_path: Path) -> WorkflowService:
    return WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "WORKFLOW_PLATFORM": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
            "LLM_CHANNEL": "mock",
            "ORCHESTRATOR_MOCK_LLM_COST_USD": "0",
        }
    )


def _seed(service: WorkflowService) -> str:
    orch = Orchestrator(
        service.store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env={
            "LLM_CHANNEL": "mock",
            "ORCHESTRATOR_MOCK_LLM_COST_USD": "0",
            "WORKFLOW_PLATFORM": "mock",
            "TTS_WRITE_ENABLED": "0",
        },
        sleep_fn=lambda _s: None,
    )
    detail = orch.start(url=FIXTURE_URL, advance=True)
    assert detail["status"] == "awaiting_approval"
    return detail["id"]


def test_outreach_page_source_nav_and_constraints():
    page = _page_source()
    main = SRC.read_text(encoding="utf-8")
    assert '{ id: "outreach", label: "达人建联" }' in main
    gov = main.split('label: "治理"', 1)[1].split("],", 1)[0]
    assert '{ id: "outreach", label: "达人建联" }' in gov
    assert "/api/workflow/outreach/drafts" in main
    assert "data-outreach-approve" in page
    assert "data-outreach-reject" in page
    assert "btn-refresh-outreach" in page
    assert "待你确认" in page
    assert "page-foot" in page
    assert "outreach-card" in page
    assert CONSTRAINT in page
    for forbidden in (
        "data-outreach-send",
        "btn-ship",
        "data-act=\"ship\"",
        "data-act=\"refund\"",
        "TTS_WRITE_ENABLED=1",
        "/outreach/drafts/${",
    ):
        assert forbidden not in page
    assert "/send" not in page


def test_admin_outreach_route_serves_spa(tmp_path: Path):
    service = _service(tmp_path)
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    page = client.get("/admin/outreach")
    assert page.status_code == 200
    assert "text/html" in page.headers.get("content-type", "")
    assert "/admin/assets/" in page.text


def test_built_bundle_contains_outreach_page():
    assets = DIST / "assets"
    assert assets.is_dir(), "缺少管理后台构建产物，请在 apps/admin 执行 npm run build"
    blobs = [path.read_text(encoding="utf-8", errors="ignore") for path in assets.glob("*.js")]
    assert blobs, "dist/assets 下没有 js"
    joined = "\n".join(blobs)
    assert "达人建联" in joined
    assert "/api/workflow/outreach/drafts" in joined
    assert "不自动发送" in joined
    assert "批准只改状态" in joined


def test_approve_and_reject_mark_status_only(tmp_path: Path):
    service = _service(tmp_path)
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    create_admin_user(service.store, username="view1", password="password123", role="viewer")
    _seed(service)
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": "owner1", "password": "password123"})
    assert login.status_code == 200, login.text

    listed = client.get("/api/workflow/outreach/drafts")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["sent"] is False
    assert body["auto_send"] is False
    assert body["count"] == 3
    drafts = body["drafts"]
    assert all(row["status"] == "pending" for row in drafts)
    assert all(row["sent"] is False for row in drafts)

    before = service.store.list_external_writes()
    approved = client.post(f"/api/workflow/outreach/drafts/{drafts[0]['id']}/approve", json={"note": "文案可以"})
    assert approved.status_code == 200, approved.text
    approved_body = approved.json()
    assert approved_body["sent"] is False
    assert approved_body["published"] is False
    assert approved_body["dispatched"] is False
    assert approved_body["auto_send"] is False
    assert approved_body["draft"]["status"] == "approved"
    assert service.store.list_external_writes() == before
    assert service.store.list_external_writes(drafts[0]["id"]) == []

    missing_note = client.post(
        f"/api/workflow/outreach/drafts/{drafts[1]['id']}/reject",
        json={"note": "  "},
    )
    assert missing_note.status_code == 400

    rejected = client.post(
        f"/api/workflow/outreach/drafts/{drafts[1]['id']}/reject",
        json={"note": "语气不像私信"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["draft"]["status"] == "rejected"
    assert rejected.json()["sent"] is False
    assert service.store.list_external_writes(drafts[1]["id"]) == []

    blocked = client.post(f"/api/workflow/outreach/drafts/{drafts[2]['id']}/send")
    assert blocked.status_code == 403
    assert "不会发送" in blocked.json()["detail"]
    still = service.store.get_draft(drafts[2]["id"])
    assert still is not None
    assert still["status"] == "pending"

    publish = client.post(f"/api/workflow/drafts/{drafts[2]['id']}/publish")
    assert publish.status_code == 403
    assert client.post("/api/workflow/orders/ORD-X/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-X/refund").status_code == 403

    client.post("/api/auth/logout")
    viewer = client.post("/api/auth/login", json={"username": "view1", "password": "password123"})
    assert viewer.status_code == 200
    assert client.get("/api/workflow/outreach/drafts").status_code == 200
    denied = client.post(f"/api/workflow/outreach/drafts/{drafts[2]['id']}/approve", json={"note": "no"})
    assert denied.status_code == 403
    assert service.store.get_draft(drafts[2]["id"])["status"] == "pending"
