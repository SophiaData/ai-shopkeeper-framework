"""短视频脚本步：确定性模板、禁写规格、失败可恢复。"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.adapters.mock import MockPlatform, MockProductSource
from shopkeeper.orchestrator.definition import PIPELINE_V1
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.orchestrator.scripting import (
    ScriptInputError,
    build_deterministic_script,
    listing_script_source,
    shot_windows,
    ungrounded_claims,
)
from shopkeeper.orchestrator.store import RUN_AWAITING_APPROVAL, RUN_FAILED, STEP_PENDING, STEP_RUNNING
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/600123456789.html"


def _orch(tmp_path: Path, *, env: dict[str, str] | None = None) -> Orchestrator:
    store = WorkflowStore(tmp_path / "workflow.db")
    merged = {"LLM_CHANNEL": "mock", "ORCHESTRATOR_MOCK_LLM_COST_USD": "0", **(env or {})}
    return Orchestrator(
        store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env=merged,
        sleep_fn=lambda _s: None,
    )


def test_bad_cart_value_is_rejected() -> None:
    with pytest.raises(ScriptInputError) as exc:
        listing_script_source(
            listing_copy={"title_th": "กล่อง", "body_th": "พับได้"},
            facts={},
            run_input={"cart": "maybe"},
        )
    assert exc.value.code == "ORCH_SCRIPT_CART"


def test_pipeline_inserts_script_between_qa_and_approval() -> None:
    ids = PIPELINE_V1.ordered_ids()
    assert ids[ids.index("qa") + 1] == "script"
    assert ids[ids.index("script") + 1] == "outreach"
    assert ids[ids.index("outreach") + 1] == "human_approval"
    assert PIPELINE_V1.step("script").depends_on == ("qa",)
    assert PIPELINE_V1.step("outreach").depends_on == ("script",)
    assert PIPELINE_V1.step("human_approval").depends_on == ("outreach",)
    assert PIPELINE_V1.step("script").kind == "ai"


def test_windows_cover_duration_and_hook_stays_in_three_seconds() -> None:
    for duration in (15, 30, 45, 60):
        windows = shot_windows(duration)
        assert windows[0] == (0, 3, "Hook")
        assert windows[-1][1] == duration
        assert [end for _, end, _ in windows] == sorted(end for _, end, _ in windows)
        assert all(end > start for start, end, _ in windows)


def test_template_repeats_seller_points_and_skips_invented_specs() -> None:
    source = listing_script_source(
        listing_copy={
            "title_th": "กล่องผ้า",
            "title_en": "fabric box",
            "body_th": "พับเก็บได้",
            "body_en": "folds flat",
        },
        facts={"title_raw": "布艺收纳"},
        run_input={"duration": 45, "cart": "否", "scene": "小厨房"},
    )
    script = build_deterministic_script(source)
    assert script["duration_s"] == 45
    assert script["cart"] is False
    assert script["scene"] == "小厨房"
    assert len(script["hooks"]) == 3
    assert len(script["covers"]) == 2
    blob = str(script)
    assert "พับเก็บได้" in blob
    assert "folds flat" in blob
    assert "ตะกร้า" not in script["shots"][-1]["voice_th"]
    assert ungrounded_claims(script, source["source_text"]) == []
    for banned in ("防水", "食品级", "承重", "全网最低"):
        assert banned not in blob


def test_seller_stated_claim_is_not_ungrounded() -> None:
    source = listing_script_source(
        listing_copy={"title_th": "กล่อง", "body_th": "มีซับในกันน้ำตามที่ผู้ขายระบุ"},
        facts={},
        run_input={},
    )
    script = build_deterministic_script(source)
    script["shots"][2]["voice_zh"] += " กันน้ำ"
    assert ungrounded_claims(script, source["source_text"]) == []


def test_default_run_script_reaches_approval_with_storyboard(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, extra_input={"duration": 30, "cart": True}, advance=True)
    assert detail["status"] == RUN_AWAITING_APPROVAL
    by_id = {s["step_id"]: s for s in detail["steps"]}
    assert by_id["qa"]["status"] == "succeeded"
    assert by_id["script"]["status"] == "succeeded"
    assert by_id["human_approval"]["status"] == "awaiting_approval"
    script = detail["context"]["video_script"]
    assert script["duration_s"] == 30
    assert script["cart"] is True
    assert script["closing_note"].startswith("发布、挂车和活动价")
    assert by_id["script"]["label"] == "短视频脚本"
    assert [step["step_id"] for step in detail["steps"]] == PIPELINE_V1.ordered_ids()
    assert by_id["script"]["ai_trace"]
    draft = orch.workflow.get_draft(detail["context"]["draft_id"])
    assert draft is not None
    assert draft["payload"]["video_script"]["hooks"]


def test_ungrounded_script_fails_before_approval(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, extra_input={"force_script_ungrounded": True}, advance=True)
    assert detail["status"] == RUN_FAILED
    assert detail["current_step"] == "script"
    assert (detail.get("error") or {}).get("code") == "ORCH_SCRIPT_UNGROUNDED"
    assert detail["context"].get("approval_id") is None
    by_id = {s["step_id"]: s for s in detail["steps"]}
    assert by_id["script"]["status"] == "failed"
    assert by_id["human_approval"]["status"] == "pending"


def test_bad_duration_fails_closed(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, extra_input={"duration": 90}, advance=True)
    assert detail["status"] == RUN_FAILED
    assert (detail.get("error") or {}).get("code") == "ORCH_SCRIPT_DURATION"


def test_resume_recovers_stale_running_script(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, advance=True)
    run_id = detail["id"]
    assert detail["status"] == RUN_AWAITING_APPROVAL
    orch.store.update_step(run_id, "script", status=STEP_RUNNING, mark_started=True)
    resumed = orch.resume(run_id)
    script = next(s for s in resumed["steps"] if s["step_id"] == "script")
    assert script["status"] == STEP_PENDING or script["status"] == "succeeded"
    assert script["status"] != STEP_RUNNING
    assert resumed["status"] == RUN_AWAITING_APPROVAL


def test_retry_after_clearing_ungrounded_flag_continues(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, extra_input={"force_script_ungrounded": True}, advance=True)
    run_id = detail["id"]
    assert detail["status"] == RUN_FAILED
    payload = dict(detail["input"])
    payload.pop("force_script_ungrounded", None)
    import json

    with orch.workflow._lock:
        with orch.workflow._connect() as conn:
            conn.execute(
                "UPDATE orch_runs SET input_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), run_id),
            )
    retried = orch.retry_step(run_id, "script")
    assert retried["status"] == RUN_AWAITING_APPROVAL
    assert retried["context"]["video_script"]["hooks"]
