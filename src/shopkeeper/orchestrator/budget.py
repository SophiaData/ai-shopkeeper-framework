"""每日 LLM 预算上限（LLM_DAILY_BUDGET_USD）。超出暂停并告警。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from shopkeeper.site_profile import business_tz
from shopkeeper.workflow.alerts import emit_alert

CODE_LLM_BUDGET_EXCEEDED = "LLM_BUDGET_EXCEEDED"


def daily_budget_usd(env: dict[str, str] | None = None) -> float | None:
    """None = 不限额；0 = 立刻超限（测试用）。"""
    source = env if env is not None else os.environ
    raw = str(source.get("LLM_DAILY_BUDGET_USD", "") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def budget_local_date(now: datetime | None = None) -> str:
    """预算日。时区与利润日、调度日相同，都是 ``business_tz()``。默认 Asia/Bangkok。"""
    zone = business_tz()
    ts = now or datetime.now(zone)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc).astimezone(zone)
    else:
        ts = ts.astimezone(zone)
    return ts.date().isoformat()


def check_budget(
    spent_usd: float,
    *,
    env: dict[str, str] | None = None,
    emit: bool = True,
    run_id: str = "",
) -> dict[str, Any]:
    """返回 ok / exceeded + limit / spent。超出时可选告警。"""
    limit = daily_budget_usd(env)
    if limit is None:
        return {"ok": True, "limit": None, "spent": spent_usd, "exceeded": False}
    exceeded = spent_usd >= limit
    if exceeded and emit:
        emit_alert(CODE_LLM_BUDGET_EXCEEDED, job_type="orchestrator", order_id=run_id or "-")
    return {
        "ok": not exceeded,
        "limit": limit,
        "spent": spent_usd,
        "exceeded": exceeded,
    }
