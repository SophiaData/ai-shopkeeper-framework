"""编排器端到端 mock：审批挂起、TTS 挂起、QA 打回、断点恢复、预算暂停。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform, MockProductSource
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.orchestrator.routes import mount_orchestrator
from shopkeeper.orchestrator.store import (
    RUN_AWAITING_APPROVAL,
    RUN_BUDGET_PAUSED,
    RUN_SUSPENDED,
    STEP_SUCCEEDED,
)
from shopkeeper.workflow.migrate import CURRENT_SCHEMA_VERSION, get_schema_version
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/600123456789.html"


def _orch(tmp_path: Path, *, env: dict[str, str] | None = None) -> Orchestrator:
    store = WorkflowStore(tmp_path / "workflow.db")
    merged = {
        "LLM_CHANNEL": "mock",
        "ORCHESTRATOR_MOCK_LLM_COST_USD": "0",
        **(env or {}),
    }
    return Orchestrator(
        store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env=merged,
        sleep_fn=lambda _s: None,
    )


@pytest.fixture
def orch(tmp_path: Path) -> Orchestrator:
    return _orch(tmp_path)


def test_migrate_v3_orch_tables(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    with store._connect() as conn:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "orch_runs" in tables
    assert "orch_steps" in tables
    assert "orch_llm_usage" in tables
    assert CURRENT_SCHEMA_VERSION >= 3


def test_fixture_url_runs_to_approval_suspend(orch: Orchestrator) -> None:
    detail = orch.start(url=FIXTURE_URL, advance=True)
    assert detail["status"] == RUN_AWAITING_APPROVAL
    assert detail["current_step"] == "human_approval"
    by_id = {s["step_id"]: s for s in detail["steps"]}
    for sid in ("scout", "pricing", "stylist", "qa", "script", "outreach"):
        assert by_id[sid]["status"] == STEP_SUCCEEDED, sid
    assert by_id["human_approval"]["status"] == "awaiting_approval"
    script = by_id["script"]["output"]["script"]
    assert len(script["hooks"]) == 3
    assert len(script["covers"]) == 2
    assert len(script["shots"]) == 5
    assert by_id["script"]["label"] == "短视频脚本"
    assert detail["context"].get("approval_id")
    assert detail["context"].get("draft_id")
    # AI 理由留痕
    assert by_id["scout"]["ai_trace"]
    assert by_id["stylist"]["ai_trace"]


def test_approve_then_publish_tts_blocked(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(
        url=FIXTURE_URL,
        extra_input={"force_tts_write_blocked": True},
        advance=True,
    )
    assert detail["status"] == RUN_AWAITING_APPROVAL
    detail = orch.approve(detail["id"], note="店主批准上架草稿")
    assert detail["status"] == RUN_SUSPENDED
    assert detail["current_step"] == "publish"
    assert (detail.get("error") or {}).get("code") == "TTS_WRITE_BLOCKED"
    by_id = {s["step_id"]: s for s in detail["steps"]}
    assert by_id["human_approval"]["status"] == STEP_SUCCEEDED
    assert by_id["publish"]["status"] == "suspended"


def test_approve_then_mock_publish_success(orch: Orchestrator) -> None:
    detail = orch.start(url=FIXTURE_URL, advance=True)
    detail = orch.approve(detail["id"], note="ok")
    # mock 平台成功 → 继续到下一审批（代发）或更远
    assert detail["status"] in {RUN_AWAITING_APPROVAL, "succeeded"}
    by_id = {s["step_id"]: s for s in detail["steps"]}
    assert by_id["publish"]["status"] == STEP_SUCCEEDED
    assert by_id["order_sync"]["status"] == STEP_SUCCEEDED
    # 代发须再审
    assert detail["status"] == RUN_AWAITING_APPROVAL
    assert detail["current_step"] == "dropship"


def test_qa_reject_triggers_rewrite(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(
        url=FIXTURE_URL,
        extra_input={"force_qa_fail": True},
        advance=True,
    )
    # 第一轮 QA 打回 → stylist 重写 → 第二轮通过 → 挂起审批
    assert detail["status"] == RUN_AWAITING_APPROVAL
    by_id = {s["step_id"]: s for s in detail["steps"]}
    assert by_id["stylist"]["rewrite_round"] >= 1
    traces = by_id["stylist"]["ai_trace"]
    assert any(
        (isinstance(t, dict) and (t.get("event") == "reset_for_rewrite" or "rewrite" in str(t.get("rationale", "")).lower()))
        or (isinstance(t, dict) and t.get("qa_feedback"))
        for t in (traces if isinstance(traces, list) else [traces])
    )


def test_mid_run_restart_recovers(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    env = {"LLM_CHANNEL": "mock", "ORCHESTRATOR_MOCK_LLM_COST_USD": "0"}
    orch1 = Orchestrator(
        store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env=env,
        sleep_fn=lambda _s: None,
    )
    detail = orch1.start(url=FIXTURE_URL, advance=True)
    run_id = detail["id"]
    assert detail["status"] == RUN_AWAITING_APPROVAL

    # 模拟进程重启：新 Orchestrator 实例读同一 DB
    orch2 = Orchestrator(
        store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env=env,
        sleep_fn=lambda _s: None,
    )
    resumed = orch2.resume(run_id)
    assert resumed["status"] == RUN_AWAITING_APPROVAL
    resumed = orch2.approve(run_id, note="after restart")
    by_id = {s["step_id"]: s for s in resumed["steps"]}
    assert by_id["publish"]["status"] == STEP_SUCCEEDED


def test_budget_exceeded_pauses(tmp_path: Path) -> None:
    orch = _orch(
        tmp_path,
        env={
            "LLM_CHANNEL": "mock",
            "LLM_DAILY_BUDGET_USD": "0.01",
            "ORCHESTRATOR_MOCK_LLM_COST_USD": "0.02",
        },
    )
    detail = orch.start(url=FIXTURE_URL, advance=True)
    assert detail["status"] == RUN_BUDGET_PAUSED
    assert (detail.get("error") or {}).get("code") == "LLM_BUDGET_EXCEEDED"


def test_cancel_and_retry_api(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    env = {"LLM_CHANNEL": "mock"}
    orch = Orchestrator(
        store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env=env,
        sleep_fn=lambda _s: None,
    )
    app = FastAPI()
    mount_orchestrator(app, orch)
    client = TestClient(app)

    started = client.post("/api/orchestrator/runs", json={"url": FIXTURE_URL})
    assert started.status_code == 200, started.text
    body = started.json()
    run_id = body["id"]
    assert body["status"] == RUN_AWAITING_APPROVAL

    listed = client.get("/api/orchestrator/runs")
    assert listed.status_code == 200
    assert any(r["id"] == run_id for r in listed.json()["runs"])

    detail = client.get(f"/api/orchestrator/runs/{run_id}")
    assert detail.status_code == 200
    assert "timeline" in detail.json() or "steps" in detail.json()

    cancelled = client.post(f"/api/orchestrator/runs/{run_id}/cancel", json={"note": "stop"})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_autorun_default_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from shopkeeper.orchestrator.scheduler import autorun_enabled, tick_autorun

    monkeypatch.delenv("ORCHESTRATOR_AUTORUN", raising=False)
    assert autorun_enabled({}) is False
    orch = _orch(tmp_path)
    assert tick_autorun(orch, orch.workflow, env={"ORCHESTRATOR_AUTORUN": "0"}) == []


def test_resume_recovers_stale_running_step(tmp_path: Path) -> None:
    """崩溃残留 step=running：resume 收回为 pending 并写 ai_trace，再可续跑。"""
    from shopkeeper.orchestrator.store import STEP_PENDING, STEP_RUNNING

    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, advance=True)
    run_id = detail["id"]
    assert detail["status"] == RUN_AWAITING_APPROVAL

    # 模拟进程在 stylist 执行中崩溃：人为把已成功步改回 running
    orch.store.update_step(run_id, "stylist", status=STEP_RUNNING, mark_started=True)
    recovered = orch.store.recover_stale_running_steps(run_id)
    assert recovered == ["stylist"]
    step = orch.store.get_step(run_id, "stylist")
    assert step is not None
    assert step["status"] == STEP_PENDING
    assert any(
        isinstance(t, dict) and t.get("event") == "recovered_from_stale_running"
        for t in (step["ai_trace"] if isinstance(step["ai_trace"], list) else [step["ai_trace"]])
    )

    # 再次标 running 后走 resume 入口（审批未过则仍 awaiting，但残留已清）
    orch.store.update_step(run_id, "stylist", status=STEP_RUNNING, mark_started=True)
    resumed = orch.resume(run_id)
    stylist = next(s for s in resumed["steps"] if s["step_id"] == "stylist")
    assert stylist["status"] != STEP_RUNNING


def test_publish_idempotent_short_circuit(tmp_path: Path) -> None:
    """publish 已有成功 context 时短路径成功，不重复调平台。"""
    from shopkeeper.orchestrator.steps import StepContext, run_publish
    from shopkeeper.orchestrator.store import STEP_SUCCEEDED

    class _BoomPlatform(MockPlatform):
        def create_listing(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("idempotent publish must not call create_listing")

    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, advance=True)
    run_id = detail["id"]
    # 注入已成功上架结果
    ctx = dict(detail["context"] or {})
    ctx["publish_result"] = {
        "platform_product_id": "PID-IDEMPOTENT",
        "published": True,
        "status": "executed",
        "dry_run": False,
    }
    ctx["platform_product_id"] = "PID-IDEMPOTENT"
    orch.store.update_run(run_id, context=ctx)
    run = orch.store.get_run(run_id)
    assert run is not None
    step = orch.store.get_step(run_id, "publish")
    assert step is not None

    step_ctx = StepContext(
        store=orch.store,
        workflow=orch.workflow,
        llm=orch.llm,
        product_source=MockProductSource(),
        platform=_BoomPlatform(),
        env=orch.env,
    )
    result = run_publish(step_ctx, run, step)
    assert result.status == STEP_SUCCEEDED
    assert result.output.get("idempotent_replay") is True
    assert result.output.get("platform_product_id") == "PID-IDEMPOTENT"
