"""编排链路端到端 mock 演示（demo-orch）集成测试：整条链路 not real。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform, MockProductSource
from shopkeeper.orchestrator.definition import PIPELINE_V1
from shopkeeper.orchestrator.demo import (
    DEMO_ENV_FORCE,
    FIXTURE_URL,
    DemoOrchError,
    build_demo_env,
    run_demo_orch,
)
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.orchestrator.routes import mount_orchestrator
from shopkeeper.orchestrator.store import RUN_AWAITING_APPROVAL, RUN_SUCCEEDED, STEP_SUCCEEDED
from shopkeeper.workflow.store import WorkflowStore

EXPECTED_STEPS = [s.id for s in PIPELINE_V1.steps]


def test_build_demo_env_forces_mock_and_blocks_tts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_WRITE_ENABLED", "1")
    monkeypatch.setenv("WORKFLOW_PLATFORM", "tiktok_live")
    monkeypatch.setenv("LLM_CHANNEL", "gateway")
    env = build_demo_env({"TTS_WRITE_ENABLED": "1", "WORKFLOW_PLATFORM": "tiktok_live"})
    assert env["TTS_WRITE_ENABLED"] == "0"
    assert env["WORKFLOW_PLATFORM"] == "mock"
    assert env["WORKFLOW_LLM_PROVIDER"] == "mock"
    assert env["LLM_CHANNEL"] == "mock"
    for k, v in DEMO_ENV_FORCE.items():
        assert env[k] == v


def test_demo_orch_full_mock_pipeline(tmp_path: Path) -> None:
    """选品→…→客服白名单 stub 串通；summary + 各步产物路径可展示。"""
    out = tmp_path / "demo-orch"
    summary = run_demo_orch(url=FIXTURE_URL, out_dir=out, auto_approve=True)

    assert summary["ok"] is True
    assert summary["status"] == RUN_SUCCEEDED
    assert summary["url"] == FIXTURE_URL
    assert summary["pipeline"] == EXPECTED_STEPS
    assert summary["env"]["TTS_WRITE_ENABLED"] == "0"
    assert summary["env"]["WORKFLOW_PLATFORM"] == "mock"

    for sid in EXPECTED_STEPS:
        assert summary["step_statuses"][sid] == STEP_SUCCEEDED, sid

    arts = summary["artifacts"]
    assert arts["platform_product_id"]
    assert arts["order_id"]
    assert arts["draft_id"]
    assert arts["approval_id"]

    summary_path = Path(summary["summary_path"])
    assert summary_path.is_file()
    loaded = json.loads(summary_path.read_text(encoding="utf-8"))
    assert loaded["run_id"] == summary["run_id"]
    assert loaded["ok"] is True

    latest = Path(summary["latest_path"])
    assert latest.is_file()
    assert json.loads(latest.read_text(encoding="utf-8"))["run_id"] == summary["run_id"]

    for sid in EXPECTED_STEPS:
        step_file = Path(arts["steps"][sid])
        assert step_file.is_file(), sid
        body = json.loads(step_file.read_text(encoding="utf-8"))
        assert body["status"] == STEP_SUCCEEDED

    assert arts["scout_report"] and Path(arts["scout_report"]).is_file()
    assert arts["pricing"] and Path(arts["pricing"]).is_file()
    assert arts["script"] and Path(arts["script"]).is_file()
    assert arts["outreach"] and Path(arts["outreach"]).is_file()
    outreach_body = json.loads(Path(arts["outreach"]).read_text(encoding="utf-8"))
    assert outreach_body["sent"] is False
    assert outreach_body["auto_send"] is False
    assert len(outreach_body["draft_ids"]) == 3
    assert len(outreach_body["messages"]) == 3
    script_body = json.loads(Path(arts["script"]).read_text(encoding="utf-8"))
    assert len(script_body["hooks"]) == 3
    assert len(script_body["covers"]) == 2
    assert script_body["duration_s"] == 30
    joined = json.dumps(script_body, ensure_ascii=False)
    for banned in ("防水", "食品级", "承重", "全网最低", "waterproof"):
        assert banned not in joined
    assert arts["stylist_copy"].get("th") and Path(arts["stylist_copy"]["th"]).is_file()
    assert arts["stylist_copy"].get("en") and Path(arts["stylist_copy"]["en"]).is_file()
    assert arts["stylist_image"] and Path(arts["stylist_image"]).is_file()
    assert Path(arts["stylist_image"]).stat().st_size > 0

    cs_path = arts["cs"].get("draft_and_send")
    assert cs_path and Path(cs_path).is_file()
    cs_body = json.loads(Path(cs_path).read_text(encoding="utf-8"))
    # 白名单物流类应 stub 发送
    sent = bool(
        (cs_body.get("send") or {}).get("sent_stub")
        or (cs_body.get("draft") or {}).get("sent_stub")
        or cs_body.get("sent_stub")
    )
    assert sent is True

    # 三次审批：上架 / 代发 / 物流
    assert len(summary["approvals"]) >= 3
    approved_steps = {a["step_id"] for a in summary["approvals"]}
    assert "human_approval" in approved_steps
    assert "dropship" in approved_steps
    assert "tracking" in approved_steps


def test_demo_orch_no_auto_approve_stops_at_gate(tmp_path: Path) -> None:
    out = tmp_path / "demo-orch-pending"
    with pytest.raises(DemoOrchError) as ei:
        run_demo_orch(url=FIXTURE_URL, out_dir=out, auto_approve=False)
    assert "awaiting_approval" in str(ei.value) or "未成功" in str(ei.value)
    # 部分产物仍应写出
    runs = [p for p in out.iterdir() if p.is_dir()]
    assert runs
    summary_file = runs[0] / "summary.json"
    assert summary_file.is_file()
    body = json.loads(summary_file.read_text(encoding="utf-8"))
    assert body["ok"] is False
    assert body["status"] == RUN_AWAITING_APPROVAL
    assert body["step_statuses"]["human_approval"] == "awaiting_approval"


def test_demo_orch_api_approve_path_still_gated(tmp_path: Path) -> None:
    """演示批准走内部 approve API 等价路径（八态闸门），不解锁真写。"""
    store = WorkflowStore(tmp_path / "wf.db")
    env = build_demo_env()
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
    assert body["current_step"] == "human_approval"

    # 连续批准直至 succeeded（上架/代发/物流）
    detail = body
    for _ in range(8):
        if detail["status"] != RUN_AWAITING_APPROVAL:
            break
        resp = client.post(
            f"/api/orchestrator/runs/{run_id}/approve",
            json={"note": "demo-orch api approve"},
        )
        assert resp.status_code == 200, resp.text
        detail = resp.json()

    assert detail["status"] == RUN_SUCCEEDED
    by_id = {s["step_id"]: s for s in detail["steps"]}
    for sid in EXPECTED_STEPS:
        assert by_id[sid]["status"] == STEP_SUCCEEDED, sid
    assert (detail.get("context") or {}).get("platform_product_id")
    cs_out = by_id["cs"]["output"]
    assert cs_out.get("sent_stub") is True or (cs_out.get("draft") or {}).get("send")


def test_cli_demo_orch_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from shopkeeper.cli import main

    monkeypatch.setenv("TTS_WRITE_ENABLED", "1")  # 应被演示强制清零
    out = tmp_path / "cli-out"
    code = main(["demo-orch", "--url", FIXTURE_URL, "--out", str(out)])
    assert code == 0
    latest = out / "latest.json"
    assert latest.is_file()
    assert json.loads(latest.read_text(encoding="utf-8"))["ok"] is True
    # 进程环境里的真写开关不应被演示永久改写；强制只在 demo env 合并层
    assert os.environ.get("TTS_WRITE_ENABLED") == "1"
