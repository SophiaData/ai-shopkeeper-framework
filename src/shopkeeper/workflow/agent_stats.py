"""各 agent 产出统计（只读）。

数字只来自工作台库已有行：``orch_runs`` / ``orch_steps`` / ``orch_llm_usage`` / ``approvals``。
没有 LLM 用量行、轨迹里也没有 token 时，该 agent 标「缺数据」，不填 0 冒充已计量。
不写库、不外呼、不打开 TTS 真写。
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from shopkeeper.orchestrator.budget import daily_budget_usd
from shopkeeper.site_profile import business_timezone_name, business_tz
from shopkeeper.orchestrator.definition import PIPELINE_V1, STEP_LABELS
from shopkeeper.orchestrator.store import OrchestratorStore
from shopkeeper.workflow.store import WorkflowStore

# 与声明式流水线同序。描述对齐 definition.PIPELINE_V1，此处只供后台展示。
AGENT_CATALOG: tuple[dict[str, str], ...] = (
    {"id": "scout", "name": "选品官", "role": "Scout", "kind": "ai", "description": "1688 链接 → 商品事实与选品报告"},
    {"id": "pricing", "name": "定价", "role": "定价", "kind": "ai", "description": "售价公式给出建议价；LLM 只写理由"},
    {"id": "stylist", "name": "文案图片", "role": "Stylist", "kind": "ai", "description": "多语言文案与图片草稿"},
    {"id": "qa", "name": "合规质检", "role": "QA", "kind": "deterministic", "description": "合规词与质检；不通过打回 Stylist"},
    {"id": "script", "name": STEP_LABELS["script"], "role": "Script", "kind": "ai", "description": "分镜、Hook 与封面；默认确定性模板，可选 LLM"},
    {"id": "outreach", "name": STEP_LABELS["outreach"], "role": "Outreach", "kind": "ai", "description": "达人建联草稿；只入库待审，不发送"},
    {"id": "human_approval", "name": "人工审批", "role": "审批", "kind": "gate", "description": "八态闸门，挂起待人工拍板"},
    {"id": "publish", "name": "上架", "role": "上架", "kind": "execution", "description": "受 TTS_WRITE_*；未放行可挂起"},
    {"id": "order_sync", "name": "订单同步", "role": "订单", "kind": "deterministic", "description": "只读订单同步，不履约"},
    {"id": "dropship", "name": "代发", "role": "代发", "kind": "execution", "description": "代发草稿闸门，不自动付钱"},
    {"id": "tracking", "name": "物流", "role": "物流", "kind": "execution", "description": "物流回填（格式校验，查询默认关），须审批"},
    {"id": "cs", "name": "客服", "role": "客服", "kind": "deterministic", "description": "白名单话术 stub；涉钱类必审"},
)

_STEP_LABELS = {row["id"]: row["name"] for row in AGENT_CATALOG}

# 审批表 action_type → 流水线 agent。未映射的动作仍计入 totals，不塞进某个 agent。
_APPROVAL_AGENT = {
    "create_listing": "human_approval",
    "pricing_override": "pricing",
    "update_price": "pricing",
    "dropship_order": "dropship",
    "place_po": "dropship",
    "update_tracking": "tracking",
    "auto_reply": "cs",
}

_QA_ERROR_CODES = frozenset({"ORCH_QA_EXHAUSTED", "ORCH_QA_BLOCK"})
_RUN_QUERY_LIMIT = 200
_RUN_LIST_LIMIT = 40
_ARTIFACTS_PER_AGENT = 3

MISSING = "缺数据"


class AgentStatsError(ValueError):
    """统计查询参数不合法。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def parse_stats_date(raw: str | None, *, field: str) -> date | None:
    """``YYYY-MM-DD``；空 → None。非法 → AgentStatsError。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise AgentStatsError(
            "AGENT_STATS_DATE_INVALID",
            f"{field} 须为 YYYY-MM-DD，收到 {raw!r}",
        ) from exc
    if parsed.isoformat() != text:
        raise AgentStatsError(
            "AGENT_STATS_DATE_INVALID",
            f"{field} 须为 YYYY-MM-DD，收到 {raw!r}",
        )
    return parsed


def bangkok_window(date_from: date | None, date_to: date | None) -> tuple[str | None, str | None]:
    """画像日历日闭区间 → UTC ISO ``[start, end)``。两端皆空 → 不限。

    时区与利润日、调度日、预算日相同，都是 ``business_tz()``。默认 Asia/Bangkok。
    """
    if date_from is None and date_to is None:
        return None, None
    if date_from is not None and date_to is not None and date_from > date_to:
        raise AgentStatsError("AGENT_STATS_DATE_INVALID", "from 不能晚于 to")
    start_day = date_from or date(1970, 1, 1)
    end_day = date_to or date(9999, 12, 30)
    zone = business_tz()
    start_dt = datetime.combine(start_day, time.min, tzinfo=zone)
    end_dt = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=zone)
    return (
        start_dt.astimezone(timezone.utc).isoformat(),
        end_dt.astimezone(timezone.utc).isoformat(),
    )


def suspend_reason(run: dict[str, Any]) -> dict[str, str]:
    """当前挂起/终态原因。优先级：待审批 > 预算 > TTS 挂起 > QA 打回 > 其它状态。"""
    status = str(run.get("status") or "")
    err = run.get("error") if isinstance(run.get("error"), dict) else {}
    code = str((err or {}).get("code") or "")
    ctx = run.get("context") if isinstance(run.get("context"), dict) else {}
    if status == "awaiting_approval":
        return {"code": "awaiting_approval", "label": "待审批"}
    if status == "budget_paused" or code == "LLM_BUDGET_EXCEEDED" or bool((ctx or {}).get("budget_paused")):
        return {"code": "budget", "label": "预算"}
    if code == "TTS_WRITE_BLOCKED" or bool((ctx or {}).get("publish_blocked")):
        return {"code": "tts_suspended", "label": "TTS 挂起"}
    if code in _QA_ERROR_CODES or bool((ctx or {}).get("needs_rewrite")):
        return {"code": "qa_rejected", "label": "QA 打回"}
    labels = {
        "suspended": "挂起",
        "failed": "失败",
        "succeeded": "已完成",
        "running": "运行中",
        "pending": "排队",
        "cancelled": "已取消",
    }
    return {"code": code or status or "unknown", "label": labels.get(status, status or "未知")}


def step_label(step_id: str | None) -> str:
    if not step_id:
        return "—"
    return _STEP_LABELS.get(step_id, step_id)


def build_agent_stats(
    workflow: WorkflowStore,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    shop_id: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 agent 汇总。``shop_id`` 为 None 时不过滤店。"""
    start_iso, end_iso = bangkok_window(date_from, date_to)
    runs = _load_runs(workflow, shop_id=shop_id, start_iso=start_iso, end_iso=end_iso)
    run_ids = [r["id"] for r in runs]
    steps_by_run = _load_steps(workflow, run_ids)
    usage = _load_llm_usage(workflow, run_ids)
    approvals = _load_approvals(
        workflow, shop_id=shop_id, start_iso=start_iso, end_iso=end_iso
    )

    by_agent_steps: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {
        row["id"]: [] for row in AGENT_CATALOG
    }
    for run in runs:
        for step in steps_by_run.get(run["id"], []):
            bucket = by_agent_steps.get(step["step_id"])
            if bucket is not None:
                bucket.append((run, step))

    agents: list[dict[str, Any]] = []
    for spec in AGENT_CATALOG:
        agents.append(
            _agent_row(
                spec,
                by_agent_steps.get(spec["id"], []),
                usage.get(spec["id"]),
                approvals["by_agent"].get(spec["id"], _empty_approval_counts()),
            )
        )

    listed = [_run_summary(run) for run in runs[:_RUN_LIST_LIMIT]]
    spent = round(sum(a["llm"]["cost_usd"] or 0 for a in agents if a["llm"]["status"] == "recorded"), 6)
    limit = daily_budget_usd(env if env is not None else os.environ)
    trace_events = sum(a["task_log_events"] for a in agents)
    return {
        "from": date_from.isoformat() if date_from else None,
        "to": date_to.isoformat() if date_to else None,
        "timezone": business_timezone_name(),
        "shop_id": shop_id,
        "sources": ["orch_runs", "orch_steps", "orch_llm_usage", "approvals"],
        "truncated": len(runs) >= _RUN_QUERY_LIMIT,
        "run_count": len(runs),
        "budget": {
            "daily_limit_usd": limit,
            "limit_status": "missing" if limit is None else "recorded",
            "limit_label": MISSING if limit is None else None,
            "spent_in_range_usd": spent,
        },
        "task_logs": {
            "source": "orch_steps.ai_trace",
            "events": trace_events,
            "business_tasks": {
                "status": "missing",
                "label": MISSING,
                "reason": "业务库 tasks/tool_calls 不在工作台库，本接口不编造该计数",
            },
        },
        "approvals": approvals["totals"],
        "agents": agents,
        "runs": listed,
    }


def build_agent_run_detail(workflow: WorkflowStore, run_id: str) -> dict[str, Any] | None:
    """单条 run 的步骤时间线。不存在 → None。"""
    detail = OrchestratorStore(workflow).run_detail(run_id)
    if detail is None:
        return None
    reason = suspend_reason(detail)
    order = {step.id: index for index, step in enumerate(PIPELINE_V1.steps)}
    ordered = sorted(
        detail.get("steps") or [],
        key=lambda step: order.get(str(step.get("step_id")), 99),
    )
    steps_out: list[dict[str, Any]] = []
    for step in ordered:
        steps_out.append(
            {
                "step_id": step["step_id"],
                "label": step_label(step["step_id"]),
                "status": step["status"],
                "attempt": step.get("attempt") or 0,
                "rewrite_round": step.get("rewrite_round") or 0,
                "started_at": step.get("started_at"),
                "finished_at": step.get("finished_at"),
                "error": step.get("error"),
                "qa_rejected": _qa_reject_count(step),
                "artifacts": _artifact_links(step["step_id"], step, detail["id"]),
            }
        )
    return {
        "run": _run_summary(detail),
        "suspend_reason": reason["label"],
        "suspend_code": reason["code"],
        "steps": steps_out,
    }


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    reason = suspend_reason(run)
    current = run.get("current_step")
    return {
        "id": run["id"],
        "flow_id": run.get("flow_id"),
        "shop_id": run.get("shop_id"),
        "status": run.get("status"),
        "current_step": current,
        "current_step_label": step_label(current) if current else "—",
        "suspend_reason": reason["label"],
        "suspend_code": reason["code"],
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "finished_at": run.get("finished_at"),
        "error": run.get("error"),
    }


def _agent_row(
    spec: dict[str, str],
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    usage: dict[str, Any] | None,
    approval_counts: dict[str, int],
) -> dict[str, Any]:
    counts = {
        "runs": 0,
        "succeeded": 0,
        "qa_rejected": 0,
        "awaiting_approval": 0,
        "failed": 0,
        "suspended": 0,
    }
    trace_events = 0
    trace_llm = {"found": False, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
    artifacts: list[dict[str, Any]] = []
    for run, step in pairs:
        if _step_touched(step):
            counts["runs"] += 1
        status = step.get("status")
        if status == "succeeded":
            counts["succeeded"] += 1
        elif status == "awaiting_approval":
            counts["awaiting_approval"] += 1
        elif status == "failed":
            counts["failed"] += 1
        elif status == "suspended":
            counts["suspended"] += 1
        counts["qa_rejected"] += _qa_reject_count(step)
        trace_events += _trace_event_count(step)
        _accumulate_trace_llm(step, trace_llm)
        if status in {"succeeded", "awaiting_approval", "failed", "suspended"}:
            artifacts.extend(_artifact_links(spec["id"], step, run["id"]))

    llm = _llm_block(usage, trace_llm)
    # 最近产物：步骤按 run 创建时间已是新→旧（runs 查询 DESC，steps 跟随）。截断即可。
    recent = artifacts[:_ARTIFACTS_PER_AGENT]
    return {
        "id": spec["id"],
        "name": spec["name"],
        "role": spec["role"],
        "kind": spec["kind"],
        "description": spec["description"],
        "health": _health(counts),
        "counts": counts,
        "llm": llm,
        "approvals": approval_counts,
        "task_log_events": trace_events,
        "recent_artifacts": recent,
    }


def _health(counts: dict[str, int]) -> str:
    if counts["runs"] == 0:
        return "idle"
    if counts["failed"] > 0:
        return "bad"
    if counts["awaiting_approval"] or counts["suspended"] or counts["qa_rejected"]:
        return "warn"
    return "ok"


def _step_touched(step: dict[str, Any]) -> bool:
    if step.get("started_at"):
        return True
    return str(step.get("status") or "") != "pending"


def _as_traces(step: dict[str, Any]) -> list[Any]:
    raw = step.get("ai_trace")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw] if raw else []
    return []


def _trace_event_count(step: dict[str, Any]) -> int:
    return sum(1 for item in _as_traces(step) if isinstance(item, dict) and item)


def _qa_reject_count(step: dict[str, Any]) -> int:
    n = 0
    for item in _as_traces(step):
        if isinstance(item, dict) and item.get("event") == "reset_for_rewrite":
            n += 1
    err = step.get("error") if isinstance(step.get("error"), dict) else {}
    if str((err or {}).get("code") or "") in _QA_ERROR_CODES:
        n += 1
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    if step.get("step_id") == "qa" and (output or {}).get("rewrite") is True and n == 0:
        n += 1
    return n


def _accumulate_trace_llm(step: dict[str, Any], acc: dict[str, Any]) -> None:
    for item in _as_traces(step):
        if not isinstance(item, dict):
            continue
        if not any(key in item for key in ("prompt_tokens", "completion_tokens", "cost_usd")):
            continue
        acc["found"] = True
        acc["prompt_tokens"] += int(item.get("prompt_tokens") or 0)
        acc["completion_tokens"] += int(item.get("completion_tokens") or 0)
        try:
            acc["cost_usd"] += float(item.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass


def _llm_block(usage: dict[str, Any] | None, trace: dict[str, Any]) -> dict[str, Any]:
    if usage and int(usage.get("calls") or 0) > 0:
        return {
            "status": "recorded",
            "label": None,
            "source": "orch_llm_usage",
            "calls": int(usage["calls"]),
            "prompt_tokens": int(usage["prompt_tokens"]),
            "completion_tokens": int(usage["completion_tokens"]),
            "cost_usd": round(float(usage["cost_usd"]), 6),
        }
    if trace.get("found"):
        return {
            "status": "recorded",
            "label": None,
            "source": "orch_steps.ai_trace",
            "calls": 0,
            "prompt_tokens": int(trace["prompt_tokens"]),
            "completion_tokens": int(trace["completion_tokens"]),
            "cost_usd": round(float(trace["cost_usd"]), 6),
        }
    return {
        "status": "missing",
        "label": MISSING,
        "source": None,
        "calls": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cost_usd": None,
    }


def _artifact_links(step_id: str, step: dict[str, Any], run_id: str) -> list[dict[str, str]]:
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    if not output:
        return []
    href = f"/api/orchestrator/runs/{run_id}"
    links: list[dict[str, str]] = []

    def add(label: str, kind: str, link: str | None = None) -> None:
        text = " ".join(str(label).split())
        if not text:
            return
        links.append(
            {
                "run_id": run_id,
                "step_id": step_id,
                "kind": kind,
                "label": text[:80],
                "href": link or href,
            }
        )

    if step_id == "scout":
        facts = output.get("facts") if isinstance(output.get("facts"), dict) else {}
        title = str((facts or {}).get("title_raw") or "").strip()
        if output.get("report") or title:
            add(f"选品报告 · {title}" if title else "选品报告", "scout_report")
    elif step_id == "pricing":
        price = output.get("suggested_price_thb")
        if price is None:
            price = output.get("suggested_retail_cents")
        if price is not None:
            add(f"建议价 {price}", "pricing")
    elif step_id == "stylist":
        copy = output.get("copy") if isinstance(output.get("copy"), dict) else {}
        title = str((copy or {}).get("title_th") or (copy or {}).get("title_en") or "").strip()
        if title:
            add(f"文案 · {title}", "listing_copy")
    elif step_id == "qa" and "passed" in output:
        add("质检通过" if output.get("passed") else "质检未通过", "qa")
    elif step_id == "script":
        script = output.get("script") if isinstance(output.get("script"), dict) else {}
        hooks = script.get("hooks") if isinstance(script, dict) else None
        if hooks:
            add(f"短视频脚本 · {len(hooks)} 个 Hook", "video_script")
        elif output.get("ungrounded"):
            add("短视频脚本未通过卖点核对", "video_script")
    elif step_id == "outreach":
        count = output.get("count")
        if count:
            add(f"达人建联草稿 · {count} 条", "outreach", "/admin/outreach")
        elif output.get("ungrounded") or output.get("problems"):
            add("达人建联草稿未通过核对", "outreach", "/admin/outreach")
    elif step_id == "human_approval":
        if output.get("draft_id"):
            add(f"草稿 {output['draft_id']}", "draft")
        if output.get("approval_id"):
            add(
                f"审批 {output['approval_id']}",
                "approval",
                f"/api/admin/approvals/{output['approval_id']}",
            )
    elif step_id == "publish" and output.get("platform_product_id"):
        add(f"平台商品 {output['platform_product_id']}", "platform_product")
    elif step_id == "order_sync" and output.get("order_id"):
        add(f"订单 {output['order_id']}", "order")
    elif step_id == "dropship":
        if output.get("approval_id"):
            add(
                f"代发审批 {output['approval_id']}",
                "approval",
                f"/api/admin/approvals/{output['approval_id']}",
            )
    elif step_id == "tracking" and output.get("approval_id"):
        add(
            f"物流审批 {output['approval_id']}",
            "approval",
            f"/api/admin/approvals/{output['approval_id']}",
        )
    elif step_id == "cs":
        note = str(output.get("note") or "").strip()
        if note:
            add(note, "cs_stub")
    return links[:2]


def _empty_approval_counts() -> dict[str, int]:
    return {"pending": 0, "approved": 0, "rejected": 0, "other": 0}


def _load_runs(
    workflow: WorkflowStore,
    *,
    shop_id: str | None,
    start_iso: str | None,
    end_iso: str | None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if shop_id is not None:
        clauses.append("shop_id = ?")
        params.append(shop_id)
    if start_iso is not None:
        clauses.append("created_at >= ?")
        params.append(start_iso)
    if end_iso is not None:
        clauses.append("created_at < ?")
        params.append(end_iso)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(_RUN_QUERY_LIMIT)
    with workflow._connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM orch_runs {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [OrchestratorStore._run_view(row) for row in rows]


def _load_steps(workflow: WorkflowStore, run_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {rid: [] for rid in run_ids}
    if not run_ids:
        return grouped
    # 保持与 run 列表一致的顺序：调用方按 run 新→旧遍历。
    marks = ",".join("?" for _ in run_ids)
    order = {rid: index for index, rid in enumerate(run_ids)}
    flow_order = {step.id: index for index, step in enumerate(PIPELINE_V1.steps)}
    with workflow._connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM orch_steps WHERE run_id IN ({marks})",
            run_ids,
        ).fetchall()
    steps = [OrchestratorStore._step_view(row) for row in rows]
    steps.sort(key=lambda s: (order.get(s["run_id"], 0), flow_order.get(s["step_id"], 99)))
    for step in steps:
        grouped.setdefault(step["run_id"], []).append(step)
    return grouped


def _load_llm_usage(workflow: WorkflowStore, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not run_ids:
        return {}
    marks = ",".join("?" for _ in run_ids)
    with workflow._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT step_id,
                   COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd
            FROM orch_llm_usage
            WHERE run_id IN ({marks})
            GROUP BY step_id
            """,
            run_ids,
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[str(row["step_id"])] = {
            "calls": int(row["calls"]),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "cost_usd": float(row["cost_usd"]),
        }
    return out


def _load_approvals(
    workflow: WorkflowStore,
    *,
    shop_id: str | None,
    start_iso: str | None,
    end_iso: str | None,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if shop_id is not None:
        clauses.append("shop_id = ?")
        params.append(shop_id)
    if start_iso is not None:
        clauses.append("created_at >= ?")
        params.append(start_iso)
    if end_iso is not None:
        clauses.append("created_at < ?")
        params.append(end_iso)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    totals = _empty_approval_counts()
    by_agent: dict[str, dict[str, int]] = {}
    with workflow._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT action_type, status, COUNT(*) AS n
            FROM approvals
            {where}
            GROUP BY action_type, status
            """,
            params,
        ).fetchall()
    for row in rows:
        action = str(row["action_type"] or "")
        status = str(row["status"] or "")
        n = int(row["n"])
        bucket = _approval_bucket(status)
        totals[bucket] += n
        agent_id = _APPROVAL_AGENT.get(action)
        if agent_id is None:
            continue
        slot = by_agent.setdefault(agent_id, _empty_approval_counts())
        slot[bucket] += n
    return {"totals": totals, "by_agent": by_agent}


def _approval_bucket(status: str) -> str:
    if status == "pending":
        return "pending"
    if status in {"approved", "executing", "executed"}:
        return "approved"
    if status == "rejected":
        return "rejected"
    return "other"
