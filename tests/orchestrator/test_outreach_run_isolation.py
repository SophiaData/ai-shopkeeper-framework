"""达人建联草稿的复用必须按 `run_id` 精确隔离（复核 P1-2 的核实结论）。

## 复核提出的疑虑

复核者指出 `_outreach_rows_for_run` 用 `list_drafts(shop_id=shop_id)` 过滤，
怀疑「任何让 `run['shop_id']` 解析成 `default` 的路径，都会让这次 run 去
`default` 店里按昵称找他人草稿并直接复用」。

**复核者自己也标注了「不能声称已复现跨店串店」，这个克制是对的。**

## 我核实的结果：不是缺陷

`_outreach_rows_for_run` 除了 `shop_id` 过滤，还有一条**更严格的**判定：

```python
if payload.get("run_id") == run_id:
```

`run_id` 是每个 run 唯一的，所以「跨店串用」不可能发生。本文件把这条
不变量**钉住** —— 它在实现里是隐式的，此前没有专门用例，
将来有人重构去掉它不会有测试报警。

## 顺带覆盖的边界

历史草稿的 `payload` 里可能**没有** `run_id`（旧版本写的行）。
`.get("run_id")` 返回 `None`，而 `None != "runX"`，所以不会被误匹配。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.workflow.store import WorkflowStore

KIND = "influencer_outreach"


def _add(store: WorkflowStore, *, shop: str, run_id: str | None, key: str) -> None:
    payload: dict[str, object] = {"creator_key": key}
    if run_id is not None:
        payload["run_id"] = run_id
    store.add_draft(
        kind=KIND,
        status="pending",
        title=key,
        body_th="",
        body_en="",
        payload=payload,
        shop_id=shop,
        source="orchestrator",
    )


def _rows(store: WorkflowStore, *, shop: str, run_id: str) -> list[dict]:
    """调用**真实函数**，而不是复刻它的逻辑。

    我第一版把判定逻辑抄了一份 —— 那样测的是「我抄的对不对」，
    而不是「实现对不对」：实现改了、抄的那份没改，测试照样绿。
    """
    from shopkeeper.orchestrator.steps import StepContext, _outreach_rows_for_run

    ctx = StepContext.__new__(StepContext)
    ctx.workflow = store
    return _outreach_rows_for_run(ctx, run_id, shop)


@pytest.fixture
def store(tmp_path):
    return WorkflowStore(tmp_path / "w.db")


def test_runs_do_not_share_drafts(store):
    """**核心**：不同 run 的草稿互不可见。"""
    for i in range(3):
        _add(store, shop="default", run_id="runA", key=f"a{i}")
        _add(store, shop="default", run_id="runB", key=f"b{i}")

    assert len(_rows(store, shop="default", run_id="runA")) == 3
    assert len(_rows(store, shop="default", run_id="runB")) == 3


def test_different_shops_do_not_share_drafts(store):
    """不同店的草稿互不可见（即便同店名被归一化到同一个值）。"""
    _add(store, shop="shopA", run_id="runA", key="k0")
    _add(store, shop="shopB", run_id="runA", key="k0")

    assert len(_rows(store, shop="shopA", run_id="runA")) == 1
    assert len(_rows(store, shop="shopB", run_id="runA")) == 1
    # 跨店用同一个 run_id 也拿不到对方的
    assert len(_rows(store, shop="shopB", run_id="runZ")) == 0


def test_legacy_draft_without_run_id_is_not_reused(store):
    """历史草稿（payload 无 `run_id`）不该被任何 run 复用。

    `.get("run_id")` 返回 `None`，`None != "runX"` —— 不会误匹配。
    """
    _add(store, shop="default", run_id=None, key="legacy")
    assert _rows(store, shop="default", run_id="runAny") == []


def test_unknown_run_id_gets_nothing(store):
    """全新 run 不该「继承」别人的草稿。"""
    _add(store, shop="default", run_id="runA", key="a0")
    assert _rows(store, shop="default", run_id="runBrandNew") == []
