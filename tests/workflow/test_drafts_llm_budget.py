"""`/drafts/generate` 必须与编排器用**同一套** LLM 预算口径。

## 缺陷（独立复核发现）

两条 LLM 路径：

| 路径 | 预算门 | 记账 |
|---|---|---|
| 编排器 `orchestrator/llm.py` | ✅ `check_budget` | ✅ `record_llm_usage` |
| `workflow/drafts.py`（`/api/workflow/drafts/generate`） | ❌ **无** | ❌ **无** |

两者用**同一个 LLM 网关**，但后者既不检查也不记账。后果有两层：

1. **`LLM_DAILY_BUDGET_USD` 拦不住这条路径** —— 实测：预算设 0、编排器拒绝，
   本路径**照常执行**
2. 后台「今日剩余额度」只读 `orch_llm_usage`，本路径消耗**不计入** ——
   显示「花了 0、额度满」，是个会误导人的数字

## 本文件钉住三件事

- 预算超限 → **拒绝**，并给出可执行提示（调高上限 / 等次日）
- 未设预算 → **照常生成**（不能因为加了门就把默认路径锁死）
- 零成本 provider → **不记账**（不写 0 冒充已计量）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.workflow.drafts import DraftError, generate_drafts
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.store import WorkflowStore

ITEM = [{"title": "收纳盒"}]


@pytest.fixture
def store(tmp_path):
    return WorkflowStore(tmp_path / "w.db")


def _usage_rows(store: WorkflowStore) -> int:
    with store._connect() as conn:  # noqa: SLF001
        return int(conn.execute("SELECT COUNT(*) AS c FROM orch_llm_usage").fetchone()["c"])


def test_budget_exhausted_blocks_generation(store, monkeypatch):
    """**核心**：预算已用尽时必须拒绝。

    修复前：预算设 0、编排器拒绝，这条路径**照常执行**。
    """
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "0")
    with pytest.raises(DraftError, match="预算"):
        generate_drafts(store, MockChatProvider(), "listing", ITEM, shop_id="default")


def test_budget_error_gives_actionable_hint(store, monkeypatch):
    """只说「不行」不够 —— 要告诉操作者怎么恢复。"""
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "0")
    with pytest.raises(DraftError) as exc:
        generate_drafts(store, MockChatProvider(), "listing", ITEM, shop_id="default")
    msg = str(exc.value)
    assert "LLM_DAILY_BUDGET_USD" in msg
    assert "次日" in msg or "重置" in msg


def test_no_budget_configured_still_generates(store, monkeypatch):
    """**反向**：没设预算时不受影响 —— 别把默认路径锁死。"""
    monkeypatch.delenv("LLM_DAILY_BUDGET_USD", raising=False)
    out = generate_drafts(store, MockChatProvider(), "listing", ITEM, shop_id="default")
    assert len(out) == 1


def test_zero_cost_provider_is_not_recorded(store, monkeypatch):
    """零成本（mock / 模板）**不写用量表** —— 不写 0 冒充已计量。"""
    monkeypatch.delenv("LLM_DAILY_BUDGET_USD", raising=False)
    generate_drafts(store, MockChatProvider(), "listing", ITEM, shop_id="default")
    assert _usage_rows(store) == 0


def test_provider_without_usage_attr_does_not_crash(store, monkeypatch):
    """provider 接口上没有 `usage` 时不得因此失败。

    `MockChatProvider` 就没有这个属性 —— 记账必须**安静跳过**而不是抛错。
    """
    monkeypatch.delenv("LLM_DAILY_BUDGET_USD", raising=False)
    provider = MockChatProvider()
    assert not hasattr(provider, "usage")
    out = generate_drafts(store, provider, "listing", ITEM, shop_id="default")
    assert len(out) == 1
