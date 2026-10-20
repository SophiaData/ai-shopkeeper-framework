"""P2-04 库存断货熔断。

## 缺口（2026-10-16 实测）

`stock.py` 的 `push_stock` 在 `desired_qty=0` 时**安静地推 0**：
没有告警、不生成下架审批单。**供应商断货了，卖家不会知道**，商品继续按原价挂着，
直到有人下单才发现发不出货。

## 本模块的边界（用例重点）

只**提醒 + 生成审批单**：不自动下架、不调平台写接口、不自动改库存。
下架是涉钱动作，必须过 `approvals` 八态闸门（AGENTS 规则 5）。
"""

from __future__ import annotations

import pytest

from shopkeeper.workflow.stock_circuit import (
    DELIST_ACTION,
    LOW_STOCK_THRESHOLD,
    STATE_LOW,
    STATE_OK,
    STATE_OUT,
    build_delist_payload,
    evaluate_stock_state,
    propose_delist,
    should_alert,
)
from shopkeeper.workflow.drafts import DraftError
from shopkeeper.workflow.store import WorkflowStore


@pytest.fixture
def store(tmp_path):
    return WorkflowStore(tmp_path / "w.db")


def _delist_rows(store: WorkflowStore) -> list[dict]:
    return [a for a in store.list_approvals() if a["action_type"] == DELIST_ACTION]


# --------------------------------------------------------------------------
# 状态判定
# --------------------------------------------------------------------------


def test_zero_is_out_of_stock():
    assert evaluate_stock_state(0) == STATE_OUT


@pytest.mark.parametrize("qty", [-1, -100])
def test_negative_is_out_of_stock(qty):
    assert evaluate_stock_state(qty) == STATE_OUT


def test_healthy_quantity():
    assert evaluate_stock_state(10) == STATE_OK


def test_threshold_quantity_is_ok():
    assert evaluate_stock_state(LOW_STOCK_THRESHOLD) == STATE_OK


def test_garbage_quantity_is_low_not_ok():
    """拿不准时按「需要看一眼」处理 —— 不假装正常。"""
    assert evaluate_stock_state("不是数字") == STATE_LOW
    assert evaluate_stock_state(None) == STATE_LOW


def test_low_is_reachable_with_a_normal_integer():
    """**`low` 必须能用普通整数走到** —— 否则它是死状态。

    原先 `LOW_STOCK_THRESHOLD = 1`：`qty <= 0` 已吃掉 `out`，
    `qty < 1` 只剩 `qty == 0`，而它在上一条已返回 `out`，
    于是 `low` 对**任何整数**都不可达 —— DESIGN v1.73 承诺的「三态判定」实际只有两态。

    旧测试全绿是因为只从 `None` / 非数字走到 `low`，从没试过正常整数。
    """
    reachable = [q for q in range(-10, 30) if evaluate_stock_state(q) == STATE_LOW]
    assert reachable, "没有任何整数能让 evaluate_stock_state 返回 low —— 死状态"


def test_low_band_sits_between_out_and_ok():
    """`low` 是「快没了但还能卖」的警告带，必须夹在 `out` 与 `ok` 之间。"""
    assert evaluate_stock_state(0) == STATE_OUT
    assert evaluate_stock_state(1) == STATE_LOW
    assert evaluate_stock_state(LOW_STOCK_THRESHOLD) == STATE_OK
    assert LOW_STOCK_THRESHOLD > 1, "阈值 ≤1 会让 low 不可达（见上一个用例）"


def test_state_transition_is_monotonic():
    """数量递增时状态只能 `out → low → ok`，不能回跳。"""
    order = {STATE_OUT: 0, STATE_LOW: 1, STATE_OK: 2}
    prev = -1
    for q in range(0, 10):
        rank = order[evaluate_stock_state(q)]
        assert rank >= prev, f"qty={q} 时状态回跳了"
        prev = rank


def test_only_out_triggers_alert():
    """`low` 只进总览，避免频繁打扰。"""
    assert should_alert(STATE_OUT) is True
    assert should_alert(STATE_LOW) is False
    assert should_alert(STATE_OK) is False


# --------------------------------------------------------------------------
# 载荷
# --------------------------------------------------------------------------


def test_payload_declares_human_required():
    p = build_delist_payload(product_id="p1", title="收纳箱", desired_qty=0)
    assert p["payload"]["requires_human"] is True
    assert "不自动下架" in p["payload"]["note"]


def test_payload_is_not_a_write_action():
    """载荷里不得出现「已执行」之类的措辞，也不该带平台写参数。"""
    p = build_delist_payload(product_id="p1", desired_qty=0)
    blob = str(p)
    assert "executed" not in blob
    assert "delist_now" not in blob


# --------------------------------------------------------------------------
# 建单
# --------------------------------------------------------------------------


def test_no_approval_when_in_stock(store):
    assert propose_delist(store, product_id="p1", desired_qty=10) is None
    assert _delist_rows(store) == []


def test_no_approval_when_low_but_not_out(store):
    assert propose_delist(store, product_id="p1", desired_qty=LOW_STOCK_THRESHOLD) is None
    assert _delist_rows(store) == []


def test_creates_pending_approval_when_out(store):
    row = propose_delist(store, product_id="p1", title="收纳箱", desired_qty=0)
    assert row is not None
    assert row["action_type"] == DELIST_ACTION
    assert row["status"] == "pending"
    assert row["entity_type"] == "product"
    assert row["entity_id"] == "p1"


def test_approval_has_deadline(store):
    """断货单不该无限期挂着。"""
    row = propose_delist(store, product_id="p1", desired_qty=0)
    assert row["decide_before"]


def test_repeated_stockout_does_not_spam_approvals(store):
    """幂等：同一商品已有 pending 单时不再重复建 —— 否则审批队列会被刷满。"""
    first = propose_delist(store, product_id="p1", desired_qty=0)
    second = propose_delist(store, product_id="p1", desired_qty=0)
    third = propose_delist(store, product_id="p1", desired_qty=0)

    assert first["id"] == second["id"] == third["id"]
    assert len(_delist_rows(store)) == 1


def test_different_products_get_separate_approvals(store):
    propose_delist(store, product_id="p1", desired_qty=0)
    propose_delist(store, product_id="p2", desired_qty=0)
    assert len(_delist_rows(store)) == 2


def test_approval_not_auto_executed(store):
    """核心边界：建单不等于执行。"""
    row = propose_delist(store, product_id="p1", desired_qty=0)
    assert row["executed_at"] is None
    assert row["status"] == "pending"


# --------------------------------------------------------------------------
# 告警
# --------------------------------------------------------------------------


def _capture_alerts(monkeypatch) -> list[tuple]:
    """`stock_circuit` 里 `emit_alert` 是**函数内延迟导入**的，
    所以要 patch 它真正的来源模块 `alerts`，而不是 `stock_circuit` 的属性。"""
    fired: list[tuple] = []
    import shopkeeper.workflow.alerts as alerts_mod

    monkeypatch.setattr(
        alerts_mod,
        "emit_alert",
        lambda code, job_type="", order_id="": fired.append((code, order_id)),
    )
    return fired


def test_alert_fires_on_stockout(store, monkeypatch):
    fired = _capture_alerts(monkeypatch)
    propose_delist(store, product_id="p1", desired_qty=0)

    assert fired == [("STOCK_OUT_OF_STOCK", "p1")]


def test_alert_silent_when_in_stock(store, monkeypatch):
    fired = _capture_alerts(monkeypatch)
    propose_delist(store, product_id="p1", desired_qty=10)

    assert fired == []


def test_alert_still_fires_when_approval_already_exists(store, monkeypatch):
    """已建过单不该把后续断货一起静音 —— 告警与建单是两件事。"""
    propose_delist(store, product_id="p1", desired_qty=0)

    fired = _capture_alerts(monkeypatch)
    propose_delist(store, product_id="p1", desired_qty=0)

    assert fired == [("STOCK_OUT_OF_STOCK", "p1")]


# --------------------------------------------------------------------------
# 端到端：与真实 push_stock 路径一致
# --------------------------------------------------------------------------


def test_stockout_scenario_end_to_end(store, monkeypatch):
    """模拟真实场景：已上架商品断货 → 告警 + 待审下架单，且**没有真的下架**。

    这里用真实的 `push_stock` 门禁（它要求草稿已审批），确认熔断与推送是两条独立路径：
    熔断只管提醒，推送仍受审批与写开关约束。
    """
    from shopkeeper.adapters.tiktok_shop import OfflineTikTokPlatform
    from shopkeeper.workflow.stock import push_stock

    fired = _capture_alerts(monkeypatch)

    # 一个已审批的 listing（真实前提，否则 push_stock 自己就会拒绝）
    store.add_draft(
        kind="listing",
        status="approved",
        title="折叠收纳箱",
        body_th="",
        body_en="",
        payload={},
        qa={"ok": True},
        source="test",
    )
    listing_id = store.list_drafts()[0]["id"]

    # 供应商断货
    row = propose_delist(store, product_id=listing_id, title="折叠收纳箱", desired_qty=0)

    assert row["status"] == "pending", "应生成待审下架单"
    assert fired == [("STOCK_OUT_OF_STOCK", listing_id)], "应告警"
    assert row["executed_at"] is None, "建单不等于执行"

    # 只读平台：推送**抛 DraftError** 被拒（与熔断无关，是被写通道挡住）。
    # 顺带确认了「熔断只提醒」这条边界：断货不会绕过写通道自动改库存。
    with pytest.raises(DraftError, match="TTS_WRITE_BLOCKED"):
        push_stock(store, OfflineTikTokPlatform("offline"), listing_id, desired_qty=0)
