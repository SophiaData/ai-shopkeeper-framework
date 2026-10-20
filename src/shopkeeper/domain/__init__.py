"""领域层：纯函数状态机与业务规则（不依赖 DB、不依赖适配器）。

DESIGN §2.1 原则 2「Agent 无状态」的另一面：规则必须是纯函数，
才能独立单测、才能在编排器与真实实现之间复用。
"""

from shopkeeper.domain.order_state import (
    AGGREGATION_EDGES,
    BOOTSTRAP_WHITELIST,
    EFFECT_ADJUDICATE,
    EFFECT_BACKFILL,
    EFFECT_LINE,
    RANK,
    RANK_EXEMPTIONS,
    TERMINAL_STATE,
    Event,
    OrderState,
    Outcome,
    apply_event,
    cell_of,
    highest_reached,
    is_rank_exempt,
    recover_lines_after_aftersale_resolution,
    recompute_from_lines,
    recompute_line_qty,
)

__all__ = [
    "OrderState",
    "Event",
    "RANK",
    "RANK_EXEMPTIONS",
    "AGGREGATION_EDGES",
    "BOOTSTRAP_WHITELIST",
    "TERMINAL_STATE",
    "Outcome",
    "apply_event",
    "cell_of",
    "highest_reached",
    "is_rank_exempt",
    "recompute_from_lines",
    "recompute_line_qty",
]
