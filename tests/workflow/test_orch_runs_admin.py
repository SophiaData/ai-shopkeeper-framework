"""编排运行只读投影：步骤持久化、时间线、错误数据、审批深链。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform, MockProductSource
from shopkeeper.orchestrator.definition import PIPELINE_V1
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.orchestrator.store import STEP_FAILED, OrchestratorStore
from shopkeeper.workflow.auth import create_admin_user
from shopkeeper.workflow.orch_runs_view import build_orch_run_detail
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/600123456789.html"
LEAK_TOKEN = "leak-token-value"
LEAK_PHONE = "13800138000"
LEAK_SNAPSHOT = "raw-snapshot-marker"


def _service(tmp_path: Path) -> WorkflowService:
    return WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "WORKFLOW_PLATFORM": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
            "ARTIFACTS_DIR": str(tmp_path / "artifacts"),
        }
    )


def _client(service: WorkflowService) -> TestClient:
    app = FastAPI()
    mount_workflow(app, service)
    return TestClient(app)


def _login(client: TestClient, username: str, password: str = "password123") -> None:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text


def test_failed_step_persists_and_errors_are_data_without_secrets(tmp_path: Path):
    path = tmp_path / "workflow.db"
    store = WorkflowStore(path)
    orch = OrchestratorStore(store)
    created = orch.create_run(PIPELINE_V1, {"url": FIXTURE_URL, "token": LEAK_TOKEN})
    run_id = created["id"]
    orch.update_step(
        run_id,
        "scout",
        status=STEP_FAILED,
        input_payload={"run_input": {"url": FIXTURE_URL, "token": LEAK_TOKEN}},
        output={
            "facts": {
                "title_raw": "抽屉分隔盒",
                "phone": LEAK_PHONE,
                "raw_snapshot": {"token": LEAK_SNAPSHOT},
            },
            "token": LEAK_TOKEN,
            "recommend": False,
        },
        error={
            "code": "P1688_FETCH_FAILED",
            "message": "采集失败",
            "api_key": LEAK_TOKEN,
            "phone": LEAK_PHONE,
        },
        mark_started=True,
        mark_finished=True,
    )
    orch.update_run(
        run_id,
        status="failed",
        current_step="scout",
        error={"code": "P1688_FETCH_FAILED", "message": "采集失败", "api_key": LEAK_TOKEN},
        finished=True,
    )
    del orch, store

    reopened = WorkflowStore(path)
    with reopened._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM orch_steps WHERE run_id = ?",
            (run_id,),
        ).fetchone()["n"]
    assert count == len(PIPELINE_V1.steps)

    detail = build_orch_run_detail(reopened, run_id)
    assert detail is not None
    assert detail["read_only"] is True
    blob = json.dumps(detail, ensure_ascii=False)
    for secret in (LEAK_TOKEN, LEAK_PHONE, LEAK_SNAPSHOT):
        assert secret not in blob
    assert "api_key" not in blob
    assert "raw_snapshot" not in blob

    assert detail["run"]["display_status"] == "failed"
    assert detail["run"]["display_label"] == "失败"
    assert detail["run"]["stopped_step"] == "scout"
    assert detail["run"]["stopped_step_label"] == "选品官"
    assert detail["run"]["product"]["title"] == "抽屉分隔盒"
    assert detail["run"]["duration_seconds"] is not None
    assert detail["run"]["error"] == {"code": "P1688_FETCH_FAILED", "message": "采集失败"}

    by_id = {step["step_id"]: step for step in detail["timeline"]}
    assert list(by_id) == [step.id for step in PIPELINE_V1.steps]
    scout = by_id["scout"]
    assert scout["display_status"] == "failed"
    assert scout["error"] == {"code": "P1688_FETCH_FAILED", "message": "采集失败"}
    assert scout["input_summary"] == [{"label": "链接", "value": FIXTURE_URL}]
    assert {"label": "商品", "value": "抽屉分隔盒"} in scout["output_summary"]
    assert scout["llm"]["status"] == "missing"
    assert scout["llm"]["label"] == "缺数据"
    assert scout["llm"]["prompt_tokens"] is None
    pending = by_id["publish"]
    assert pending["display_status"] == "pending"
    assert pending["display_label"] == "未开始"


def test_awaiting_approval_links_to_review_queue(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.db")
    orch = Orchestrator(
        store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env={"LLM_CHANNEL": "mock", "ORCHESTRATOR_MOCK_LLM_COST_USD": "0"},
        sleep_fn=lambda _s: None,
    )
    detail = orch.start(url=FIXTURE_URL, advance=True)
    assert detail["status"] == "awaiting_approval"
    view = build_orch_run_detail(store, detail["id"])
    assert view is not None
    assert view["run"]["display_status"] == "waiting_approval"
    assert view["run"]["display_label"] == "等待人工审批"
    assert view["run"]["stopped_step"] == "human_approval"
    pending = view["run"]["pending_approval"]
    assert pending["pending"] is True
    assert pending["status"] == "pending"
    assert pending["href"] == f"/admin/approvals?id={pending['id']}"
    gate = next(step for step in view["timeline"] if step["step_id"] == "human_approval")
    assert gate["display_status"] == "waiting_approval"
    assert gate["approval"]["href"] == pending["href"]
    assert gate["approval"]["pending"] is True
    scout = next(step for step in view["timeline"] if step["step_id"] == "scout")
    assert scout["llm"]["status"] == "recorded"
    assert scout["llm"]["prompt_tokens"] == 100
    assert scout["llm"]["completion_tokens"] == 50
    qa = next(step for step in view["timeline"] if step["step_id"] == "qa")
    assert qa["llm"]["status"] == "missing"
    assert qa["llm"]["cost_usd"] is None


def test_demo_orch_timeline_api_is_readonly_and_lists_every_step(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TTS_WRITE_ENABLED", "1")
    service = _service(tmp_path)
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    create_admin_user(service.store, username="view1", password="password123", role="viewer")
    client = _client(service)

    assert client.get("/api/admin/runs").status_code == 401
    assert client.get("/api/admin/runs/missing").status_code == 401

    _login(client, "view1")
    empty = client.get("/api/admin/runs")
    assert empty.status_code == 200
    assert empty.json()["read_only"] is True
    assert empty.json()["runs"] == []
    missing = client.get("/api/admin/runs/does-not-exist")
    assert missing.status_code == 404

    client.post("/api/auth/logout")
    _login(client, "owner1")
    bad_shop = client.get("/api/admin/runs", params={"shop_id": "not-a-shop"})
    assert bad_shop.status_code == 400

    demo = client.post("/api/admin/demo-orch", json={})
    assert demo.status_code == 200, demo.text
    body = demo.json()
    assert body["href"] == f"/admin/runs?run={body['run_id']}"
    assert os.environ["TTS_WRITE_ENABLED"] == "1"
    product_title = MockProductSource().fetch(FIXTURE_URL).data.title_raw

    listed = client.get("/api/admin/runs").json()
    assert listed["runs"][0]["id"] == body["run_id"]
    assert listed["runs"][0]["display_status"] == "ok"
    assert listed["runs"][0]["display_label"] == "成功"
    assert listed["runs"][0]["product"]["title"] == product_title
    assert listed["runs"][0]["stopped_step"] == "cs"
    assert listed["runs"][0]["stopped_step_label"] == "客服"
    assert isinstance(listed["runs"][0]["duration_seconds"], int)
    assert listed["runs"][0]["pending_approval"] is None

    fresh = WorkflowStore(service.store.path)
    with fresh._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM orch_steps WHERE run_id = ?",
            (body["run_id"],),
        ).fetchone()["n"]
    assert count == len(PIPELINE_V1.steps)

    detail = client.get(f"/api/admin/runs/{body['run_id']}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["read_only"] is True
    ids = [step["step_id"] for step in payload["timeline"]]
    assert ids == [step.id for step in PIPELINE_V1.steps]
    assert all(step["display_status"] == "ok" for step in payload["timeline"])
    labels = {step["step_id"]: step["label"] for step in payload["timeline"]}
    assert labels["script"] == "短视频脚本"
    assert labels["outreach"] == "达人建联"
    assert labels["human_approval"] == "人工审批"
    scout = next(step for step in payload["timeline"] if step["step_id"] == "scout")
    assert any(item["value"] == product_title for item in scout["output_summary"])
    assert scout["llm"]["status"] == "recorded"
    assert scout["llm"]["prompt_tokens"] == 100
    qa = next(step for step in payload["timeline"] if step["step_id"] == "qa")
    assert qa["llm"]["status"] == "missing"
    assert qa["llm"]["prompt_tokens"] is None
    script = next(step for step in payload["timeline"] if step["step_id"] == "script")
    assert any(item["label"] == "Hook" for item in script["output_summary"])
    outreach = next(step for step in payload["timeline"] if step["step_id"] == "outreach")
    assert any(item["label"] == "草稿数" and item["value"] != "0" for item in outreach["output_summary"])
    assert any(item["label"] == "已发送" and item["value"] == "否" for item in outreach["output_summary"])
    gate = next(step for step in payload["timeline"] if step["step_id"] == "human_approval")
    assert gate["approval"]["href"].startswith("/admin/approvals?id=")
    assert gate["approval"]["pending"] is False
    assert "phone" not in json.dumps(payload, ensure_ascii=False)
