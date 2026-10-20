"""人工重试必须有上限（独立复核发现的 P0-3）。

## 缺陷

`retry_step` 是公开端点 `POST /runs/{id}/steps/{sid}/retry`，而 `_execute_step`
每次都 `attempt + 1` 并**重新执行 handler** —— 对 `kind="ai"` 的步就是
**再调一次 LLM、再计一次费**。原先没有任何上限。

**实测（修复前）**：连续 6 次 `retry_step("stylist")` →
`orch_llm_usage` 从 5 条涨到 11 条，tokens 750 → 1650，**无一次被拦**。

`_execute_step` 里的 `RetryPolicy.max_retries` 只管**自动**重试；
人工重试走的是另一条路，此前完全没约束。

## 边界

这不是「禁止重试」—— 改稿重跑是正常操作，所以保留 `max_retries + 1` 次；
被拦时的错误信息明确告诉操作者替代路径（新建 run）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.orchestrator.engine import Orchestrator, OrchestratorError
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/1.html"
AI_STEP = "stylist"


@pytest.fixture
def env(tmp_path):
    store = WorkflowStore(tmp_path / "w.db")
    orch = Orchestrator(store, platform=MockPlatform(), env={"LLM_PROVIDER": "mock"})
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]
    return orch, store, rid


def _llm_calls(store: WorkflowStore, rid: str) -> int:
    with store._connect() as conn:  # noqa: SLF001
        return int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM orch_llm_usage WHERE run_id = ?", (rid,)
            ).fetchone()["n"]
        )


def test_retry_is_capped(env):
    """超过上限必须抛错，不能无限重跑。"""
    orch, store, rid = env
    allowed = 0
    with pytest.raises(OrchestratorError, match="人工重试上限"):
        for _ in range(20):
            orch.retry_step(rid, AI_STEP)
            allowed += 1
    assert allowed < 20, "必须有上限"
    assert allowed >= 1, "正常改稿重跑不该被禁"


def test_retry_cap_bounds_llm_spend(env):
    """**核心**：LLM 调用数必须有界 —— 这是省钱的那条不变量。"""
    orch, store, rid = env
    before = _llm_calls(store, rid)

    with pytest.raises(OrchestratorError):
        for _ in range(20):
            orch.retry_step(rid, AI_STEP)

    after = _llm_calls(store, rid)
    assert after - before <= 5, f"重试期间 LLM 调用涨了 {after - before} 次，应有界"


def test_error_message_gives_alternative(env):
    """被拦时要告诉操作者「怎么办」，不能只说不行。"""
    orch, store, rid = env
    with pytest.raises(OrchestratorError) as exc:
        for _ in range(20):
            orch.retry_step(rid, AI_STEP)
    assert "新建 run" in str(exc.value), "错误信息应给出可执行的替代路径"


def test_first_retry_still_works(env):
    """**反向**：第一次重试必须放行 —— 别把改稿重跑这条正常路径封死。"""
    orch, store, rid = env
    result = orch.retry_step(rid, AI_STEP)
    assert result is not None
    assert _llm_calls(store, rid) >= 1
