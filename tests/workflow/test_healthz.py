"""GET /healthz：无鉴权探活，200 且无密钥字段。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper import __version__
from shopkeeper.workflow.routes import HEALTHZ_KEYS, mount_workflow
from shopkeeper.workflow.service import WorkflowService


def _service(tmp_path) -> WorkflowService:
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


_SECRETISH = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "private_key",
    "encrypt_key",
    "webhook",
)


def test_healthz_returns_200_without_secret_fields(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKFLOW_API_TOKEN", raising=False)
    app = FastAPI()
    mount_workflow(app, _service(tmp_path))
    client = TestClient(app)

    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == HEALTHZ_KEYS
    assert body["ok"] is True
    assert body["version"] == __version__
    assert body["publish_enabled"] is True
    lowered = {str(k).lower() for k in body}
    for needle in _SECRETISH:
        assert needle not in lowered
        for key, value in body.items():
            assert needle not in str(key).lower()
            assert needle not in str(value).lower()


def test_healthz_skips_workflow_token_gate(tmp_path, monkeypatch):
    """即使配置了 WORKFLOW_API_TOKEN，探活仍无需口令。"""
    monkeypatch.setenv("WORKFLOW_API_TOKEN", "desk-token")
    app = FastAPI()
    mount_workflow(app, _service(tmp_path))
    client = TestClient(app)

    assert client.get("/api/workflow/status").status_code == 401
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert "token" not in resp.text.lower()


def test_runtime_app_exposes_healthz(tmp_path, monkeypatch):
    from shopkeeper.workflow.serve import create_runtime_app
    from tests.workflow.env_isolation import isolate_runtime_env

    isolate_runtime_env(monkeypatch, tmp_path, platform="mock")
    app = create_runtime_app(start_scheduler=False)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["publish_enabled"] is True
