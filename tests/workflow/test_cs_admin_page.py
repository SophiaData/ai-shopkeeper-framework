"""/admin 客服页：治理侧栏、硬约束文案、复用 /api/workflow/cs，构建产物含入口。"""

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

CONSTRAINT = (
    "不外呼 TikTok 客服；发货/退款仍 403；不打开 TTS_WRITE_*；"
    "涉钱类（refund/compensation/price_match/complaint）必审。"
)


def _page_cs_source() -> str:
    text = SRC.read_text(encoding="utf-8")
    start = text.index("function pageCs()")
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
        }
    )


def test_cs_page_source_nav_and_constraints():
    page = _page_cs_source()
    main = SRC.read_text(encoding="utf-8")
    assert '{ id: "cs", label: "客服" }' in main
    # 客服在治理组，不在运营组
    gov = main.split('label: "治理"', 1)[1].split("],", 1)[0]
    assert '{ id: "cs", label: "客服" }' in gov
    assert "/api/workflow/cs/inbox" in main
    assert "/api/workflow/cs/draft" in main
    assert "/api/workflow/cs/drafts/" in main
    assert "/admin/approvals?id=" in page
    assert "data-cs-approval" in page
    assert "data-cs-send" in page
    assert "data-cs-draft-msg" in page
    assert "btn-refresh-cs" in page
    assert "待审草稿" in page
    assert "sent_stub" in page
    assert "dry_run" in page
    assert "白名单免审" in page
    assert "page-foot" in page
    assert "health-dot" in page
    assert CONSTRAINT in page
    for forbidden in (
        "btn-ship",
        "data-act=\"ship\"",
        "data-act=\"refund\"",
        "/orders/",
        "TTS_WRITE_ENABLED=1",
    ):
        assert forbidden not in page


def test_admin_cs_route_serves_spa(tmp_path: Path):
    service = _service(tmp_path)
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    page = client.get("/admin/cs")
    assert page.status_code == 200
    assert "text/html" in page.headers.get("content-type", "")
    assert "/admin/assets/" in page.text


def test_built_bundle_contains_cs_page():
    assets = DIST / "assets"
    assert assets.is_dir(), "缺少管理后台构建产物，请在 apps/admin 执行 npm run build"
    blobs = [p.read_text(encoding="utf-8", errors="ignore") for p in assets.glob("*.js")]
    assert blobs, "dist/assets 下没有 js"
    joined = "\n".join(blobs)
    assert "客服" in joined
    assert "/api/workflow/cs/inbox" in joined
    assert "不外呼 TikTok 客服" in joined
    assert "refund/compensation/price_match/complaint" in joined
    assert "TTS_WRITE_*" in joined


def test_admin_session_cs_whitelist_send_and_money_approval(tmp_path: Path):
    """正式后台会话下复用既有客服 API：白名单 stub、涉钱必审、viewer 不能写。"""
    service = _service(tmp_path)
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    create_admin_user(service.store, username="view1", password="password123", role="viewer")
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)

    login = client.post("/api/auth/login", json={"username": "owner1", "password": "password123"})
    assert login.status_code == 200, login.text

    inbox = client.post(
        "/api/workflow/cs/inbox",
        json={
            "conversation_id": "conv-admin-wl",
            "category": "logistics_query",
            "body": "พัสดุถึงไหนแล้วคะ",
            "lang": "th",
        },
    )
    assert inbox.status_code == 200, inbox.text
    listed = client.get("/api/workflow/cs/inbox")
    assert listed.status_code == 200
    assert listed.json()["count"] >= 1
    message_id = inbox.json()["message"]["id"]

    drafted = client.post("/api/workflow/cs/draft", json={"message_id": message_id})
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["whitelist"] is True
    assert drafted.json()["approval"] is None
    draft_id = drafted.json()["draft"]["id"]

    sent = client.post(f"/api/workflow/cs/drafts/{draft_id}/send")
    assert sent.status_code == 200, sent.text
    assert sent.json()["sent_stub"] is True
    assert sent.json()["dry_run"] is True
    assert sent.json()["draft"]["status"] == "sent_stub"

    money = client.post(
        "/api/workflow/cs/draft",
        json={
            "conversation_id": "conv-admin-refund",
            "category": "refund",
            "body": "ขอคืนเงิน",
        },
    )
    assert money.status_code == 200, money.text
    body = money.json()
    assert body["whitelist"] is False
    assert body["approval"]["action_type"] == "auto_reply"
    assert body["approval"]["status"] == "pending"
    approval_id = body["approval"]["id"]

    pending = client.get("/api/workflow/cs/drafts", params={"status": "pending_approval"})
    assert pending.status_code == 200
    ids = {row["id"] for row in pending.json()["drafts"]}
    assert body["draft"]["id"] in ids

    detail = client.get(f"/api/admin/approvals/{approval_id}")
    assert detail.status_code == 200
    assert detail.json()["action_type"] == "auto_reply"
    assert detail.json()["status"] == "pending"

    blocked = client.post(f"/api/workflow/cs/drafts/{body['draft']['id']}/send")
    assert blocked.status_code == 409

    assert client.post("/api/workflow/orders/ORD-X/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-X/refund").status_code == 403

    client.post("/api/auth/logout")
    viewer = client.post("/api/auth/login", json={"username": "view1", "password": "password123"})
    assert viewer.status_code == 200
    assert client.get("/api/workflow/cs/inbox").status_code == 200
    denied = client.post(
        "/api/workflow/cs/inbox",
        json={"conversation_id": "conv-view", "category": "complaint", "body": "no"},
    )
    assert denied.status_code == 403
