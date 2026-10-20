"""编排器 API 纳入管理后台登录保护；审批节点仅 owner 并记录审批人。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.workflow.auth import create_admin_user
from shopkeeper.workflow.migrate import CURRENT_SCHEMA_VERSION, get_schema_version
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/600123456789.html"


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowStore]:
    service = WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "WORKFLOW_PLATFORM": "mock",
            "LLM_CHANNEL": "mock",
            "ORCHESTRATOR_MOCK_LLM_COST_USD": "0",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
        }
    )
    app = FastAPI()
    mount_workflow(app, service)
    return TestClient(app), service.store


def _login(client: TestClient, username: str, password: str) -> None:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text


def test_migrate_v4_admin_after_orch(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    with store._connect() as conn:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        assert CURRENT_SCHEMA_VERSION >= 4
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "orch_runs" in tables
    assert "admin_users" in tables
    assert "pii_purge_audit" in tables


def test_orchestrator_write_requires_login_when_admins_exist(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")
    denied = client.post("/api/orchestrator/runs", json={"url": FIXTURE_URL})
    assert denied.status_code == 401


def test_orchestrator_approve_owner_only_and_records_actor(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")
    create_admin_user(store, username="ops1", password="password123", role="operator")

    _login(client, "owner1", "password123")
    started = client.post("/api/orchestrator/runs", json={"url": FIXTURE_URL})
    assert started.status_code == 200, started.text
    run_id = started.json()["id"]
    assert started.json()["status"] == "awaiting_approval"
    approval_id = started.json()["context"]["approval_id"]
    client.post("/api/auth/logout")

    _login(client, "ops1", "password123")
    blocked = client.post(
        f"/api/orchestrator/runs/{run_id}/approve",
        json={"note": "想过"},
    )
    assert blocked.status_code == 403
    client.post("/api/auth/logout")

    _login(client, "owner1", "password123")
    ok = client.post(
        f"/api/orchestrator/runs/{run_id}/approve",
        json={"note": "店主批准"},
    )
    assert ok.status_code == 200, ok.text
    row = store.get_approval(approval_id)
    assert row is not None
    assert row["decided_by"] == "owner1"
    # approve 后续 advance 可能已 CLAIM→executed（mock 上架）
    assert row["status"] in {"approved", "executing", "executed"}


def test_orchestrator_timeline_has_io_and_cost(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    create_admin_user(store, username="owner1", password="password123", role="owner")
    _login(client, "owner1", "password123")
    started = client.post("/api/orchestrator/runs", json={"url": FIXTURE_URL})
    run_id = started.json()["id"]
    detail = client.get(f"/api/orchestrator/runs/{run_id}")
    assert detail.status_code == 200
    body = detail.json()
    timeline = body.get("timeline") or body.get("steps") or []
    assert timeline
    scout = next(s for s in timeline if s["step_id"] == "scout")
    assert "input" in scout and "output" in scout
    assert "ai_trace" in scout
    assert "cost_usd" in scout
    assert scout.get("rationale") or any(
        isinstance(t, dict) and t.get("rationale") for t in (scout.get("ai_trace") or [])
    )
