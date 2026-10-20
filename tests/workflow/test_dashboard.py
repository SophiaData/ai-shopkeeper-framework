"""工作台页面、只读快照和进程入口。审核规则沿用 review 路由。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService


def _service(tmp_path) -> WorkflowService:
    return WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
        }
    )


def test_dashboard_shows_offline_snapshot_and_blocks_writes(tmp_path):
    from fastapi import FastAPI

    app = FastAPI()
    mount_workflow(app, _service(tmp_path))
    client = TestClient(app)
    page = client.get("/workflow")
    assert page.status_code == 200
    assert "人工审核" in page.text
    status = client.get("/api/workflow/status").json()
    assert status["tiktok_mode"] == "offline"
    assert status["publish_enabled"] is False
    snapshot = client.get("/api/workflow/snapshot").json()
    assert snapshot["orders"] == []
    generated = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "video_script", "items": [{"name": "真空袋", "specs": "带单向阀"}]},
    )
    draft_id = generated.json()["drafts"][0]["id"]
    approved = client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "可以拍"})
    assert approved.status_code == 200
    assert approved.json()["published"] is False
    assert client.post(f"/api/workflow/drafts/{draft_id}/publish").status_code == 403
    listing = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "抽屉分隔盒", "specs": "外径 20cm，材质 PP"}]},
    )
    listing_id = listing.json()["drafts"][0]["id"]
    assert client.post(f"/api/workflow/drafts/{listing_id}/publish").status_code == 409
    assert client.post("/api/workflow/orders/1/ship").status_code == 403
    assert client.post("/api/workflow/orders/1/refund").status_code == 403
    report = client.post("/api/workflow/jobs/research")
    assert report.status_code == 200
    assert "不编造候选" in report.json()["body_md"]


def test_runtime_app_exposes_m1_and_workflow(tmp_path, monkeypatch):
    from shopkeeper.workflow.serve import create_runtime_app
    from tests.workflow.env_isolation import isolate_runtime_env

    # 与仓库 .env / 宿主 WORKFLOW_PLATFORM 隔离，固定为无凭据 offline。
    isolate_runtime_env(monkeypatch, tmp_path, platform=None)
    app = create_runtime_app(start_scheduler=False)
    client = TestClient(app)
    assert client.get("/tasks").status_code == 200
    assert client.get("/api/workflow/status").json()["tiktok_mode"] == "offline"
    page = client.get("/workflow")
    assert page.status_code == 200
    assert 'lines.join("\\n")' in page.text


def test_env_file_fills_blanks_without_overriding(tmp_path, monkeypatch):
    from shopkeeper.workflow.envfile import load_env_file
    from tests.workflow.env_isolation import isolate_runtime_env

    isolate_runtime_env(monkeypatch, tmp_path, platform=None)
    monkeypatch.setenv("WORKFLOW_LLM_PROVIDER", "mock")
    (tmp_path / ".env").write_text("WORKFLOW_LLM_PROVIDER=openai\nWORKFLOW_DEMO_FLAG=yes\n", encoding="utf-8")
    load_env_file()
    assert os.environ["WORKFLOW_LLM_PROVIDER"] == "mock"
    assert os.environ["WORKFLOW_DEMO_FLAG"] == "yes"


def test_workflow_api_requires_token_when_configured(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from tests.workflow.env_isolation import isolate_runtime_env

    isolate_runtime_env(monkeypatch, tmp_path, platform=None, WORKFLOW_API_TOKEN="desk-token")
    app = FastAPI()
    mount_workflow(app, _service(tmp_path))
    client = TestClient(app)
    denied = client.get("/api/workflow/status")
    assert denied.status_code == 401
    allowed = client.get("/api/workflow/status", headers={"X-Workflow-Token": "desk-token"})
    assert allowed.status_code == 200
    assert client.get("/workflow").status_code == 401
    page = client.get("/workflow", headers={"X-Workflow-Token": "desk-token"})
    assert page.status_code == 200
    assert "X-CSRF-Token" in page.text
    assert "desk-token" not in page.text


def test_workflow_api_rejects_non_loopback_without_token(monkeypatch):
    from shopkeeper.workflow.access import workflow_api_allowed

    monkeypatch.delenv("WORKFLOW_API_TOKEN", raising=False)
    assert workflow_api_allowed("127.0.0.1", "") is True
    assert workflow_api_allowed("testclient", "") is True
    assert workflow_api_allowed("10.1.2.3", "") is False
    monkeypatch.setenv("WORKFLOW_API_TOKEN", "desk-token")
    assert workflow_api_allowed("10.1.2.3", "desk-token") is True
    assert workflow_api_allowed("127.0.0.1", "wrong") is False


def test_compose_publishes_workflow_on_loopback():
    text = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "127.0.0.1:8780:8780" in text
    assert "8000:8000" not in text
    assert "0.0.0.0:8780" not in text
    assert "restart: unless-stopped" in text
    assert "/healthz" in text
    assert "TTS_WRITE_ENABLED" in text
    assert "BACKUP_DAILY" in text


def test_dockerfile_runs_as_non_root_with_healthcheck():
    text = Path("Dockerfile").read_text(encoding="utf-8")
    assert "USER shopkeeper" in text
    assert "HEALTHCHECK" in text
    assert "/healthz" in text
    assert "WORKFLOW_PORT=8780" in text
    assert "EXPOSE 8780" in text
