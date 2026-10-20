"""管理后台登录、限流、角色、审批人记录、未登录 401。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.workflow.auth import create_admin_user, create_session, hash_password, verify_password
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService
from shopkeeper.workflow.store import WorkflowStore


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


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowStore, WorkflowService]:
    service = _service(tmp_path)
    app = FastAPI()
    mount_workflow(app, service)
    return TestClient(app), service.store, service


def _login(client: TestClient, username: str, password: str) -> dict:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return res.json()


def test_password_hash_roundtrip():
    encoded = hash_password("correct-horse")
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("correct-horse", encoded)
    assert not verify_password("wrong-password", encoded)


def test_login_logout_and_me(tmp_path: Path):
    client, store, _service = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")

    denied = client.get("/api/auth/me")
    assert denied.status_code == 401

    body = _login(client, "owner1", "password123")
    assert body["user"]["username"] == "owner1"
    assert body["user"]["role"] == "owner"
    assert body["csrf_token"]

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "owner1"

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_wrong_password_rate_limited(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ADMIN_LOGIN_MAX_FAILURES", "3")
    monkeypatch.setenv("ADMIN_LOGIN_WINDOW_SECONDS", "900")
    client, store, _service = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")

    for _ in range(3):
        bad = client.post("/api/auth/login", json={"username": "owner1", "password": "wrong-pass"})
        assert bad.status_code == 401
    blocked = client.post("/api/auth/login", json={"username": "owner1", "password": "wrong-pass"})
    assert blocked.status_code == 429
    # 正确密码在限流窗口内也被挡
    still = client.post("/api/auth/login", json={"username": "owner1", "password": "password123"})
    assert still.status_code == 429


def test_admin_api_requires_login(tmp_path: Path):
    client, store, _service = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")
    assert client.get("/api/admin/overview").status_code == 401
    _login(client, "owner1", "password123")
    ok = client.get("/api/admin/overview")
    assert ok.status_code == 200
    assert "pending_approvals" in ok.json()


def test_viewer_cannot_write_operator_cannot_approve(tmp_path: Path):
    client, store, _service = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")
    create_admin_user(store, username="ops1", password="password123", role="operator")
    create_admin_user(store, username="view1", password="password123", role="viewer")

    # 先用 owner 生成待审
    _login(client, "owner1", "password123")
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "盒", "specs": "PP"}]},
    )
    assert created.status_code == 200, created.text
    approval_id = created.json()["drafts"][0]["payload"]["approval_id"]
    client.post("/api/auth/logout")

    # viewer 读可以，写不行
    _login(client, "view1", "password123")
    assert client.get("/api/admin/overview").status_code == 200
    denied = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "x", "specs": "y"}]},
    )
    assert denied.status_code == 403
    client.post("/api/auth/logout")

    # operator 可生成，不可审批
    _login(client, "ops1", "password123")
    gen = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "video_script", "items": [{"name": "脚本", "specs": "x"}]},
    )
    assert gen.status_code == 200
    blocked = client.post(
        f"/api/workflow/approvals/{approval_id}/approve",
        json={"note": "想过"},
    )
    assert blocked.status_code == 403
    client.post("/api/auth/logout")

    # owner 可通过，decided_by 写入账号；API 必须带展示时 hash
    _login(client, "owner1", "password123")
    no_hash = client.post(
        f"/api/workflow/approvals/{approval_id}/approve",
        json={"note": "没有 hash"},
    )
    assert no_hash.status_code == 400
    legacy_no_hash = client.post(
        f"/approvals/{approval_id}/approve",
        json={"note": "legacy 没有 hash"},
    )
    assert legacy_no_hash.status_code == 400
    ok = client.post(
        f"/api/workflow/approvals/{approval_id}/approve",
        json={"note": "可以上", "expected_hash": store.get_approval(approval_id)["payload_hash"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved"
    assert ok.json()["decided_by"] == "owner1"


def test_reject_requires_note_and_records_actor(tmp_path: Path):
    client, store, _service = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")
    _login(client, "owner1", "password123")
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "袋", "specs": "PE"}]},
    )
    approval_id = created.json()["drafts"][0]["payload"]["approval_id"]
    missing = client.post(f"/api/workflow/approvals/{approval_id}/reject", json={"note": ""})
    assert missing.status_code == 400
    ok = client.post(
        f"/api/workflow/approvals/{approval_id}/reject",
        json={"note": "文案夸大", "expected_hash": store.get_approval(approval_id)["payload_hash"]},
    )
    assert ok.status_code == 200
    assert ok.json()["decided_by"] == "owner1"
    assert ok.json()["decision_note"] == "文案夸大"


def test_machine_token_still_works_when_admins_exist(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WORKFLOW_API_TOKEN", "machine-secret")
    client, store, _service = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")
    headers = {"X-Workflow-Token": "machine-secret"}
    created = client.post(
        "/api/workflow/drafts/generate",
        headers=headers,
        json={"kind": "listing", "items": [{"name": "盒", "specs": "x"}]},
    )
    assert created.status_code == 200, created.text
    approval_id = created.json()["drafts"][0]["payload"]["approval_id"]
    ok = client.post(
        f"/approvals/{approval_id}/approve",
        headers=headers,
        json={"note": "machine"},
    )
    assert ok.status_code == 200
    assert ok.json()["decided_by"] == "machine"


def test_unauthenticated_write_401_after_admin_exists(tmp_path: Path):
    client, store, _service = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")
    # TestClient host 是 testclient；有管理员后纯本机写应 401
    denied = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "x", "specs": "y"}]},
    )
    assert denied.status_code == 401


def test_admin_page_and_overview_empty_state(tmp_path: Path):
    client, store, _service = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")
    page = client.get("/admin/")
    assert page.status_code == 200
    assert "AI 店长" in page.text or "admin" in page.text.lower()
    _login(client, "owner1", "password123")
    overview = client.get("/api/admin/overview").json()
    assert overview["pending_approvals"] == 0
    assert overview["empty"] is True
    assert isinstance(overview["alerts"], list)
    assert isinstance(overview["components"], list) and overview["components"]
    assert "banner" in overview
    assert isinstance(overview["switches"], list)


def test_workflow_pages_reject_remote_anonymous_and_never_render_machine_token(tmp_path, monkeypatch):
    import uuid
    from shopkeeper.workflow.routes import render_workflow_page

    machine_token = uuid.uuid4().hex
    monkeypatch.setenv("WORKFLOW_API_TOKEN", machine_token)
    service = _service(tmp_path)
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app, client=("203.0.113.20", 12345))
    for path in ("/workflow", "/workflow/catalog", "/workflow/profit"):
        denied = client.get(path)
        assert denied.status_code == 401
        allowed = client.get(path, headers={"X-Workflow-Token": machine_token})
        assert allowed.status_code == 200
        assert machine_token not in allowed.text
    assert machine_token not in render_workflow_page()


def test_workflow_page_uses_session_without_elevating_viewer(tmp_path, monkeypatch):
    import uuid

    machine_token = uuid.uuid4().hex
    monkeypatch.setenv("WORKFLOW_API_TOKEN", machine_token)
    client, store, _ = _client(tmp_path)
    create_admin_user(store, username="reader", password="password123", role="viewer")
    assert client.get("/workflow").status_code == 401
    _login(client, "reader", "password123")
    page = client.get("/workflow")
    assert page.status_code == 200
    assert machine_token not in page.text
    assert "X-CSRF-Token" in page.text
    denied = client.post("/api/workflow/drafts/generate", json={
        "kind": "listing", "items": [{"name": "box", "specs": "PP"}],
    })
    assert denied.status_code == 403


def test_workflow_page_requires_session_after_local_admin_created(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKFLOW_API_TOKEN", raising=False)
    client, store, _ = _client(tmp_path)
    assert client.get("/workflow").status_code == 200
    create_admin_user(store, username="owner", password="password123", role="owner")
    assert client.get("/workflow").status_code == 401
    _login(client, "owner", "password123")
    assert client.get("/workflow").status_code == 200

def test_stock_alias_requires_auth_and_operator_cannot_approve_outreach(tmp_path: Path) -> None:
    from shopkeeper.orchestrator.outreach import DRAFT_KIND

    client, store, _service = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")
    create_admin_user(store, username="ops1", password="password123", role="operator")

    anonymous = client.post("/listings/demo/stock-push", json={"desired_qty": 1})
    assert anonymous.status_code == 401

    _login(client, "ops1", "password123")
    store.add_draft(
        kind=DRAFT_KIND,
        status="pending",
        title="达人A",
        body_th="สวัสดี",
        body_en="Hello",
        payload={"creator": "@a"},
        qa={"ok": True},
        source="test",
    )
    draft = store.list_drafts()[0]
    blocked = client.post(
        f"/api/workflow/outreach/drafts/{draft['id']}/approve",
        json={"note": "operator 不能批"},
    )
    assert blocked.status_code == 403
    assert store.get_draft(draft["id"])["status"] == "pending"

def test_approval_expected_hash_rejects_changed_content(tmp_path: Path) -> None:
    client, store, _service = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")
    approval = store.add_approval(
        {
            "action_type": "dropship_order",
            "entity_type": "dropship_draft",
            "entity_id": "d1",
            "shop_id": "default",
            "payload": {"qty": 1},
            "payload_hash": "hash-a",
            "decide_before": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
    )
    store.update_pending_approval_snapshot(
        approval["id"], {"qty": 2}, hash_value="hash-b",
    )
    _login(client, "owner1", "password123")
    stale = client.post(
        f"/api/workflow/approvals/{approval['id']}/approve",
        json={"note": "批准旧内容", "expected_hash": "hash-a"},
    )
    assert stale.status_code == 409
    assert store.get_approval(approval["id"])["status"] == "pending"
    fresh = client.post(
        f"/api/workflow/approvals/{approval['id']}/approve",
        json={"note": "批准新内容", "expected_hash": "hash-b"},
    )
    assert fresh.status_code == 200, fresh.text
