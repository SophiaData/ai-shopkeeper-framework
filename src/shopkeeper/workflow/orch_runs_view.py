"""编排 run 只读投影，供 /admin 编排运行页。

步骤、输入输出与 LLM 用量已经落在工作台库 migrate v3 的
``orch_runs`` / ``orch_steps`` / ``orch_llm_usage``。本模块只读这些行和
``approvals``，不建表、不写库、不外呼。

摘要只取白名单字段。电话、地址、令牌、原始快照不进入响应。
没有 LLM 计量时标「缺数据」，不把 0 写成已计量。
错误以 ``{code, message}`` 数据返回。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shopkeeper.orchestrator.definition import PIPELINE_V1, STEP_LABELS
from shopkeeper.orchestrator.store import OrchestratorStore
from shopkeeper.workflow.store import WorkflowStore

MISSING = "缺数据"

_DISPLAY = {
    "succeeded": ("ok", "成功"),
    "failed": ("failed", "失败"),
    "awaiting_approval": ("waiting_approval", "等待人工审批"),
    "suspended": ("suspended", "已挂起"),
    "budget_paused": ("suspended", "预算挂起"),
    "running": ("running", "运行中"),
    "pending": ("pending", "未开始"),
    "skipped": ("skipped", "已跳过"),
    "cancelled": ("cancelled", "已取消"),
}

_APPROVAL_OUTPUT_KEYS = (
    "approval_id",
    "pricing_approval_id",
    "dropship_approval_id",
    "tracking_approval_id",
)
_CONTEXT_APPROVAL_KEY = {
    "human_approval": "approval_id",
    "pricing": "pricing_approval_id",
    "dropship": "dropship_approval_id",
    "tracking": "tracking_approval_id",
}
_AI_RATIONALE_STEPS = frozenset({"scout", "pricing", "stylist", "script", "outreach"})
_STEP_ORDER = {step.id: index for index, step in enumerate(PIPELINE_V1.steps)}
_STEP_DESC = {step.id: step.description for step in PIPELINE_V1.steps}


def build_orch_run_list(
    workflow: WorkflowStore,
    *,
    limit: int = 50,
    shop_id: str | None = None,
) -> dict[str, Any]:
    """最近 run 列表。``shop_id`` 为空则不按店过滤。"""
    capped = max(1, min(int(limit), 100))
    runs = OrchestratorStore(workflow).list_runs(limit=capped, shop_id=shop_id)
    grouped = _load_steps(workflow, [run["id"] for run in runs])
    return {
        "read_only": True,
        "sources": ["orch_runs", "orch_steps", "approvals"],
        "limit": capped,
        "shop_id": shop_id,
        "runs": [_run_card(workflow, run, grouped.get(run["id"], [])) for run in runs],
    }


def build_orch_run_detail(workflow: WorkflowStore, run_id: str) -> dict[str, Any] | None:
    """单条 run 的逐步时间线。不存在 → None。"""
    detail = OrchestratorStore(workflow).run_detail(run_id)
    if detail is None:
        return None
    steps = _order_steps(detail.get("steps") or [])
    usage = _load_llm_usage(workflow, run_id)
    card = _run_card(workflow, detail, steps)
    card["error"] = _error_data(detail.get("error"))
    return {
        "read_only": True,
        "sources": ["orch_runs", "orch_steps", "orch_llm_usage", "approvals"],
        "run": card,
        "timeline": [_step_card(workflow, detail, step, usage.get(step["step_id"])) for step in steps],
    }


def _run_card(workflow: WorkflowStore, run: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = _order_steps(steps)
    started = run.get("created_at")
    finished = run.get("finished_at")
    open_ended = not finished
    end = finished or run.get("updated_at")
    seconds = _duration_seconds(started, end)
    display_status, display_label = _display(run.get("status"))
    stopped = _stopped_step(run, ordered)
    pending = None
    if display_status == "waiting_approval":
        current_id = run.get("current_step") or stopped
        current = next((step for step in ordered if step.get("step_id") == current_id), None)
        if current is not None:
            pending = _step_approval(workflow, current, run)
        if pending is None:
            for step in ordered:
                if step.get("status") == "awaiting_approval":
                    pending = _step_approval(workflow, step, run)
                    if pending is not None:
                        break
    return {
        "id": run["id"],
        "flow_id": run.get("flow_id"),
        "shop_id": run.get("shop_id"),
        "status": run.get("status"),
        "display_status": display_status,
        "display_label": display_label,
        "product": _product(run, ordered),
        "started_at": started,
        "finished_at": finished,
        "updated_at": run.get("updated_at"),
        "duration_seconds": seconds,
        "duration_label": _duration_label(seconds, open_ended=open_ended and seconds is not None),
        "duration_open": open_ended,
        "stopped_step": stopped,
        "stopped_step_label": STEP_LABELS.get(stopped, stopped) if stopped else "—",
        "pending_approval": pending,
    }


def _step_card(
    workflow: WorkflowStore,
    run: dict[str, Any],
    step: dict[str, Any],
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    display_status, display_label = _display(step.get("status"))
    started = step.get("started_at")
    finished = step.get("finished_at")
    seconds = _duration_seconds(started, finished)
    return {
        "step_id": step.get("step_id"),
        "label": STEP_LABELS.get(str(step.get("step_id")), str(step.get("step_id") or "—")),
        "description": _STEP_DESC.get(str(step.get("step_id")), ""),
        "status": step.get("status"),
        "display_status": display_status,
        "display_label": display_label,
        "attempt": int(step.get("attempt") or 0),
        "started_at": started,
        "finished_at": finished,
        "duration_seconds": seconds,
        "duration_label": _duration_label(seconds) if started or finished else MISSING,
        "input_summary": _input_summary(step),
        "output_summary": _output_summary(step),
        "llm": _llm_block(usage, step),
        "error": _error_data(step.get("error")),
        "approval": _step_approval(workflow, step, run),
    }


def _display(status: Any) -> tuple[str, str]:
    key = str(status or "")
    if key in _DISPLAY:
        return _DISPLAY[key]
    return (key or "unknown", key or "—")


def _stopped_step(run: dict[str, Any], steps: list[dict[str, Any]]) -> str | None:
    current = run.get("current_step")
    if isinstance(current, str) and current.strip():
        return current
    last: str | None = None
    for step in steps:
        if str(step.get("status") or "") != "pending":
            last = str(step.get("step_id") or "") or last
    return last


def _product(run: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    raw_in = run.get("input") if isinstance(run.get("input"), dict) else {}
    url = raw_in.get("url") if isinstance(raw_in.get("url"), str) else None
    title: str | None = None
    for step in steps:
        out = step.get("output") if isinstance(step.get("output"), dict) else {}
        facts = out.get("facts") if isinstance(out.get("facts"), dict) else {}
        raw_title = facts.get("title_raw")
        if isinstance(raw_title, str) and raw_title.strip():
            title = raw_title.strip()
            source = facts.get("source_url")
            if isinstance(source, str) and source.strip():
                url = source.strip()
            break
    if title is None:
        for step in steps:
            out = step.get("output") if isinstance(step.get("output"), dict) else {}
            copy = out.get("copy") if isinstance(out.get("copy"), dict) else {}
            for key in ("title_th", "title_en"):
                value = copy.get(key)
                if isinstance(value, str) and value.strip():
                    title = value.strip()
                    break
            if title:
                break
    clean_url = url.strip() if isinstance(url, str) else ""
    return {
        "title": _clip(title, 160) if title else None,
        "title_label": _clip(title, 80) if title else MISSING,
        "url": _clip(clean_url, 200) if clean_url else None,
    }


def _input_summary(step: dict[str, Any]) -> list[dict[str, str]]:
    raw = step.get("input") if isinstance(step.get("input"), dict) else {}
    run_input = raw.get("run_input") if isinstance(raw.get("run_input"), dict) else {}
    items: list[dict[str, str]] = []
    _add(items, "链接", run_input.get("url"))
    if isinstance(run_input.get("target_price_cents"), int):
        _add(items, "目标价（分）", run_input.get("target_price_cents"))
    if isinstance(run_input.get("duration_s"), int):
        _add(items, "时长（秒）", run_input.get("duration_s"))
    return items


def _output_summary(step: dict[str, Any]) -> list[dict[str, str]]:
    sid = str(step.get("step_id") or "")
    out = step.get("output") if isinstance(step.get("output"), dict) else {}
    items: list[dict[str, str]] = []
    if sid == "scout":
        facts = out.get("facts") if isinstance(out.get("facts"), dict) else {}
        _add(items, "商品", facts.get("title_raw"))
        if isinstance(facts.get("price_cents"), int):
            currency = facts.get("currency") if isinstance(facts.get("currency"), str) else ""
            _add(items, "货源价（分）", f"{facts['price_cents']} {currency}".strip())
        _add(items, "货号", facts.get("source_item_id"))
        if isinstance(out.get("recommend"), bool):
            _add(items, "建议上架", out.get("recommend"))
    elif sid == "pricing":
        _add(items, "建议价", out.get("suggested_price_thb"))
        _add(items, "价格来源", out.get("price_source"))
        _add(items, "审批状态", out.get("status"))
    elif sid == "stylist":
        copy = out.get("copy") if isinstance(out.get("copy"), dict) else {}
        _add(items, "泰文标题", copy.get("title_th"))
        _add(items, "英文标题", copy.get("title_en"))
    elif sid == "qa":
        if "passed" in out:
            _add(items, "通过", out.get("passed"))
        _add(items, "反馈", out.get("feedback"))
    elif sid == "script":
        script = out.get("script") if isinstance(out.get("script"), dict) else {}
        duration = out.get("duration_s") if out.get("duration_s") is not None else script.get("duration_s")
        _add(items, "时长（秒）", duration)
        cart = out.get("cart") if "cart" in out else script.get("cart")
        if isinstance(cart, bool):
            _add(items, "挂车", cart)
        hooks = script.get("hooks") if isinstance(script.get("hooks"), list) else []
        if hooks and isinstance(hooks[0], dict):
            _add(items, "Hook", hooks[0].get("voice_zh") or hooks[0].get("voice_th"))
        shots = script.get("shots") if isinstance(script.get("shots"), list) else []
        if shots:
            _add(items, "分镜数", len(shots))
        hits = out.get("ungrounded")
        if isinstance(hits, list) and hits:
            _add(items, "未证实说法", "、".join(str(item) for item in hits[:6]))
    elif sid == "outreach":
        count = out.get("count")
        if not isinstance(count, int) and isinstance(out.get("draft_ids"), list):
            count = len(out["draft_ids"])
        _add(items, "草稿数", count)
        if "sent" in out:
            _add(items, "已发送", out.get("sent"))
        if out.get("auto_send") is False:
            _add(items, "自动发送", False)
        hits = out.get("ungrounded")
        if isinstance(hits, list) and hits:
            _add(items, "未证实说法", "、".join(str(item) for item in hits[:6]))
    elif sid == "human_approval":
        _add(items, "草稿", out.get("draft_id"))
        _add(items, "审批单", out.get("approval_id"))
        _add(items, "状态", out.get("status"))
    elif sid == "publish":
        _add(items, "平台商品", out.get("platform_product_id"))
        _add(items, "状态", out.get("status"))
        if out.get("blocked") is True:
            _add(items, "真写", "未放行")
    elif sid == "order_sync":
        _add(items, "订单", out.get("order_id"))
        if "synced" in out:
            _add(items, "已同步", out.get("synced"))
    elif sid == "dropship":
        _add(items, "订单", out.get("order_id"))
        _add(items, "审批单", out.get("approval_id"))
    elif sid == "tracking":
        _add(items, "审批单", out.get("approval_id"))
    elif sid == "cs":
        _add(items, "说明", out.get("note"))
        if "sent_stub" in out:
            _add(items, "本地 stub 发送", out.get("sent_stub"))
    rationale = _rationale(step)
    if rationale and sid in _AI_RATIONALE_STEPS:
        _add(items, "理由", rationale)
    return items[:8]


def _step_approval(
    workflow: WorkflowStore,
    step: dict[str, Any],
    run: dict[str, Any],
) -> dict[str, Any] | None:
    out = step.get("output") if isinstance(step.get("output"), dict) else {}
    approval_id = ""
    for key in _APPROVAL_OUTPUT_KEYS:
        value = out.get(key)
        if isinstance(value, str) and value.strip():
            approval_id = value.strip()
            break
    if not approval_id and step.get("status") == "awaiting_approval":
        ctx = run.get("context") if isinstance(run.get("context"), dict) else {}
        key = _CONTEXT_APPROVAL_KEY.get(str(step.get("step_id") or ""))
        value = ctx.get(key) if key else None
        if isinstance(value, str) and value.strip():
            approval_id = value.strip()
    if not approval_id:
        return None
    row = workflow.get_approval(approval_id)
    status = str(row["status"]) if row and row.get("status") else None
    return {
        "id": approval_id,
        "status": status or "missing",
        "status_label": status or MISSING,
        "pending": status == "pending",
        "href": f"/admin/approvals?id={approval_id}",
    }


def _llm_block(usage: dict[str, Any] | None, step: dict[str, Any]) -> dict[str, Any]:
    if usage and int(usage.get("calls") or 0) > 0:
        return {
            "status": "recorded",
            "label": None,
            "source": "orch_llm_usage",
            "calls": int(usage["calls"]),
            "prompt_tokens": int(usage["prompt_tokens"]),
            "completion_tokens": int(usage["completion_tokens"]),
            "cost_usd": round(float(usage["cost_usd"]), 6),
            "model": usage.get("model") or None,
        }
    trace = {"found": False, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "model": None}
    raw = step.get("ai_trace")
    traces = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) and raw else [])
    for item in traces:
        if not isinstance(item, dict):
            continue
        if not any(key in item for key in ("prompt_tokens", "completion_tokens", "cost_usd")):
            continue
        trace["found"] = True
        trace["prompt_tokens"] += int(item.get("prompt_tokens") or 0)
        trace["completion_tokens"] += int(item.get("completion_tokens") or 0)
        try:
            trace["cost_usd"] += float(item.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
        if item.get("model") and not trace["model"]:
            trace["model"] = str(item["model"])
    if trace["found"]:
        return {
            "status": "recorded",
            "label": None,
            "source": "orch_steps.ai_trace",
            "calls": 0,
            "prompt_tokens": int(trace["prompt_tokens"]),
            "completion_tokens": int(trace["completion_tokens"]),
            "cost_usd": round(float(trace["cost_usd"]), 6),
            "model": trace["model"],
        }
    return {
        "status": "missing",
        "label": MISSING,
        "source": None,
        "calls": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cost_usd": None,
        "model": None,
    }


def _error_data(error: Any) -> dict[str, Any] | None:
    if not error:
        return None
    if not isinstance(error, dict):
        return {"code": "ORCH_ERROR", "message": _clip(error, 300)}
    payload: dict[str, Any] = {
        "code": str(error.get("code") or "ORCH_ERROR"),
        "message": _clip(error.get("message") or "", 300),
    }
    hits = error.get("hits")
    if isinstance(hits, list):
        cleaned = [_clip(item, 80) for item in hits if isinstance(item, (str, int)) and not isinstance(item, bool)]
        cleaned = [item for item in cleaned if item]
        if cleaned:
            payload["hits"] = cleaned[:8]
    return payload


def _rationale(step: dict[str, Any]) -> str:
    raw = step.get("ai_trace")
    traces = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) and raw else [])
    last = ""
    for item in traces:
        if isinstance(item, dict) and item.get("rationale"):
            last = str(item["rationale"])
    return last


def _add(items: list[dict[str, str]], label: str, value: Any) -> None:
    if isinstance(value, bool):
        text = "是" if value else "否"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
    elif isinstance(value, str):
        text = _clip(value)
    else:
        return
    if not text:
        return
    items.append({"label": label, "value": text})


def _clip(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _duration_seconds(start: Any, end: Any) -> int | None:
    left = _parse_iso(start)
    right = _parse_iso(end)
    if left is None or right is None:
        return None
    return max(0, int((right - left).total_seconds()))


def _duration_label(seconds: int | None, *, open_ended: bool = False) -> str:
    if seconds is None:
        return MISSING
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        text = f"{hours}小时{minutes}分"
    elif minutes:
        text = f"{minutes}分{sec}秒"
    else:
        text = f"{sec}秒"
    if open_ended:
        text += "（至最近更新）"
    return text


def _order_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(steps, key=lambda step: _STEP_ORDER.get(str(step.get("step_id")), 99))


def _load_steps(workflow: WorkflowStore, run_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in run_ids}
    if not run_ids:
        return grouped
    marks = ",".join("?" for _ in run_ids)
    with workflow._connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM orch_steps WHERE run_id IN ({marks})",
            run_ids,
        ).fetchall()
    steps = [OrchestratorStore._step_view(row) for row in rows]
    for step in _order_steps(steps):
        grouped.setdefault(step["run_id"], []).append(step)
    return grouped


def _load_llm_usage(workflow: WorkflowStore, run_id: str) -> dict[str, dict[str, Any]]:
    with workflow._connect() as conn:
        rows = conn.execute(
            """
            SELECT step_id,
                   COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd,
                   MAX(model) AS model
            FROM orch_llm_usage
            WHERE run_id = ?
            GROUP BY step_id
            """,
            (run_id,),
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[str(row["step_id"])] = {
            "calls": int(row["calls"]),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "cost_usd": float(row["cost_usd"]),
            "model": row["model"],
        }
    return out
