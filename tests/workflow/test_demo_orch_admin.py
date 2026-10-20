"""POST /api/admin/demo-orch：写入本服务工作台库，强制 mock，CSRF + 角色。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.orchestrator.demo import DEMO_ENV_FORCE, FIXTURE_URL
from shopkeeper.orchestrator.definition import PIPELINE_V1
from shopkeeper.orchestrator.store import RUN_SUCCEEDED, STEP_SUCCEEDED
from shopkeeper.workflow.auth import create_admin_user
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService


def _service(tmp_path: Path, env: dict[str, str] | None = None) -> WorkflowService:
    base = {
        "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
        "WORKFLOW_LLM_PROVIDER": "mock",
        "WORKFLOW_PLATFORM": "mock",
        "RESEARCH_INPUT_PATH": "",
        "DAILY_METRICS_PATH": "",
        "WORKFLOW_WEBHOOK_URL": "",
        "ARTIFACTS_DIR": str(tmp_path / "artifacts"),
    }
    if env:
        base.update(env)
    return WorkflowService.from_env(base)


def _client(tmp_path: Path, **env: str) -> tuple[TestClient, WorkflowService]:
    service = _service(tmp_path, env or None)
    app = FastAPI()
    mount_workflow(app, service)
    return TestClient(app), service


def _login(client: TestClient, username: str, password: str) -> dict:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return res.json()


def test_demo_orch_owner_writes_live_db_and_forces_mock(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TTS_WRITE_ENABLED", "1")
    monkeypatch.setenv("WORKFLOW_PLATFORM", "tiktok_live")
    monkeypatch.setenv("LLM_CHANNEL", "gateway")
    client, service = _client(tmp_path)
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    _login(client, "owner1", "password123")

    res = client.post("/api/admin/demo-orch", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["status"] == RUN_SUCCEEDED
    assert body["forced_mock"] is True
    assert body["tts_write_enabled"] == "0"
    assert body["env"]["WORKFLOW_PLATFORM"] == "mock"
    assert body["env"]["TTS_WRITE_ENABLED"] == "0"
    assert body["env"]["LLM_CHANNEL"] == "mock"
    assert body["href"] == f"/admin/runs?run={body['run_id']}"
    for sid in [s.id for s in PIPELINE_V1.steps]:
        assert body["step_statuses"][sid] == STEP_SUCCEEDED, sid

    # 进程 env 不被演示改开/改关
    assert os.environ["TTS_WRITE_ENABLED"] == "1"
    assert os.environ["WORKFLOW_PLATFORM"] == "tiktok_live"
    assert os.environ["LLM_CHANNEL"] == "gateway"
    for key in ("WORKFLOW_PLATFORM", "WORKFLOW_LLM_PROVIDER", "TTS_WRITE_ENABLED", "LLM_CHANNEL"):
        assert body["env"][key] == DEMO_ENV_FORCE[key]

    run = client.get(f"/api/orchestrator/runs/{body['run_id']}")
    assert run.status_code == 200
    assert run.json()["status"] == RUN_SUCCEEDED
    assert run.json()["input"]["url"] == FIXTURE_URL


def test_demo_orch_viewer_forbidden_and_missing_csrf(tmp_path: Path):
    client, service = _client(tmp_path)
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    create_admin_user(service.store, username="view1", password="password123", role="viewer")

    denied = client.post("/api/admin/demo-orch", json={})
    assert denied.status_code == 401

    _login(client, "view1", "password123")
    viewer = client.post("/api/admin/demo-orch", json={})
    assert viewer.status_code == 403
    assert "只读" in viewer.text

    client.post("/api/auth/logout")
    _login(client, "owner1", "password123")
    bad_csrf = client.post(
        "/api/admin/demo-orch",
        json={},
        headers={"X-CSRF-Token": "not-the-session-csrf"},
    )
    assert bad_csrf.status_code == 403
    assert "CSRF" in bad_csrf.text


def test_demo_orch_operator_allowed(tmp_path: Path):
    client, service = _client(tmp_path)
    create_admin_user(service.store, username="ops1", password="password123", role="operator")
    _login(client, "ops1", "password123")
    res = client.post("/api/admin/demo-orch", json={"auto_approve": True})
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert res.json()["run_id"]
