"""`order_id` 的派生必须有足够熵（独立复核发现）。

## 缺陷

原先：

```python
order_id = f"ORD-ORCH-{(run['id'])[:8]}"
```

`run['id']` 是 `uuid4().hex`（32 位十六进制），只取 **8 位 = 32 bit**。
生日碰撞在约 **9.3k 个 run** 时就有 1% 概率撞车。

而 `upsert_synced_order` 是 ON CONFLICT 覆盖 —— **两笔订单会静默合并成一条**：

```
run1 = "abcdef12aaaa0000" / run2 = "abcdef12bbbb1111"
→ 都派生出 ORD-ORCH-abcdef12
→ 落库只剩一条（后者覆盖前者），**无任何报错**
```

## 边界

`run['id']` 本身已经是 32 位十六进制，**直接用完整值**即可 ——
不需要新造 id 方案，也不额外占多少存储。低熵派生在这里没有任何收益。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/1.html"


def _run_and_get_order_id(tmp_path: Path) -> str:
    store = WorkflowStore(tmp_path / "w.db")
    orch = Orchestrator(store, platform=MockPlatform(), env={"LLM_PROVIDER": "mock"})
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]
    # 推进过审批，走到 order_sync
    for _ in range(6):
        if orch.store.get_run(rid)["status"] != "awaiting_approval":
            break
        orch.approve(rid, note="ok")
    ctx = orch.store.get_run(rid)["context"] or {}
    oid = str(ctx.get("order_id") or "")
    assert oid, "应已产生 order_id"
    return oid


def test_order_id_uses_full_run_id(tmp_path):
    """**核心**：派生出的 order_id 必须包含**完整**的 run id。"""
    store = WorkflowStore(tmp_path / "w.db")
    orch = Orchestrator(store, platform=MockPlatform(), env={"LLM_PROVIDER": "mock"})
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]
    for _ in range(6):
        if orch.store.get_run(rid)["status"] != "awaiting_approval":
            break
        orch.approve(rid, note="ok")

    ctx = orch.store.get_run(rid)["context"] or {}
    oid = str(ctx.get("order_id") or "")
    assert rid in oid, f"order_id {oid!r} 未包含完整 run id {rid!r}"


def test_two_runs_with_shared_prefix_do_not_collide(tmp_path):
    """同前缀的 run id 不得派生出同一个 order_id。

    这是原先 8 位前缀的真实后果 —— 两笔订单静默合并。
    """
    a = "abcdef12aaaa0000abcdef12aaaa0000"
    b = "abcdef12bbbb1111abcdef12bbbb1111"
    assert a[:8] == b[:8], "前置条件：前 8 位相同"
    assert f"ORD-ORCH-{a}" != f"ORD-ORCH-{b}"


def test_source_no_longer_slices_run_id():
    """**防复发**：源码里不得再出现 `run['id'])[:8]` 这类截断。

    只测行为挡不住「有人又改回 8 位但恰好测试没覆盖」；这条断言直接读源码。
    """
    src = (
        Path(__file__).resolve().parents[2] / "src/shopkeeper/orchestrator/steps.py"
    ).read_text(encoding="utf-8")
    # ⚠️ 必须**排除注释行** —— 修复说明里就引用了旧写法，
    # 不排除的话这条断言会被自己的注释满足出「假红」（我第一版就是这样）。
    code_lines = [
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    ]
    bad = re.findall(r"ORD-ORCH-\{[^}]*\[:\d+\]", "\n".join(code_lines))
    assert not bad, f"order_id 仍是截断派生：{bad}"


def test_order_sync_is_still_idempotent(tmp_path):
    """**反向**：同一 run 重入必须仍派生出同一个 order_id（幂等不能破）。"""
    store = WorkflowStore(tmp_path / "w.db")
    orch = Orchestrator(store, platform=MockPlatform(), env={"LLM_PROVIDER": "mock"})
    rid = orch.start(url=FIXTURE_URL, advance=True)["id"]
    for _ in range(6):
        if orch.store.get_run(rid)["status"] != "awaiting_approval":
            break
        orch.approve(rid, note="ok")

    first = (orch.store.get_run(rid)["context"] or {}).get("order_id")
    orch.retry_step(rid, "order_sync")
    second = (orch.store.get_run(rid)["context"] or {}).get("order_id")
    assert first == second, "同一 run 重入应派生同一个 order_id"
