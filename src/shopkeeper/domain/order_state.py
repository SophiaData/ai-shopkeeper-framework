"""订单主状态机（SCHEMA §2.1「矩阵即法律」的可执行形态）。

M1 只做**纯函数 + 单测**，不建 orders 表（DESIGN §9 M1-B「不提前实现完整订单域」）。
本模块是三样东西的唯一裁判：
1. 迁移合法性（矩阵格子，`—`=ILLEGAL）
2. ILLEGAL 处置（丢弃 + 告警，**不抛错、不阻断事件流**）
3. rank 的用途（只判同向事件迟到/重复，**不判合法性**——SCHEMA §2.1 v0.4 收窄）

任何与 docs/SCHEMA.md §2.1 不一致的实现都是 bug，不是"改进"。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class OrderState(StrEnum):
    CREATED = "CREATED"
    PAID = "PAID"
    FULFILLING = "FULFILLING"
    PARTIALLY_SHIPPED = "PARTIALLY_SHIPPED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    COMPLETED = "COMPLETED"
    REFUND_REQUESTED = "REFUND_REQUESTED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


#: SCHEMA §2.1 首行：状态与 rank
RANK: dict[OrderState, int] = {
    OrderState.CREATED: 10,
    OrderState.PAID: 20,
    OrderState.FULFILLING: 30,
    OrderState.PARTIALLY_SHIPPED: 35,
    OrderState.SHIPPED: 40,
    OrderState.DELIVERED: 50,
    OrderState.COMPLETED: 60,
    OrderState.REFUND_REQUESTED: 65,  # 伴随态
    OrderState.REFUNDED: 70,
    OrderState.CANCELLED: 80,
    OrderState.CLOSED: 90,
}

INITIAL_STATE = OrderState.CREATED
#: 唯一终态。CLOSED 仍可被售后窗/争议重开（脚注⁸/矩阵 CLOSED 行），故不出现在"无出边"清单里。
TERMINAL_STATE = OrderState.CLOSED


class Event(StrEnum):
    PAID = "paid"
    BUYER_CANCEL = "buyer_cancel"
    PO_CANCELLED = "po_cancelled"
    PO_SUBMITTED = "po_submitted"
    PARTIAL_SHIP = "partial_ship"
    ALL_SHIPPED = "all_shipped"
    DELIVERED = "delivered"
    RETURN_RECEIVED = "return_received"
    REFUND_REQUESTED = "refund_requested"
    REFUND_REJECTED = "refund_rejected"
    REFUND_COMPLETED = "refund_completed"
    DISPUTE_OPEN = "dispute_open"
    AFTERSALE_RESOLVED = "aftersale_resolved"
    PLATFORM_CLOSE = "platform_close"


#: 效果标签：格子除"去哪"之外还规定"做什么"
EFFECT_BACKFILL = "backfill"  # 仅回填时间戳/字段，不迁移主状态（脚注⁴）
EFFECT_LINE = "line_level"  # 行级数量写入（脚注⁹ / 行级写入表）
EFFECT_LEDGER = "ledger"  # 台账动作
EFFECT_ROLLBACK = "rollback"  # 回退：目标从 history 重算（脚注⁵）
EFFECT_ADJUDICATE = "cancel_adjudicate"  # 取消须先裁决在途 PO + 台账闭合（脚注¹）
EFFECT_WINDOW = "aftersale_window"  # 售后窗内才合法（脚注⁸）
EFFECT_FREEZE_LINE = "freeze_line"  # 冻结该行后续发货
EFFECT_PO_ROLLBACK = "po_rollback"  # PO 回退语义


@dataclass(frozen=True)
class Cell:
    """一个矩阵格子。target=None 表示 →自身。"""

    target: OrderState | None
    effects: frozenset[str] = frozenset()

    @property
    def is_self(self) -> bool:
        return self.target is None


def to(state: OrderState, *effects: str) -> Cell:
    return Cell(state, frozenset(effects))


def self_cell(*effects: str) -> Cell:
    return Cell(None, frozenset(effects))


#: 回退格子：目标运行时由 history 重算（脚注⁵），矩阵里不写死
def rollback(*effects: str) -> Cell:
    return Cell(None, frozenset([*effects, EFFECT_ROLLBACK]))


ILLEGAL: Cell | None = None

#: 矩阵即法律：逐格照抄 SCHEMA §2.1 表格，`—` 落成 None
MATRIX: dict[OrderState, dict[Event, Cell | None]] = {
    OrderState.CREATED: {
        Event.PAID: to(OrderState.PAID),
        Event.BUYER_CANCEL: to(OrderState.CANCELLED, EFFECT_LEDGER),
        Event.PO_CANCELLED: ILLEGAL,
        Event.PO_SUBMITTED: ILLEGAL,
        Event.PARTIAL_SHIP: ILLEGAL,
        Event.ALL_SHIPPED: ILLEGAL,
        Event.DELIVERED: ILLEGAL,
        Event.RETURN_RECEIVED: ILLEGAL,
        Event.REFUND_REQUESTED: ILLEGAL,
        Event.REFUND_REJECTED: ILLEGAL,
        Event.REFUND_COMPLETED: ILLEGAL,
        Event.DISPUTE_OPEN: ILLEGAL,
        Event.AFTERSALE_RESOLVED: ILLEGAL,
        Event.PLATFORM_CLOSE: to(OrderState.CANCELLED, EFFECT_LEDGER),
    },
    OrderState.PAID: {
        Event.PAID: ILLEGAL,
        Event.BUYER_CANCEL: to(OrderState.CANCELLED, EFFECT_ADJUDICATE, EFFECT_LEDGER),
        Event.PO_CANCELLED: to(OrderState.CANCELLED, EFFECT_ADJUDICATE, EFFECT_LEDGER),
        Event.PO_SUBMITTED: to(OrderState.FULFILLING),  # 脚注⁷：仅 PAID→FULFILLING
        Event.PARTIAL_SHIP: ILLEGAL,
        Event.ALL_SHIPPED: ILLEGAL,
        Event.DELIVERED: ILLEGAL,
        Event.RETURN_RECEIVED: ILLEGAL,
        Event.REFUND_REQUESTED: to(OrderState.REFUND_REQUESTED, EFFECT_FREEZE_LINE),
        Event.REFUND_REJECTED: ILLEGAL,
        Event.REFUND_COMPLETED: ILLEGAL,
        Event.DISPUTE_OPEN: ILLEGAL,
        Event.AFTERSALE_RESOLVED: ILLEGAL,
        Event.PLATFORM_CLOSE: to(OrderState.CANCELLED, EFFECT_ADJUDICATE, EFFECT_LEDGER),
    },
    OrderState.FULFILLING: {
        Event.PAID: ILLEGAL,
        Event.BUYER_CANCEL: to(OrderState.CANCELLED, EFFECT_ADJUDICATE, EFFECT_LEDGER),
        Event.PO_CANCELLED: to(OrderState.CANCELLED, EFFECT_ADJUDICATE, EFFECT_LEDGER),
        Event.PO_SUBMITTED: self_cell(),
        Event.PARTIAL_SHIP: to(OrderState.PARTIALLY_SHIPPED, EFFECT_LINE),
        Event.ALL_SHIPPED: to(OrderState.SHIPPED, EFFECT_LINE),
        Event.DELIVERED: ILLEGAL,
        Event.RETURN_RECEIVED: ILLEGAL,
        Event.REFUND_REQUESTED: to(OrderState.REFUND_REQUESTED, EFFECT_FREEZE_LINE),
        Event.REFUND_REJECTED: ILLEGAL,
        Event.REFUND_COMPLETED: ILLEGAL,
        Event.DISPUTE_OPEN: ILLEGAL,
        Event.AFTERSALE_RESOLVED: ILLEGAL,
        Event.PLATFORM_CLOSE: ILLEGAL,
    },
    OrderState.PARTIALLY_SHIPPED: {
        Event.PAID: ILLEGAL,
        Event.BUYER_CANCEL: ILLEGAL,
        Event.PO_CANCELLED: self_cell(EFFECT_PO_ROLLBACK),  # 豁免④
        Event.PO_SUBMITTED: self_cell(),
        Event.PARTIAL_SHIP: self_cell(EFFECT_LINE),
        Event.ALL_SHIPPED: to(OrderState.SHIPPED, EFFECT_LINE),
        Event.DELIVERED: to(OrderState.DELIVERED, EFFECT_LINE),  # 脚注³
        Event.RETURN_RECEIVED: self_cell(EFFECT_LINE),
        Event.REFUND_REQUESTED: to(OrderState.REFUND_REQUESTED, EFFECT_FREEZE_LINE),
        Event.REFUND_REJECTED: ILLEGAL,
        Event.REFUND_COMPLETED: self_cell(EFFECT_LINE, EFFECT_LEDGER),
        Event.DISPUTE_OPEN: ILLEGAL,
        Event.AFTERSALE_RESOLVED: ILLEGAL,
        Event.PLATFORM_CLOSE: ILLEGAL,
    },
    OrderState.SHIPPED: {
        Event.PAID: ILLEGAL,
        Event.BUYER_CANCEL: ILLEGAL,
        Event.PO_CANCELLED: ILLEGAL,
        Event.PO_SUBMITTED: ILLEGAL,
        Event.PARTIAL_SHIP: ILLEGAL,
        Event.ALL_SHIPPED: self_cell(EFFECT_LINE),
        Event.DELIVERED: to(OrderState.DELIVERED),
        Event.RETURN_RECEIVED: self_cell(EFFECT_LINE),
        Event.REFUND_REQUESTED: to(OrderState.REFUND_REQUESTED, EFFECT_FREEZE_LINE),
        Event.REFUND_REJECTED: ILLEGAL,
        Event.REFUND_COMPLETED: self_cell(EFFECT_LINE, EFFECT_LEDGER),
        Event.DISPUTE_OPEN: ILLEGAL,
        Event.AFTERSALE_RESOLVED: ILLEGAL,
        Event.PLATFORM_CLOSE: ILLEGAL,
    },
    OrderState.DELIVERED: {
        Event.PAID: ILLEGAL,
        Event.BUYER_CANCEL: ILLEGAL,
        Event.PO_CANCELLED: ILLEGAL,
        Event.PO_SUBMITTED: ILLEGAL,
        Event.PARTIAL_SHIP: ILLEGAL,
        Event.ALL_SHIPPED: ILLEGAL,
        Event.DELIVERED: self_cell(),
        Event.RETURN_RECEIVED: self_cell(EFFECT_LINE),
        Event.REFUND_REQUESTED: to(OrderState.REFUND_REQUESTED, EFFECT_FREEZE_LINE),
        Event.REFUND_REJECTED: ILLEGAL,
        Event.REFUND_COMPLETED: self_cell(EFFECT_LINE, EFFECT_LEDGER),
        Event.DISPUTE_OPEN: ILLEGAL,
        Event.AFTERSALE_RESOLVED: ILLEGAL,
        Event.PLATFORM_CLOSE: to(OrderState.COMPLETED),
    },
    OrderState.COMPLETED: {
        Event.PAID: ILLEGAL,
        Event.BUYER_CANCEL: ILLEGAL,
        Event.PO_CANCELLED: ILLEGAL,
        Event.PO_SUBMITTED: ILLEGAL,
        Event.PARTIAL_SHIP: ILLEGAL,
        Event.ALL_SHIPPED: ILLEGAL,
        Event.DELIVERED: ILLEGAL,
        Event.RETURN_RECEIVED: self_cell(EFFECT_LINE),
        Event.REFUND_REQUESTED: to(OrderState.REFUND_REQUESTED, EFFECT_WINDOW, EFFECT_FREEZE_LINE),  # 脚注⁸
        Event.REFUND_REJECTED: ILLEGAL,
        Event.REFUND_COMPLETED: self_cell(EFFECT_LINE, EFFECT_LEDGER),
        Event.DISPUTE_OPEN: to(OrderState.REFUND_REQUESTED, EFFECT_FREEZE_LINE),
        Event.AFTERSALE_RESOLVED: ILLEGAL,
        Event.PLATFORM_CLOSE: to(OrderState.CLOSED),
    },
    OrderState.REFUND_REQUESTED: {
        Event.PAID: ILLEGAL,
        Event.BUYER_CANCEL: ILLEGAL,
        Event.PO_CANCELLED: ILLEGAL,
        Event.PO_SUBMITTED: ILLEGAL,
        Event.PARTIAL_SHIP: ILLEGAL,
        Event.ALL_SHIPPED: self_cell(EFFECT_BACKFILL),  # 脚注⁴：仅回填
        Event.DELIVERED: self_cell(EFFECT_BACKFILL),  # 脚注⁴：仅回填
        Event.RETURN_RECEIVED: self_cell(EFFECT_LINE),
        Event.REFUND_REQUESTED: self_cell(EFFECT_FREEZE_LINE),
        Event.REFUND_REJECTED: rollback(EFFECT_LINE),  # 脚注⁵：回退
        Event.REFUND_COMPLETED: self_cell(EFFECT_LINE, EFFECT_LEDGER),  # 脚注⁹：不直接迁主状态
        Event.DISPUTE_OPEN: ILLEGAL,
        Event.AFTERSALE_RESOLVED: rollback(EFFECT_LINE),  # 脚注¹⁰：部分退款了结出口
        Event.PLATFORM_CLOSE: ILLEGAL,
    },
    OrderState.REFUNDED: {
        Event.PAID: ILLEGAL,
        Event.BUYER_CANCEL: ILLEGAL,
        Event.PO_CANCELLED: ILLEGAL,
        Event.PO_SUBMITTED: ILLEGAL,
        Event.PARTIAL_SHIP: ILLEGAL,
        Event.ALL_SHIPPED: ILLEGAL,
        Event.DELIVERED: ILLEGAL,
        Event.RETURN_RECEIVED: ILLEGAL,
        Event.REFUND_REQUESTED: ILLEGAL,
        Event.REFUND_REJECTED: ILLEGAL,
        Event.REFUND_COMPLETED: self_cell(EFFECT_BACKFILL),
        Event.DISPUTE_OPEN: ILLEGAL,
        Event.AFTERSALE_RESOLVED: ILLEGAL,
        Event.PLATFORM_CLOSE: to(OrderState.CLOSED),
    },
    OrderState.CANCELLED: {
        Event.PAID: ILLEGAL,
        Event.BUYER_CANCEL: self_cell(),
        Event.PO_CANCELLED: self_cell(),
        Event.PO_SUBMITTED: ILLEGAL,
        Event.PARTIAL_SHIP: ILLEGAL,
        Event.ALL_SHIPPED: ILLEGAL,
        Event.DELIVERED: ILLEGAL,
        Event.RETURN_RECEIVED: ILLEGAL,
        Event.REFUND_REQUESTED: ILLEGAL,
        Event.REFUND_REJECTED: ILLEGAL,
        # 脚注⁶：已支付后取消，平台必然回传退款完成 → 只回填 refunded_at + 台账
        Event.REFUND_COMPLETED: self_cell(EFFECT_BACKFILL, EFFECT_LEDGER),
        Event.DISPUTE_OPEN: ILLEGAL,
        Event.AFTERSALE_RESOLVED: ILLEGAL,
        Event.PLATFORM_CLOSE: to(OrderState.CLOSED),
    },
    OrderState.CLOSED: {
        Event.PAID: ILLEGAL,
        Event.BUYER_CANCEL: ILLEGAL,
        Event.PO_CANCELLED: ILLEGAL,
        Event.PO_SUBMITTED: ILLEGAL,
        Event.PARTIAL_SHIP: ILLEGAL,
        Event.ALL_SHIPPED: ILLEGAL,
        Event.DELIVERED: ILLEGAL,
        Event.RETURN_RECEIVED: ILLEGAL,
        Event.REFUND_REQUESTED: to(OrderState.REFUND_REQUESTED, EFFECT_WINDOW, EFFECT_FREEZE_LINE),  # 豁免②
        Event.REFUND_REJECTED: ILLEGAL,
        Event.REFUND_COMPLETED: self_cell(EFFECT_LINE, EFFECT_LEDGER),
        Event.DISPUTE_OPEN: to(OrderState.REFUND_REQUESTED, EFFECT_FREEZE_LINE),  # 豁免③
        Event.AFTERSALE_RESOLVED: ILLEGAL,
        Event.PLATFORM_CLOSE: self_cell(),
    },
}

#: 行聚合边（矩阵之外的第二权威来源，SCHEMA §2.1「主状态推进的单一权威」）
AGGREGATION_EDGES: dict[str, set[OrderState]] = {
    "aggregate_row1_refunded": {OrderState.REFUNDED},  # 聚合行 1 → REFUNDED（唯一判据）
    "aggregate_row2_cancelled": {OrderState.CANCELLED},  # 聚合行 2 → CANCELLED
}

#: 冷启动白名单（SCHEMA §2.1「冷启动」：按平台当前状态设初态，history 记 source='bootstrap'）
BOOTSTRAP_WHITELIST: frozenset[OrderState] = frozenset(OrderState)

#: SCHEMA §2.1 rank 豁免五类（v0.7），逐条与矩阵列名一一对应
RANK_EXEMPTIONS: tuple[tuple[str, ...], ...] = (
    ("①", "REFUND_REQUESTED", Event.REFUND_REJECTED.value, "回退，目标从 history 重算（脚注⁵）"),
    ("②", "CLOSED", Event.REFUND_REQUESTED.value, "售后窗重开（脚注⁸）"),
    ("③", "CLOSED|COMPLETED", Event.DISPUTE_OPEN.value, "争议重开"),
    ("④", "PARTIALLY_SHIPPED", Event.PO_CANCELLED.value, "PO 回退到自身"),
    ("⑤", "REFUND_REQUESTED", Event.AFTERSALE_RESOLVED.value, "售后了结恢复前置态（脚注¹⁰）"),
)


@dataclass
class Outcome:
    """一次事件裁决结果。illegal 不抛错，只带告警（SCHEMA §2.1 ILLEGAL 处置）。"""

    state: OrderState
    applied: bool
    illegal: bool = False
    late: bool = False
    alert: str | None = None  # 'warn' | 'critical' | None
    effects: frozenset[str] = frozenset()
    reason: str = ""
    history_entry: dict = field(default_factory=dict)


def cell_of(state: OrderState, event: Event) -> Cell | None:
    return MATRIX[state][event]


def is_rank_exempt(state: OrderState, event: Event, target: OrderState | None) -> bool:
    """豁免清单之外不存在任何降 rank 的合法迁移（SCHEMA §2.1 v0.7）。"""
    if target is None:
        return False
    pairs = {
        (OrderState.REFUND_REQUESTED, Event.REFUND_REJECTED),
        (OrderState.CLOSED, Event.REFUND_REQUESTED),
        (OrderState.COMPLETED, Event.DISPUTE_OPEN),
        (OrderState.CLOSED, Event.DISPUTE_OPEN),
        (OrderState.PARTIALLY_SHIPPED, Event.PO_CANCELLED),
        (OrderState.REFUND_REQUESTED, Event.AFTERSALE_RESOLVED),
    }
    if (state, event) not in pairs:
        return False
    return RANK[target] < RANK[state] or EFFECT_ROLLBACK in (cell_of(state, event).effects if cell_of(state, event) else frozenset())


def highest_reached(history: Iterable[OrderState], *, exclude: Sequence[OrderState] = (OrderState.REFUND_REQUESTED,)) -> OrderState | None:
    """回退目标 = 进入退款态之前达到过的最高 rank（脚注⁵，权威=history，不用缓存列）。"""
    best: OrderState | None = None
    for s in history:
        if s in exclude:
            continue
        if best is None or RANK[s] > RANK[best]:
            best = s
    return best


def apply_event(
    state: OrderState,
    event: Event,
    *,
    history: Sequence[OrderState] = (),
    within_aftersale_window: bool = True,
    ledger_closed: bool = True,
) -> Outcome:
    """单格裁决。顺序固定：**先事件后聚合**（聚合由 recompute_from_lines 单独走）。

    - 矩阵 ILLEGAL → 丢弃 + warn 告警，状态不变，不抛错
    - 脚注⁸ 窗外 refund_requested → 同 ILLEGAL 处置
    - 脚注¹ 取消但台账未闭合 → 迁移不落地（保持原态 + critical 告警）
    - 同向迟到（rank 更低且非豁免）→ 只回填不迁移，late=True
    """
    cell = cell_of(state, event)
    if cell is None:
        return Outcome(state=state, applied=False, illegal=True, alert="warn", reason=f"ILLEGAL: {state}+{event} 不在矩阵合法格内，丢弃+告警")

    if EFFECT_WINDOW in cell.effects and not within_aftersale_window:
        return Outcome(
            state=state, applied=False, illegal=True, alert="warn",
            reason=f"窗外 refund_requested/dispute（脚注⁸）：{state}+{event} 丢弃+告警",
        )

    if EFFECT_ADJUDICATE in cell.effects and not ledger_closed:
        return Outcome(
            state=state, applied=False, alert="critical",
            reason=f"脚注¹：在途 PO 未裁决/台账未闭合，禁止进入取消链（{state}+{event}）",
        )

    target = state if cell.is_self else cell.target
    assert target is not None
    if EFFECT_ROLLBACK in cell.effects:
        target = highest_reached(history) or OrderState.PAID

    # rank 只判同向迟到（v0.4 收窄）：目标 rank 严格低于当前且不在豁免内 → 迟到只回填
    if RANK[target] < RANK[state] and not is_rank_exempt(state, event, target):
        return Outcome(
            state=state, applied=False, late=True, alert="warn",
            effects=cell.effects | {EFFECT_BACKFILL},
            reason=f"迟到事件：{target}({RANK[target]}) < {state}({RANK[state]})，只回填不迁移，history 记 late_event=TRUE",
        )

    return Outcome(
        state=target, applied=target is not state or EFFECT_BACKFILL in cell.effects or EFFECT_LINE in cell.effects,
        effects=cell.effects,
        reason=f"{state} --{event}--> {target if not cell.is_self else '自身'}",
        history_entry={"from": state.value, "event": event.value, "to": target.value},
    )


def recompute_from_lines(
    state: OrderState,
    lines: Sequence[dict],
    *,
    paid: bool = True,
) -> OrderState:
    """行聚合（SCHEMA §2.1 聚合表，行 1 优先于行 2；幂等可重放）。

    行 1（→REFUNDED）：所有行已结清（每行 state ∈ {refunded,cancelled}，或
      state ∈ {partially_refunded,refunded} 且 qty_refunded=qty_shipped 且
      qty_shipped+qty_cancelled=qty），且至少一行 qty_refunded>0，且当前 rank ≤ 70。
    行 2（→CANCELLED）：所有行 state='cancelled' 且已支付，且当前 rank ≤ 80。
    其他：维持事件驱动结果。
    """
    if not lines:
        return state

    def settled(l: dict) -> bool:
        if l["state"] in ("refunded", "cancelled"):
            return True
        return (
            l["state"] in ("partially_refunded", "refunded")
            and l["qty_refunded"] == l["qty_shipped"]
            and l["qty_shipped"] + l["qty_cancelled"] == l["qty"]
        )

    all_settled = all(settled(l) for l in lines)
    any_refunded = any(l["qty_refunded"] > 0 for l in lines)
    if all_settled and any_refunded and RANK[state] <= RANK[OrderState.REFUNDED]:
        return OrderState.REFUNDED  # 行 1 优先，且这是 REFUNDED 的唯一判据（脚注⁹）

    all_cancelled = all(l["state"] == "cancelled" for l in lines)
    if all_cancelled and paid and RANK[state] <= RANK[OrderState.CANCELLED]:
        return OrderState.CANCELLED

    return state


def recover_lines_after_aftersale_resolution(line: dict) -> dict:
    """售后了结后，把行从 `refund_requested` / `refunding` 解冻。

    补 R8 终审 **L4** 登记项：`aftersale_resolved` 只回退**主状态**（脚注¹⁰），
    不写行级；于是残留的 `refund_requested`/`refunding` 行**没有回收规则**，
    而 §2.1 行级写入表明写"`refund_requested` 行冻结该行后续发货"
    ——不回收就是永久冻结，商品明明还能发。

    回收口径复用 `refund_rejected` 的既有判据（§2.1 行级写入表）：
    `qty_shipped > 0 → delivered`，否则 `normal`。
    **数量计数一律不动**：部分退款是既成事实，解冻只是恢复发货能力（脚注¹⁰）。
    """
    state = line.get("state")
    if state not in ("refund_requested", "refunding"):
        return line
    recovered = dict(line)
    recovered["state"] = "delivered" if line.get("qty_shipped", 0) > 0 else "normal"
    recovered["recovered_from"] = state
    recovered["recovered_by"] = "aftersale_resolved"
    return recovered


def recompute_line_qty(line: dict, refunds: Sequence[dict], cancels: Sequence[dict]) -> dict:
    """行级数量**绝对重算**（R3 [P0]-2 / O2：禁用增量 +=，防跨 source 重复事件双计）。"""
    qty_refunded = sum(r["qty"] for r in refunds if r["state"] == "completed")
    qty_cancelled = sum(c["qty"] for c in cancels if c["confirmed"])
    qty = line["qty"]
    qty_shipped = line.get("qty_shipped", 0)

    if qty_refunded >= qty:
        state = "refunded"
    elif qty_refunded > 0:
        state = "partially_refunded"
    elif qty_shipped == 0 and qty_cancelled == qty:
        state = "cancelled"
    elif qty_shipped > 0:
        state = line.get("state") if line.get("state") in ("partially_shipped", "shipped", "delivered") else "partially_shipped"
    else:
        state = line.get("state", "normal")

    return {
        **line,
        "qty_refunded": qty_refunded,
        "qty_cancelled": qty_cancelled,
        "qty_shipped": qty_shipped,
        "state": state,
        # R5 O5：本地账错（退款量 > 已发量）阻断入库；平台事实超发则入库 + critical
        "violates_shipped_cap": qty_refunded > qty_shipped,
    }
