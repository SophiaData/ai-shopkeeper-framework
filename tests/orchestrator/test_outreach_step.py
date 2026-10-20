"""达人建联草稿步：确定性模板、禁写说法、合规失败不入库、待审不发送。"""

from __future__ import annotations

import json
from pathlib import Path

from shopkeeper.adapters.mock import MockPlatform, MockProductSource
from shopkeeper.orchestrator.definition import PIPELINE_V1
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.orchestrator.outreach import (
    OutreachInputError,
    build_deterministic_outreach,
    listing_outreach_source,
    load_system_prompt,
    shape_problems,
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


def _outreach_drafts(orch: Orchestrator) -> list[dict]:
    return [row for row in orch.workflow.list_drafts() if row["kind"] == "influencer_outreach"]


def test_pipeline_places_outreach_after_script() -> None:
    ids = PIPELINE_V1.ordered_ids()
    assert ids[ids.index("qa") + 1] == "script"
    assert ids[ids.index("script") + 1] == "outreach"
    assert ids[ids.index("outreach") + 1] == "human_approval"
    assert PIPELINE_V1.step("outreach").kind == "ai"
    assert PIPELINE_V1.step("outreach").requires_approval is False
    assert PIPELINE_V1.step("human_approval").depends_on == ("outreach",)


def test_unknown_creator_code() -> None:
    import pytest

    with pytest.raises(OutreachInputError) as exc:
        listing_outreach_source(
            listing_copy={"title_th": "กล่อง", "body_th": "พับได้"},
            facts={},
            run_input={"creators": ["带货"]},
        )
    assert exc.value.code == "ORCH_OUTREACH_CREATOR"


def test_prompt_file_is_used_when_present(tmp_path: Path) -> None:
    present = tmp_path / "influencer-outreach.md"
    present.write_text("## 系统提示\n\n```text\n只写草稿，昵称用输入。\n```\n", encoding="utf-8")
    system, source = load_system_prompt(present)
    assert source == str(present)
    assert "只写草稿，昵称用输入。" in system
    assert "不要输出批量群发方案" in system
    assert "不要承诺未在输入里出现的佣金比例" in system
    assert "只返回 JSON" in system
    assert "creator_type、creator_name" in system
    missing = tmp_path / "missing.md"
    fallback, fallback_source = load_system_prompt(missing)
    assert fallback_source == "embedded"
    assert "达人合作文案助理" in fallback
    assert "只返回 JSON" in fallback


def test_template_uses_three_types_and_skips_invented_offers() -> None:
    source = listing_outreach_source(
        listing_copy={
            "title_th": "กล่องผ้า",
            "title_en": "fabric box",
            "body_th": "พับเก็บได้",
            "body_en": "folds flat",
        },
        facts={"title_raw": "布艺收纳"},
        run_input={"creators": [{"creator_type": "测评", "creator_name": "Nicha", "offer": "寄样"}]},
    )
    bundle = build_deterministic_outreach(source)
    assert shape_problems(bundle, source["creators"]) == []
    assert ungrounded_claims(bundle, source["source_text"]) == []
    message = bundle["messages"][0]
    assert message["creator_name"] == "Nicha"
    blob = json.dumps(bundle, ensure_ascii=False)
    assert "พับเก็บได้" in blob
    assert "待你填写" in blob
    for banned in ("已发送", "30%", "免打孔", "保底", "赠品", "粉丝 12000", "miracle cure"):
        assert banned not in blob
    defaults = listing_outreach_source(
        listing_copy={"title_th": "กล่อง", "body_th": "พับได้"},
        facts={},
        run_input={},
    )
    assert [row["creator_type"] for row in defaults["creators"]] == ["测评", "生活", "出租屋改造"]
    assert all(row["creator_name"] == "达人" for row in defaults["creators"])


def test_seller_stated_no_drill_is_not_ungrounded() -> None:
    source = listing_outreach_source(
        listing_copy={"title_th": "ชั้นวาง", "body_th": "免打孔，可还原"},
        facts={},
        run_input={"creators": [{"creator_type": "出租屋改造", "creator_name": "Dao"}]},
    )
    bundle = build_deterministic_outreach(source)
    bundle["messages"][0]["dm_zh"] += " 免打孔"
    assert ungrounded_claims(bundle, source["source_text"]) == []


def test_default_run_stores_pending_drafts_and_does_not_send(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, advance=True)
    assert detail["status"] == RUN_AWAITING_APPROVAL
    by_id = {step["step_id"]: step for step in detail["steps"]}
    assert by_id["qa"]["status"] == "succeeded"
    assert by_id["script"]["status"] == "succeeded"
    assert by_id["outreach"]["status"] == "succeeded"
    assert by_id["outreach"]["label"] == "达人建联"
    assert by_id["human_approval"]["status"] == "awaiting_approval"
    outreach = detail["context"]["outreach"]
    assert outreach["sent"] is False
    assert outreach["auto_send"] is False
    assert len(outreach["draft_ids"]) == 3
    drafts = _outreach_drafts(orch)
    assert len(drafts) == 3
    assert {row["status"] for row in drafts} == {"pending"}
    assert {row["payload"]["creator_type"] for row in drafts} == {"测评", "生活", "出租屋改造"}
    for row in drafts:
        assert row["qa"]["ok"] is True
        assert row["payload"]["sent"] is False
        assert "待你填写" in json.dumps(row["payload"]["message"], ensure_ascii=False)
        assert orch.workflow.list_external_writes(row["id"]) == []
    assert by_id["outreach"]["ai_trace"]


def test_ungrounded_outreach_fails_before_queue(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, extra_input={"force_outreach_ungrounded": True}, advance=True)
    assert detail["status"] == RUN_FAILED
    assert detail["current_step"] == "outreach"
    assert (detail.get("error") or {}).get("code") == "ORCH_OUTREACH_UNGROUNDED"
    assert detail["context"].get("approval_id") is None
    assert _outreach_drafts(orch) == []
    by_id = {step["step_id"]: step for step in detail["steps"]}
    assert by_id["outreach"]["status"] == "failed"
    assert by_id["human_approval"]["status"] == "pending"


def test_compliance_block_does_not_enter_queue(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, extra_input={"force_outreach_compliance": True}, advance=True)
    assert detail["status"] == RUN_FAILED
    assert (detail.get("error") or {}).get("code") == "ORCH_OUTREACH_COMPLIANCE"
    assert _outreach_drafts(orch) == []


def test_bad_creator_fails_closed(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(
        url=FIXTURE_URL,
        extra_input={"creators": [{"creator_type": "带货"}]},
        advance=True,
    )
    assert detail["status"] == RUN_FAILED
    assert (detail.get("error") or {}).get("code") == "ORCH_OUTREACH_CREATOR"
    assert _outreach_drafts(orch) == []


def test_resume_does_not_duplicate_drafts(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, advance=True)
    run_id = detail["id"]
    assert len(_outreach_drafts(orch)) == 3
    orch.store.update_step(run_id, "outreach", status=STEP_RUNNING, mark_started=True)
    resumed = orch.resume(run_id)
    outreach = next(step for step in resumed["steps"] if step["step_id"] == "outreach")
    assert outreach["status"] in {STEP_PENDING, "succeeded"}
    assert outreach["status"] != STEP_RUNNING
    assert len(_outreach_drafts(orch)) == 3
    assert resumed["status"] == RUN_AWAITING_APPROVAL


def test_retry_after_clearing_ungrounded_flag_creates_drafts(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    detail = orch.start(url=FIXTURE_URL, extra_input={"force_outreach_ungrounded": True}, advance=True)
    run_id = detail["id"]
    assert detail["status"] == RUN_FAILED
    assert _outreach_drafts(orch) == []
    payload = dict(detail["input"])
    payload.pop("force_outreach_ungrounded", None)
    with orch.workflow._lock:
        with orch.workflow._connect() as conn:
            conn.execute(
                "UPDATE orch_runs SET input_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), run_id),
            )
    retried = orch.retry_step(run_id, "outreach")
    assert retried["status"] == RUN_AWAITING_APPROVAL
    assert len(retried["context"]["outreach"]["draft_ids"]) == 3
    assert len(_outreach_drafts(orch)) == 3
