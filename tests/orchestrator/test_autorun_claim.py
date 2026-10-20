"""autorun 的每日 claim 不能被空跑永久占住（独立复核发现）。

`tick_autorun` 先 `claim_job` 占当天的格子，再读候选 URL。原实现在
**没有候选 URL** 时直接 `return []` —— **没有释放 claim**。

两个后果：

1. 当天再也不会尝试（`claim_job` 永远返回 False）——配好 URL 也只能等第二天
2. `job_ran()` 只看「有没有行」，于是**状态页显示「今天已跑过」**，
   而实际上一个 run 都没启动

这与本项目踩过的「REAPER 定义了却从不触发」是同一类锁死模式：
**一个「还没做」的格子被记成「已经做了」**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.orchestrator.scheduler import AUTORUN_JOB, tick_autorun
from shopkeeper.workflow.store import WorkflowStore


class _FakeOrch:
    def __init__(self) -> None:
        self.started: list[str] = []

    def start(self, **kw):
        self.started.append(str(kw.get("url") or ""))
        return {"id": "r1", "status": "awaiting_approval"}


@pytest.fixture
def store(tmp_path):
    return WorkflowStore(tmp_path / "w.db")


def test_empty_urls_releases_claim(store):
    """没有候选 URL 时**必须释放** claim，不留僵尸行。"""
    orch = _FakeOrch()
    assert tick_autorun(orch, store, env={"ORCHESTRATOR_AUTORUN": "1"}) == []

    with store._connect() as conn:  # noqa: SLF001
        rows = [dict(r) for r in conn.execute("SELECT * FROM job_runs")]
    assert rows == [], f"不该留僵尸行，实际 {rows}"


def test_empty_urls_does_not_report_as_ran(store):
    """空跑之后 `job_ran` 必须是 False —— 否则状态页会谎报「今天已跑过」。"""
    tick_autorun(_FakeOrch(), store, env={"ORCHESTRATOR_AUTORUN": "1"})
    from datetime import datetime

    from shopkeeper.workflow.jobs import BANGKOK

    today = datetime.now(BANGKOK).date().isoformat()
    assert store.job_ran(AUTORUN_JOB, today) is False


def test_can_run_after_urls_appear(store):
    """空跑一次之后，补上 URL 仍然能在**同一天**跑 —— 这才说明 claim 真释放了。"""
    orch = _FakeOrch()
    tick_autorun(orch, store, env={"ORCHESTRATOR_AUTORUN": "1"})

    result = tick_autorun(
        orch, store, env={"ORCHESTRATOR_AUTORUN": "1", "ORCHESTRATOR_AUTORUN_URLS": "https://x/1"}
    )
    assert len(result) == 1, f"补上 URL 后应能启动，实际 {result}"
    assert orch.started == ["https://x/1"]


def test_successful_run_still_claims_the_day(store):
    """**反向**：真正跑过的日子仍要占住格子（否则会一天跑多次）。"""
    orch = _FakeOrch()
    env = {"ORCHESTRATOR_AUTORUN": "1", "ORCHESTRATOR_AUTORUN_URLS": "https://x/1"}
    assert len(tick_autorun(orch, store, env=env)) == 1

    second = tick_autorun(orch, store, env=env)
    assert second == [], "同一天不该跑第二批"
    assert len(orch.started) == 1


def test_disabled_by_default(store):
    """默认关（`ORCHESTRATOR_AUTORUN=0`）时不动任何状态。"""
    orch = _FakeOrch()
    assert tick_autorun(orch, store, env={}) == []
    assert orch.started == []
    with store._connect() as conn:  # noqa: SLF001
        assert [dict(r) for r in conn.execute("SELECT * FROM job_runs")] == []


def test_advance_stops_at_approval_gate(tmp_path):
    """`advance=True` 只能推进到**审批挂起**，不得越过人工闸门。

    `tick_autorun` 用 `advance=True`，所以这条不变量是自动化的边界所在：
    它可以生成内容、建审批单，但**不能**执行上架（那是涉钱动作）。
    """
    from shopkeeper.adapters.mock import MockPlatform
    from shopkeeper.orchestrator.engine import Orchestrator

    store = WorkflowStore(tmp_path / "w.db")
    orch = Orchestrator(store, platform=MockPlatform(), env={"LLM_PROVIDER": "mock"})
    detail = orch.start(url="https://detail.1688.com/offer/1.html", advance=True)

    assert detail.get("status") == "awaiting_approval"
    approvals = [(a["action_type"], a["status"]) for a in store.list_approvals()]
    assert approvals == [("create_listing", "pending")], approvals
    assert all(a["executed_at"] is None for a in store.list_approvals()), "不得自动执行"
