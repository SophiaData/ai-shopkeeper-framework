"""可选自动启动：ORCHESTRATOR_AUTORUN=0 默认关。

开启后每日（业务时区，默认曼谷）选品 N 个候选 URL 并推进到审批挂起。
候选来自 ORCHESTRATOR_AUTORUN_URLS（逗号分隔）或 RESEARCH_INPUT_PATH CSV 的 url 列。
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from shopkeeper.logging_setup import get_logger
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.site_profile import business_tz
from shopkeeper.workflow.store import WorkflowStore

log = get_logger("shopkeeper.orchestrator.scheduler")

AUTORUN_JOB = "orchestrator_autorun"


def autorun_enabled(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("ORCHESTRATOR_AUTORUN", "0") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def autorun_limit(env: dict[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    raw = str(source.get("ORCHESTRATOR_AUTORUN_LIMIT", "3") or "3").strip()
    try:
        return max(1, min(int(raw), 20))
    except ValueError:
        return 3


def order_autorun_enabled(env: dict[str, str] | None = None) -> bool:
    """已同步订单的后台持续编排开关。默认关；与候选 URL 的每日 autorun 相互独立。"""
    source = env if env is not None else os.environ
    raw = str(source.get("ORCHESTRATOR_ORDER_AUTORUN", "0") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def order_autorun_limit(env: dict[str, str] | None = None) -> int:
    source = env if env is not None else os.environ
    raw = str(source.get("ORCHESTRATOR_ORDER_AUTORUN_LIMIT", "5") or "5").strip()
    try:
        return max(1, min(int(raw), 50))
    except ValueError:
        return 5


def candidate_urls(env: dict[str, str] | None = None) -> list[str]:
    source = env if env is not None else os.environ
    raw = str(source.get("ORCHESTRATOR_AUTORUN_URLS", "") or "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    path = str(source.get("RESEARCH_INPUT_PATH", "") or "").strip()
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    urls: list[str] = []
    try:
        with p.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                u = (row.get("url") or row.get("link") or row.get("source_url") or "").strip()
                if u:
                    urls.append(u)
    except OSError as exc:
        log.error("autorun candidates read failed path=%s err=%s", path, exc)
        return []
    return urls


def tick_autorun(
    orch: Orchestrator,
    store: WorkflowStore,
    *,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """每日至多跑一批。返回本次启动的 run 摘要。"""
    source = env if env is not None else os.environ
    started_orders: list[dict[str, Any]] = []
    if order_autorun_enabled(source):
        try:
            started_orders = orch.tick_order_fulfillment(
                limit=order_autorun_limit(source)
            )
        except Exception:  # noqa: BLE001 - 订单 tick 失败不拖垮 URL autorun
            log.exception("order fulfillment autorun tick failed")
    if not autorun_enabled(source):
        return started_orders
    zone = business_tz()
    ts = now or datetime.now(zone)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=zone)
    else:
        ts = ts.astimezone(zone)
    local_date = ts.date().isoformat()
    if not store.claim_job(AUTORUN_JOB, local_date):
        return []

    urls = candidate_urls(source)[: autorun_limit(source)]
    started: list[dict[str, Any]] = list(started_orders)
    try:
        if not urls:
            # ⚠️ **必须释放 claim**，否则当天的格子被永久占住。
            #
            # 原实现直接 `return []`，于是 `job_runs` 里留下一条
            # `report_id=''` 的僵尸行，两个后果：
            #   1. 当天再也不会尝试（claim_job 永远返回 False）——
            #      配置当天补上候选 URL 也没用，只能等第二天
            #   2. `job_ran()` 只看「有没有行」，于是**状态页会显示「今天已跑过」**，
            #      而实际上一个 run 都没启动
            #
            # 这与本项目踩过的「REAPER 定义了却从不触发」是同一类锁死模式：
            # 一个「还没做」的格子被记成「已经做了」。
            log.info("autorun skipped: no candidate urls date=%s", local_date)
            store.release_job(AUTORUN_JOB, local_date)
            return started
        for url in urls:
            try:
                detail = orch.start(url=url, advance=True)
                started.append(
                    {
                        "run_id": detail.get("id"),
                        "status": detail.get("status"),
                        "url": url,
                    }
                )
            except Exception:  # noqa: BLE001
                log.exception("autorun start failed url=%s", url)
        return started
    except Exception:
        store.release_job(AUTORUN_JOB, local_date)
        raise
