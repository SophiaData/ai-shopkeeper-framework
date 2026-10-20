"""各 agent 产出统计：只读接口，数字来自编排器与审批表。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform, MockProductSource
from shopkeeper.orchestrator.demo import FIXTURE_URL, run_demo_orch
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.workflow.agent_stats import suspend_reason
from shopkeeper.workflow.auth import create_admin_user
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService

URL = "https://detail.1688.com/offer/600123456789.html"


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


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowService]:
    service = _service(tmp_path)
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    return client, service


def _login(client: TestClient) -> None:
    res = client.post("/api/auth/login", json={"username": "owner1", "password": "password123"})
    assert res.status_code == 200, res.text


def _orch(service: WorkflowService, *, env: dict[str, str] | None = None) -> Orchestrator:
    merged = {
        "LLM_CHANNEL": "mock",
        "ORCHESTRATOR_MOCK_LLM_COST_USD": "0",
        "WORKFLOW_PLATFORM": "mock",
        "TTS_WRITE_ENABLED": "0",
        **(env or {}),
    }
    return Orchestrator(
        service.store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env=merged,
        sleep_fn=lambda _s: None,
    )


def _agent(body: dict, agent_id: str) -> dict:
    for row in body["agents"]:
        if row["id"] == agent_id:
            return row
    raise AssertionError(agent_id)


def test_stats_requires_login(tmp_path: Path):
    client, _service = _client(tmp_path)
    denied = client.get("/api/admin/agents/stats")
    assert denied.status_code == 401


def test_stats_post_is_not_a_write_api(tmp_path: Path):
    client, _service = _client(tmp_path)
    _login(client)
    res = client.post("/api/admin/agents/stats", json={})
    assert res.status_code == 405


def test_empty_stats_marks_llm_missing_and_lists_pipeline(tmp_path: Path):
    client, _service = _client(tmp_path)
    _login(client)
    res = client.get("/api/admin/agents/stats")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["run_count"] == 0
    assert body["runs"] == []
    assert body["sources"] == ["orch_runs", "orch_steps", "orch_llm_usage", "approvals"]
    ids = [row["id"] for row in body["agents"]]
    assert ids[:6] == ["scout", "pricing", "stylist", "qa", "script", "outreach"]
    assert _agent(body, "script")["name"] == "短视频脚本"
    assert _agent(body, "outreach")["name"] == "达人建联"
    assert "cs" in ids
    scout = _agent(body, "scout")
    assert scout["counts"]["runs"] == 0
    assert scout["health"] == "idle"
    assert scout["llm"]["status"] == "missing"
    assert scout["llm"]["label"] == "缺数据"
    assert scout["llm"]["cost_usd"] is None
    assert body["task_logs"]["business_tasks"]["label"] == "缺数据"
    assert body["budget"]["spent_in_range_usd"] == 0


def test_invalid_date_and_range_and_unknown_shop(tmp_path: Path):
    client, _service = _client(tmp_path)
    _login(client)
    bad = client.get("/api/admin/agents/stats", params={"from": "yesterday"})
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "AGENT_STATS_DATE_INVALID"
    backwards = client.get("/api/admin/agents/stats", params={"from": "2026-10-13", "to": "2026-10-01"})
    assert backwards.status_code == 400
    assert backwards.json()["detail"]["code"] == "AGENT_STATS_DATE_INVALID"
    unknown = client.get("/api/admin/agents/stats", params={"shop_id": "not-a-shop"})
    assert unknown.status_code == 400
    assert unknown.json()["detail"]["code"] == "SHOP_UNKNOWN"


def test_pipeline_to_approval_is_visible(tmp_path: Path):
    client, service = _client(tmp_path)
    detail = _orch(service).start(url=URL, advance=True)
    assert detail["status"] == "awaiting_approval"
    _login(client)
    body = client.get("/api/admin/agents/stats").json()
    assert body["run_count"] == 1
    scout = _agent(body, "scout")
    assert scout["counts"]["runs"] == 1
    assert scout["counts"]["succeeded"] == 1
    assert scout["llm"]["status"] == "recorded"
    assert scout["llm"]["label"] is None
    assert scout["llm"]["prompt_tokens"] >= 100
    assert scout["llm"]["cost_usd"] == 0
    assert any("选品报告" in art["label"] for art in scout["recent_artifacts"])
    stylist = _agent(body, "stylist")
    assert stylist["counts"]["succeeded"] == 1
    assert stylist["llm"]["status"] == "recorded"
    qa = _agent(body, "qa")
    assert qa["counts"]["succeeded"] == 1
    assert qa["llm"]["status"] == "missing"
    assert qa["llm"]["label"] == "缺数据"
    gate = _agent(body, "human_approval")
    assert gate["counts"]["awaiting_approval"] == 1
    assert gate["approvals"]["pending"] >= 1
    assert body["approvals"]["pending"] >= 1
    run = body["runs"][0]
    assert run["id"] == detail["id"]
    assert run["current_step"] == "human_approval"
    assert run["suspend_reason"] == "待审批"
    assert run["suspend_code"] == "awaiting_approval"

    timeline = client.get(f"/api/admin/agents/runs/{detail['id']}")
    assert timeline.status_code == 200
    steps = timeline.json()["steps"]
    assert [s["step_id"] for s in steps][0] == "scout"
    assert steps[0]["started_at"]
    assert any(s["step_id"] == "human_approval" and s["status"] == "awaiting_approval" for s in steps)
    script_step = next(s for s in steps if s["step_id"] == "script")
    assert script_step["label"] == "短视频脚本"
    assert script_step["status"] == "succeeded"

    missing = client.get("/api/admin/agents/runs/does-not-exist")
    assert missing.status_code == 404

    # 日期窗外不应把这条 run 算进来
    empty = client.get("/api/admin/agents/stats", params={"from": "2000-01-01", "to": "2000-01-02"})
    assert empty.status_code == 200
    assert empty.json()["run_count"] == 0
    assert _agent(empty.json(), "scout")["counts"]["runs"] == 0
    assert _agent(empty.json(), "scout")["llm"]["label"] == "缺数据"


def test_qa_reject_budget_and_tts_reasons(tmp_path: Path):
    client, service = _client(tmp_path)
    qa_run = _orch(service).start(url=URL, extra_input={"force_qa_fail": True}, advance=True)
    assert qa_run["status"] == "awaiting_approval"
    _orch(service, env={"LLM_DAILY_BUDGET_USD": "0"}).start(url=URL, advance=True)
    blocked = _orch(service).start(
        url=URL,
        extra_input={"force_tts_write_blocked": True},
        advance=True,
    )
    blocked = _orch(service).approve(blocked["id"], note="测试批准，不真写")
    assert blocked["status"] == "suspended"
    assert (blocked.get("error") or {}).get("code") == "TTS_WRITE_BLOCKED"

    _login(client)
    body = client.get("/api/admin/agents/stats").json()
    assert _agent(body, "stylist")["counts"]["qa_rejected"] >= 1
    assert _agent(body, "qa")["counts"]["qa_rejected"] >= 1
    scout_runs = [r for r in body["runs"] if r["suspend_reason"] == "预算"]
    assert scout_runs and scout_runs[0]["suspend_code"] == "budget"
    tts_runs = [r for r in body["runs"] if r["id"] == blocked["id"]]
    assert tts_runs[0]["suspend_reason"] == "TTS 挂起"
    assert tts_runs[0]["current_step"] == "publish"
    assert _agent(body, "publish")["counts"]["suspended"] >= 1

    # 纯函数：QA 打回码映射（编排器在同一轮 advance 里会把可重写的 QA 打回消化掉）
    assert suspend_reason({"status": "failed", "error": {"code": "ORCH_QA_EXHAUSTED"}, "context": {}})[
        "label"
    ] == "QA 打回"
    assert suspend_reason({"status": "running", "error": None, "context": {"needs_rewrite": True}})[
        "label"
    ] == "QA 打回"


def test_demo_orch_populates_real_counts(tmp_path: Path):
    """mock demo-orch 写入同一工作台库后，统计接口必须出现真实非零数字。"""
    db = tmp_path / "workflow.db"
    summary = run_demo_orch(url=FIXTURE_URL, out_dir=tmp_path / "demo-out", db_path=db, auto_approve=True)
    assert summary["ok"] is True
    service = WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(db),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "WORKFLOW_PLATFORM": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
        }
    )
    create_admin_user(service.store, username="owner1", password="password123", role="viewer")
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    _login(client)
    body = client.get("/api/admin/agents/stats").json()
    assert body["run_count"] == 1
    for agent_id in (
        "scout",
        "pricing",
        "stylist",
        "qa",
        "script",
        "outreach",
        "publish",
        "order_sync",
        "dropship",
        "tracking",
        "cs",
    ):
        row = _agent(body, agent_id)
        assert row["counts"]["succeeded"] >= 1, agent_id
    assert _agent(body, "scout")["llm"]["prompt_tokens"] > 0
    assert _agent(body, "cs")["llm"]["label"] == "缺数据"
    assert body["approvals"]["approved"] >= 1
    run = body["runs"][0]
    assert run["suspend_reason"] == "已完成"
    detail = client.get(f"/api/admin/agents/runs/{summary['run_id']}").json()
    assert [s["status"] for s in detail["steps"]].count("succeeded") == len(detail["steps"])
    assert any(s["artifacts"] for s in detail["steps"])
