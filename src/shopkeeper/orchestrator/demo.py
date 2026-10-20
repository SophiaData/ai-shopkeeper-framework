"""编排链路端到端 mock 演示：一条命令串通选品→…→客服白名单回复。

默认强制：WORKFLOW_PLATFORM=mock、WORKFLOW_LLM_PROVIDER=mock、TTS_WRITE_ENABLED=0、
LLM_CHANNEL=mock；不外呼真实 TikTok / 1688 / LLM。审批步经 Orchestrator.approve
（内部走八态闸门 apply_approval_event），不解锁 TTS 真写。

产物落 ``artifacts/demo-orch/<run_id>/``：summary.json + 各步 JSON / 文案 / mock 图。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from shopkeeper.adapters.mock import MockImage, MockPlatform, MockProductSource
from shopkeeper.orchestrator.definition import PIPELINE_V1
from shopkeeper.orchestrator.engine import Orchestrator, OrchestratorError
from shopkeeper.orchestrator.store import (
    RUN_AWAITING_APPROVAL,
    RUN_SUCCEEDED,
    STEP_SUCCEEDED,
)
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/600123456789.html"
DEFAULT_OUT = "artifacts/demo-orch"
MAX_AUTO_APPROVALS = 8

#: 演示强制环境：覆盖进程 env 中可能打开的真写/真 LLM 开关
DEMO_ENV_FORCE: dict[str, str] = {
    "WORKFLOW_PLATFORM": "mock",
    "WORKFLOW_LLM_PROVIDER": "mock",
    "TTS_WRITE_ENABLED": "0",
    "LLM_CHANNEL": "mock",
    "ORCHESTRATOR_LLM": "mock",
    "ORCHESTRATOR_MOCK_LLM_COST_USD": "0",
    "ALIBABA_1688_ENABLED": "0",
    "FEISHU_NOTIFY": "0",
    "IMAGE_REDRAW_ENABLED": "0",
}


class DemoOrchError(RuntimeError):
    """演示未跑通终态。"""


def build_demo_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """合并 os.environ + 演示强制项；强制项不可被 overrides 改开真写。"""
    merged = dict(os.environ)
    if overrides:
        merged.update({str(k): str(v) for k, v in overrides.items()})
    merged.update(DEMO_ENV_FORCE)
    return merged


def run_demo_orch(
    *,
    url: str = FIXTURE_URL,
    out_dir: str | Path = DEFAULT_OUT,
    auto_approve: bool = True,
    approve_note: str = "demo-orch 演示批准（八态闸门，不解锁真写）",
    db_path: str | Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """跑通 listing_to_care_v1 mock 全链路并写出可展示 artifacts。

    Returns:
        summary 字典（已写入 ``summary.json``）。失败时抛 ``DemoOrchError``。
    """
    env = build_demo_env(env_overrides)
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    wf_db = Path(db_path) if db_path else root / "workflow.db"
    wf_db.parent.mkdir(parents=True, exist_ok=True)

    store = WorkflowStore(wf_db)
    orch = Orchestrator(
        store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env=env,
        sleep_fn=lambda _s: None,
    )

    detail = orch.start(url=url, flow_id=PIPELINE_V1.id, advance=True)
    run_id = str(detail["id"])
    approvals: list[dict[str, Any]] = []

    if auto_approve:
        for i in range(MAX_AUTO_APPROVALS):
            if detail.get("status") != RUN_AWAITING_APPROVAL:
                break
            step_id = detail.get("current_step")
            try:
                detail = orch.approve(
                    run_id,
                    note=f"{approve_note} [{i + 1}:{step_id}]",
                    decided_by="demo-orch",
                )
            except OrchestratorError as exc:
                raise DemoOrchError(f"批准失败 step={step_id}: {exc}") from exc
            approvals.append(
                {
                    "index": i + 1,
                    "step_id": step_id,
                    "status_after": detail.get("status"),
                    "current_step": detail.get("current_step"),
                }
            )

    if detail.get("status") != RUN_SUCCEEDED:
        # 仍写出部分产物便于排障
        summary = _materialize(root, detail, approvals=approvals, ok=False)
        raise DemoOrchError(
            f"演示未成功结束：status={detail.get('status')} "
            f"current_step={detail.get('current_step')} "
            f"error={detail.get('error')} summary={summary['summary_path']}"
        )

    return _materialize(root, detail, approvals=approvals, ok=True)


def _materialize(
    root: Path,
    detail: dict[str, Any],
    *,
    approvals: list[dict[str, Any]],
    ok: bool,
) -> dict[str, Any]:
    run_id = str(detail["id"])
    run_dir = root / run_id
    steps_dir = run_dir / "steps"
    stylist_dir = run_dir / "stylist"
    cs_dir = run_dir / "cs"
    steps_dir.mkdir(parents=True, exist_ok=True)
    stylist_dir.mkdir(parents=True, exist_ok=True)
    cs_dir.mkdir(parents=True, exist_ok=True)

    step_paths: dict[str, str] = {}
    by_id = {s["step_id"]: s for s in detail.get("steps") or []}
    for sid, step in by_id.items():
        path = steps_dir / f"{sid}.json"
        path.write_text(
            json.dumps(
                {
                    "step_id": sid,
                    "status": step.get("status"),
                    "output": step.get("output") or {},
                    "ai_trace": step.get("ai_trace") or {},
                    "error": step.get("error"),
                    "rewrite_round": step.get("rewrite_round"),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        step_paths[sid] = str(path.as_posix())

    ctx = detail.get("context") or {}
    copy = ctx.get("listing_copy") or {}
    copy_paths: dict[str, str] = {}
    if copy:
        for lang, key in (("th", "body_th"), ("en", "body_en")):
            title = str(copy.get(f"title_{lang}") or "")
            body = str(copy.get(key) or "")
            p = stylist_dir / f"copy_{lang}.txt"
            p.write_text(f"{title}\n\n{body}\n", encoding="utf-8")
            copy_paths[lang] = str(p.as_posix())
        listing_json = stylist_dir / "listing_copy.json"
        listing_json.write_text(
            json.dumps(copy, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        copy_paths["listing_copy"] = str(listing_json.as_posix())

    image_path: str | None = None
    if copy or by_id.get("stylist", {}).get("status") == STEP_SUCCEEDED:
        image_path = _write_mock_image(stylist_dir, run_id=run_id, title=str(copy.get("title_en") or "demo"))

    cs_paths: dict[str, str] = {}
    cs_draft = ctx.get("cs_draft") or (by_id.get("cs") or {}).get("output") or {}
    if cs_draft:
        cs_file = cs_dir / "draft_and_send.json"
        cs_file.write_text(
            json.dumps(cs_draft, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        cs_paths["draft_and_send"] = str(cs_file.as_posix())

    report = ctx.get("report")
    report_path: str | None = None
    if report is not None:
        rp = run_dir / "scout_report.json"
        rp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        report_path = str(rp.as_posix())

    pricing = ctx.get("pricing")
    pricing_path: str | None = None
    if pricing is not None:
        pp = run_dir / "pricing.json"
        pp.write_text(json.dumps(pricing, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        pricing_path = str(pp.as_posix())

    script = ctx.get("video_script") or ((by_id.get("script") or {}).get("output") or {}).get("script")
    script_path: str | None = None
    if script:
        sp = run_dir / "script.json"
        sp.write_text(json.dumps(script, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        script_path = str(sp.as_posix())

    outreach = ctx.get("outreach") or (by_id.get("outreach") or {}).get("output") or {}
    outreach_path: str | None = None
    if outreach:
        op = run_dir / "outreach.json"
        op.write_text(json.dumps(outreach, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        outreach_path = str(op.as_posix())

    expected_steps = [s.id for s in PIPELINE_V1.steps]
    step_statuses = {sid: (by_id.get(sid) or {}).get("status") for sid in expected_steps}

    summary: dict[str, Any] = {
        "ok": ok,
        "run_id": run_id,
        "flow_id": detail.get("flow_id"),
        "status": detail.get("status"),
        "url": (detail.get("input") or {}).get("url"),
        "shop_id": detail.get("shop_id"),
        "current_step": detail.get("current_step"),
        "error": detail.get("error"),
        "approvals": approvals,
        "step_statuses": step_statuses,
        "artifacts": {
            "run_dir": str(run_dir.as_posix()),
            "steps": step_paths,
            "scout_report": report_path,
            "pricing": pricing_path,
            "script": script_path,
            "outreach": outreach_path,
            "stylist_copy": copy_paths,
            "stylist_image": image_path,
            "cs": cs_paths,
            "context_keys": sorted(ctx.keys()),
            "platform_product_id": ctx.get("platform_product_id"),
            "order_id": ctx.get("order_id"),
            "draft_id": ctx.get("draft_id"),
            "approval_id": ctx.get("approval_id"),
        },
        "env": {
            "WORKFLOW_PLATFORM": DEMO_ENV_FORCE["WORKFLOW_PLATFORM"],
            "WORKFLOW_LLM_PROVIDER": DEMO_ENV_FORCE["WORKFLOW_LLM_PROVIDER"],
            "TTS_WRITE_ENABLED": DEMO_ENV_FORCE["TTS_WRITE_ENABLED"],
            "LLM_CHANNEL": DEMO_ENV_FORCE["LLM_CHANNEL"],
            "note": "演示强制 mock；不外呼真实 TikTok/1688/LLM；不解锁 TTS 真写",
        },
        "pipeline": expected_steps,
    }

    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    # 根目录快捷指针，便于 make demo-orch 后一眼找到最新产物
    latest = root / "latest.json"
    latest.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "ok": ok,
                "status": detail.get("status"),
                "summary_path": str(summary_path.as_posix()),
                "run_dir": str(run_dir.as_posix()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    summary["summary_path"] = str(summary_path.as_posix())
    summary["latest_path"] = str(latest.as_posix())
    return summary


def _write_mock_image(stylist_dir: Path, *, run_id: str, title: str) -> str | None:
    """落一张确定性 mock 图，供演示展示（不外呼图像网关）。"""
    images_dir = stylist_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    try:
        client = MockImage()
        result = client.repaint(
            image_ref=f"demo-orch:{run_id}",
            lang="EN",
            watermark_check=False,
            overlay_headline=(title or "demo-orch")[:48],
            overlay_sub="mock · no network",
        )
        if not result.ok or result.data is None:
            return None
        asset = result.data
        src = Path(getattr(asset, "output_path", None) or getattr(asset, "path", "") or "")
        if not src.is_file():
            return None
        dest = images_dir / (src.name if src.suffix else "listing_en.png")
        dest.write_bytes(src.read_bytes())
        return str(dest.as_posix())
    except Exception:  # noqa: BLE001
        # 演示展示用；图像失败不阻断链路摘要
        return None
