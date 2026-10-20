"""C2：订单状态机矩阵单测（DESIGN §9 M1-C 第 3 项 = M1 CI gate）。

权威口径：docs/SCHEMA.md §2.1「矩阵即法律」。本文件的期望表是**从文档逐格抄录**的独立副本，
review 时可与 SCHEMA 表格逐格对照；实现与本表不一致即为 bug。

覆盖：
  ① 11 态 × 14 事件 = 154 格逐格断言（格子行为 100% 覆盖）
  ② 三条可达性断言（非初始态有入边 / 非终态有出边 / 全状态从 CREATED 可达）
  ③ 两条路径级断言必须到 CLOSED（SCHEMA §2.1 样例路径 1、2）
  ④ rank 豁免五类 + 部分退款 qty 绝对重算
"""

from __future__ import annotations

from collections import deque

import pytest

from shopkeeper.domain.order_state import (
    AGGREGATION_EDGES,
    BOOTSTRAP_WHITELIST,
    EFFECT_ADJUDICATE,
    EFFECT_BACKFILL,
    EFFECT_ROLLBACK,
    EFFECT_WINDOW,
    ILLEGAL,
    MATRIX,
    RANK,
    RANK_EXEMPTIONS,
    TERMINAL_STATE,
    Event,
    OrderState,
    apply_event,
    cell_of,
    highest_reached,
    is_rank_exempt,
    recompute_from_lines,
    recompute_line_qty,
)

# ---------------------------------------------------------------------------
# 期望表：从 SCHEMA §2.1 矩阵逐格抄录
#   "T:STATE" 迁移 / "S" 到自身 / "S:xx" 到自身带效果 / "R" 回退（脚注⁵/¹⁰）
#   未列出的格子 = ILLEGAL（—）
# ---------------------------------------------------------------------------
EXPECTED: dict[OrderState, dict[Event, str]] = {
    OrderState.CREATED: {
        Event.PAID: "T:PAID",
        Event.BUYER_CANCEL: "T:CANCELLED",
        Event.PLATFORM_CLOSE: "T:CANCELLED",
    },
    OrderState.PAID: {
        Event.BUYER_CANCEL: "T:CANCELLED",
        Event.PO_CANCELLED: "T:CANCELLED",
        Event.PO_SUBMITTED: "T:FULFILLING",
        Event.REFUND_REQUESTED: "T:REFUND_REQUESTED",
        Event.PLATFORM_CLOSE: "T:CANCELLED",
    },
    OrderState.FULFILLING: {
        Event.BUYER_CANCEL: "T:CANCELLED",
        Event.PO_CANCELLED: "T:CANCELLED",
        Event.PO_SUBMITTED: "S",
        Event.PARTIAL_SHIP: "T:PARTIALLY_SHIPPED",
        Event.ALL_SHIPPED: "T:SHIPPED",
        Event.REFUND_REQUESTED: "T:REFUND_REQUESTED",
    },
    OrderState.PARTIALLY_SHIPPED: {
        Event.PO_CANCELLED: "S",
        Event.PO_SUBMITTED: "S",
        Event.PARTIAL_SHIP: "S",
        Event.ALL_SHIPPED: "T:SHIPPED",
        Event.DELIVERED: "T:DELIVERED",
        Event.RETURN_RECEIVED: "S",
        Event.REFUND_REQUESTED: "T:REFUND_REQUESTED",
        Event.REFUND_COMPLETED: "S",
    },
    OrderState.SHIPPED: {
        Event.ALL_SHIPPED: "S",
        Event.DELIVERED: "T:DELIVERED",
        Event.RETURN_RECEIVED: "S",
        Event.REFUND_REQUESTED: "T:REFUND_REQUESTED",
        Event.REFUND_COMPLETED: "S",
    },
    OrderState.DELIVERED: {
        Event.DELIVERED: "S",
        Event.RETURN_RECEIVED: "S",
        Event.REFUND_REQUESTED: "T:REFUND_REQUESTED",
        Event.REFUND_COMPLETED: "S",
        Event.PLATFORM_CLOSE: "T:COMPLETED",
    },
    OrderState.COMPLETED: {
        Event.RETURN_RECEIVED: "S",
        Event.REFUND_REQUESTED: "T:REFUND_REQUESTED",
        Event.REFUND_COMPLETED: "S",
        Event.DISPUTE_OPEN: "T:REFUND_REQUESTED",
        Event.PLATFORM_CLOSE: "T:CLOSED",
    },
    OrderState.REFUND_REQUESTED: {
        Event.ALL_SHIPPED: "S:backfill",
        Event.DELIVERED: "S:backfill",
        Event.RETURN_RECEIVED: "S",
        Event.REFUND_REQUESTED: "S",
        Event.REFUND_REJECTED: "R",
        Event.REFUND_COMPLETED: "S",
        Event.AFTERSALE_RESOLVED: "R",
    },
    OrderState.REFUNDED: {
        Event.REFUND_COMPLETED: "S:backfill",
        Event.PLATFORM_CLOSE: "T:CLOSED",
    },
    OrderState.CANCELLED: {
        Event.BUYER_CANCEL: "S",
        Event.PO_CANCELLED: "S",
        Event.REFUND_COMPLETED: "S:backfill",
        Event.PLATFORM_CLOSE: "T:CLOSED",
    },
    OrderState.CLOSED: {
        Event.REFUND_REQUESTED: "T:REFUND_REQUESTED",
        Event.REFUND_COMPLETED: "S",
        Event.DISPUTE_OPEN: "T:REFUND_REQUESTED",
        Event.PLATFORM_CLOSE: "S",
    },
}

ALL_CELLS = [(s, e) for s in OrderState for e in Event]


def _expect(state: OrderState, event: Event) -> str:
    return EXPECTED.get(state, {}).get(event, "-")


# ---------------------------------------------------------------------------
# ① 矩阵形状与逐格行为（154 格全覆盖）
# ---------------------------------------------------------------------------

def test_matrix_shape_is_exactly_11x14():
    assert len(OrderState) == 11, "状态数必须与 SCHEMA §2.1 一致"
    assert len(Event) == 14, "事件全集必须与 SCHEMA §2.1 一致（含 R6 补的 aftersale_resolved）"
    assert set(MATRIX) == set(OrderState)
    for s in OrderState:
        assert set(MATRIX[s]) == set(Event), f"{s} 行列不齐：矩阵必须是全 11×14"
    assert len(ALL_CELLS) == 154


@pytest.mark.parametrize("state,event", ALL_CELLS, ids=[f"{s.value}+{e.value}" for s, e in ALL_CELLS])
def test_every_cell_matches_schema_matrix(state, event):
    spec = _expect(state, event)
    cell = cell_of(state, event)

    if spec == "-":
        assert cell is ILLEGAL, f"SCHEMA 说 {state}+{event} 是 —，实现却给了 {cell}"
        out = apply_event(state, event)
        assert not out.applied and out.illegal and out.alert == "warn"
        assert out.state is state, "ILLEGAL 处置=丢弃+告警，主状态不得变化"
        return

    assert cell is not None, f"SCHEMA 说 {state}+{event} 合法，实现却是 —"

    if spec.startswith("T:"):
        assert not cell.is_self and cell.target.value == spec[2:]
    elif spec == "S":
        assert cell.is_self
    elif spec == "S:backfill":
        assert cell.is_self and EFFECT_BACKFILL in cell.effects
    elif spec == "R":
        assert cell.is_self and EFFECT_ROLLBACK in cell.effects
    else:  # pragma: no cover
        raise AssertionError(f"期望表语法未识别: {spec}")


def test_illegal_disposition_never_raises():
    """154 格全跑一遍，任何格子都不得抛异常（不阻断事件流）。"""
    for state, event in ALL_CELLS:
        apply_event(state, event, history=[OrderState.CREATED, OrderState.DELIVERED])


def test_legal_cell_count_matches_transcription():
    """合法格总数 = 抄录表条数 = 逐行数 SCHEMA 矩阵每行非 `—` 格之和（3+5+6+8+5+5+5+7+2+4+4）。
    这个硬数字防的是"实现与抄录同时漏掉同一格"。"""
    implemented = sum(1 for s, e in ALL_CELLS if cell_of(s, e) is not None)
    expected = sum(1 for s, e in ALL_CELLS if _expect(s, e) != "-")
    assert implemented == expected == 54, f"合法格数漂移: 实现 {implemented} / 抄录 {expected}"


# ---------------------------------------------------------------------------
# ② 三条可达性断言
# ---------------------------------------------------------------------------

def _transition_edges() -> set[tuple[OrderState, OrderState]]:
    edges: set[tuple[OrderState, OrderState]] = set()
    for s in OrderState:
        for e in Event:
            c = cell_of(s, e)
            if c is None:
                continue
            if c.target is not None:
                edges.add((s, c.target))
            elif EFFECT_ROLLBACK in c.effects:
                # 回退边目标由 history 重算：任何曾达到的状态都可能是目标，按全连通计入
                edges.update((s, t) for t in OrderState if t is not OrderState.CREATED)
    # 行聚合边（REFUNDED 的唯一入边来源，脚注⁹）
    for targets in AGGREGATION_EDGES.values():
        for src in (OrderState.REFUND_REQUESTED, OrderState.SHIPPED, OrderState.DELIVERED):
            edges.update((src, t) for t in targets)
    return edges


def test_every_non_initial_state_has_in_edge():
    edges = _transition_edges()
    incoming = {t for _, t in edges}
    missing = {s for s in OrderState if s is not OrderState.CREATED} - incoming
    assert not missing, f"这些状态没有任何入边（不可达死角）: {sorted(x.value for x in missing)}"


def test_every_non_terminal_state_has_out_edge():
    edges = _transition_edges()
    outgoing = {s for s, _ in edges}
    missing = {s for s in OrderState if s is not TERMINAL_STATE} - outgoing
    assert not missing, f"这些非终态没有出边（会永久卡住）: {sorted(x.value for x in missing)}"


def test_all_states_reachable_from_created_without_bootstrap():
    """全状态从 CREATED 可达；bootstrap 白名单**单列**，不得用来掩盖不可达状态。"""
    edges = _transition_edges()
    seen = {OrderState.CREATED}
    queue = deque([OrderState.CREATED])
    while queue:
        cur = queue.popleft()
        for s, t in edges:
            if s == cur and t not in seen:
                seen.add(t)
                queue.append(t)
    assert seen == set(OrderState), f"不可达状态: {sorted(x.value for x in set(OrderState) - seen)}"
    assert BOOTSTRAP_WHITELIST == frozenset(OrderState), "冷启动白名单=全平台可映射状态（SCHEMA §2.1 冷启动）"


def test_refunded_only_via_aggregation_not_via_event():
    """脚注⁹：`refund_completed` 事件本身不得直接迁主状态到 REFUNDED。"""
    for s in OrderState:
        c = cell_of(s, Event.REFUND_COMPLETED)
        assert c is None or c.target is not OrderState.REFUNDED, f"{s}+refund_completed 直接迁到 REFUNDED，违反脚注⁹"
    assert recompute_from_lines(OrderState.REFUND_REQUESTED, [_line(qty=2, shipped=2, refunded=2, state="refunded")]) is OrderState.REFUNDED


# ---------------------------------------------------------------------------
# ③ 两条路径级断言（SCHEMA §2.1「路径级单测样例」，必须到 CLOSED）
# ---------------------------------------------------------------------------


def _walk(events):
    """纯事件驱动走一遍（先事件），返回 (末状态, history)。"""
    state = OrderState.CREATED
    history: list[OrderState] = [state]
    for ev in events:
        out = apply_event(state, ev, history=history)
        assert not out.illegal, f"路径样例在 {state}+{ev} 处踩到 ILLEGAL: {out.reason}"
        state = out.state
        history.append(state)
    return state, history


def test_path1_partial_ship_plus_remainder_cancel_plus_refund_reaches_closed():
    """样例 1：partial_ship(2/3) + 余量取消(1) + refund_completed(已发 2 件)
    → 聚合行 1（每行已结清）→ REFUNDED；platform_close → CLOSED（R5 存量 [P0]）。

    顺序固定「先事件后聚合」（SCHEMA §2.1）：聚合只在事件链走完后执行一次。
    """
    state, _ = _walk([Event.PAID, Event.PO_SUBMITTED, Event.PARTIAL_SHIP, Event.PO_CANCELLED, Event.REFUND_COMPLETED])
    assert state is OrderState.PARTIALLY_SHIPPED, "事件链末端仍是 PARTIALLY_SHIPPED（部分退款不迁主状态）"

    lines = [_line(qty=3, shipped=2, refunded=2, cancelled=1, state="refunded")]
    state = recompute_from_lines(state, lines)
    assert state is OrderState.REFUNDED, "聚合行 1（每行已结清 + 至少一行退款>0）应判 REFUNDED"

    assert apply_event(OrderState.REFUNDED, Event.PLATFORM_CLOSE).state is OrderState.CLOSED


def test_path2_aftersale_resolved_rollback_then_close():
    """样例 2：delivered + refund_requested + refund_completed(部分退 1/3)
    + aftersale_resolved → 回退 DELIVERED；platform_close → COMPLETED → CLOSED（R6 存量同族 [P0]）。"""
    state, history = _walk(
        [
            Event.PAID,
            Event.PO_SUBMITTED,
            Event.ALL_SHIPPED,
            Event.DELIVERED,
            Event.REFUND_REQUESTED,
            Event.REFUND_COMPLETED,
        ]
    )
    assert state is OrderState.REFUND_REQUESTED

    out = apply_event(state, Event.AFTERSALE_RESOLVED, history=history)
    assert not out.illegal, out.reason
    state = out.state
    assert state is OrderState.DELIVERED, f"aftersale_resolved 应回退到 history 最高 rank（DELIVERED），实得 {state}"

    assert apply_event(state, Event.PLATFORM_CLOSE).state is OrderState.COMPLETED
    assert apply_event(OrderState.COMPLETED, Event.PLATFORM_CLOSE).state is OrderState.CLOSED


def test_partial_refund_does_not_move_main_state():
    """守卫（§2.1 v0.4）：部分退款不迁移主状态、不提前关单。"""
    lines = [_line(qty=3, shipped=2, refunded=1, state="partially_refunded")]
    assert recompute_from_lines(OrderState.DELIVERED, lines) is OrderState.DELIVERED
    assert recompute_from_lines(OrderState.REFUND_REQUESTED, lines) is OrderState.REFUND_REQUESTED


# ---------------------------------------------------------------------------
# ④ rank 规则与 qty 绝对重算
# ---------------------------------------------------------------------------

def test_rank_exemptions_are_exactly_five_classes():
    assert len(RANK_EXEMPTIONS) == 5, "SCHEMA §2.1 v0.7 豁免清单是五类，多一条少一条都是漂移"
    # 只有真正降 rank 的边才需要豁免：CLOSED(90) → REFUND_REQUESTED(65)
    assert is_rank_exempt(OrderState.CLOSED, Event.REFUND_REQUESTED, OrderState.REFUND_REQUESTED)
    assert is_rank_exempt(OrderState.CLOSED, Event.DISPUTE_OPEN, OrderState.REFUND_REQUESTED)
    # COMPLETED(60) → REFUND_REQUESTED(65) 是升 rank，本就不需要豁免
    assert not is_rank_exempt(OrderState.COMPLETED, Event.DISPUTE_OPEN, OrderState.REFUND_REQUESTED)
    assert not is_rank_exempt(OrderState.DELIVERED, Event.PLATFORM_CLOSE, OrderState.COMPLETED)


def test_no_unexempted_rank_decreasing_transition():
    """豁免清单之外不存在任何降 rank 的合法迁移。"""
    for s in OrderState:
        for e in Event:
            c = cell_of(s, e)
            if c is None or c.target is None:
                continue
            if RANK[c.target] < RANK[s]:
                assert is_rank_exempt(s, e, c.target), f"未豁免的降 rank 迁移: {s}+{e}->{c.target}"


def test_late_same_direction_event_only_backfills():
    """同向迟到：只回填不迁移，history 记 late_event。"""
    out = apply_event(OrderState.DELIVERED, Event.PAID)
    assert out.illegal  # 矩阵直接判 —
    out2 = apply_event(OrderState.REFUND_REQUESTED, Event.DELIVERED, history=[OrderState.CREATED, OrderState.DELIVERED])
    assert out2.applied and out2.state is OrderState.REFUND_REQUESTED and EFFECT_BACKFILL in out2.effects


def test_highest_reached_ignores_refund_companion_state():
    assert highest_reached([OrderState.CREATED, OrderState.PAID, OrderState.DELIVERED, OrderState.REFUND_REQUESTED]) is OrderState.DELIVERED
    assert highest_reached([OrderState.CREATED]) is OrderState.CREATED


def test_cancel_requires_ledger_closure_footnote1():
    """脚注¹：在途 PO 未裁决/台账未闭合时，取消链不得落地，且告警为 critical。"""
    out = apply_event(OrderState.PAID, Event.BUYER_CANCEL, ledger_closed=False)
    assert not out.applied and out.alert == "critical" and out.state is OrderState.PAID
    assert EFFECT_ADJUDICATE in cell_of(OrderState.PAID, Event.BUYER_CANCEL).effects


def test_aftersale_window_governs_refund_requested_footnote8():
    for s in (OrderState.COMPLETED, OrderState.CLOSED):
        assert EFFECT_WINDOW in cell_of(s, Event.REFUND_REQUESTED).effects
        out = apply_event(s, Event.REFUND_REQUESTED, within_aftersale_window=False)
        assert out.illegal and out.alert == "warn", f"{s} 窗外 refund_requested 必须丢弃+告警"
        assert apply_event(s, Event.REFUND_REQUESTED, within_aftersale_window=True).state is OrderState.REFUND_REQUESTED


def test_refund_qty_is_absolute_recompute_not_increment():
    """O2/R3 [P0]-2：qty_refunded 绝对重算，重复事件双计必须不可能。"""
    line = _line(qty=3, shipped=2, state="delivered")
    refunds = [{"qty": 1, "state": "completed"}, {"qty": 1, "state": "completed"}]
    once = recompute_line_qty(line, refunds, [])
    twice = recompute_line_qty(line, refunds + refunds, [])
    assert once["qty_refunded"] == 2, "两条 completed 各 1 件 → 绝对值 2，不是 += 的 4"
    assert twice["qty_refunded"] == 4  # 绝对重算按事实求和：喂重复事件源才会超发
    assert twice["violates_shipped_cap"] is True, "退款量超已发 → 必须标记违例（R5 O5 分流依据）"


def test_cancelled_qty_is_absolute_and_remainder_keeps_line_state():
    """R5 存量 [P0]：部分取消不改变行状态（余量取消由结清裁决）。"""
    line = _line(qty=3, shipped=2, state="partially_shipped")
    out = recompute_line_qty(line, [], [{"qty": 1, "confirmed": True}])
    assert out["qty_cancelled"] == 1 and out["state"] == "partially_shipped"
    fully = recompute_line_qty(_line(qty=2, shipped=0, state="normal"), [], [{"qty": 2, "confirmed": True}])
    assert fully["state"] == "cancelled"


def _line(qty, shipped=0, refunded=0, cancelled=0, state="normal"):
    return {"qty": qty, "qty_shipped": shipped, "qty_refunded": refunded, "qty_cancelled": cancelled, "state": state}
