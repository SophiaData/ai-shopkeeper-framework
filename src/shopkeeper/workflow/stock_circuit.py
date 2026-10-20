"""库存断货熔断（HANDOFF_PLAN P2-04）。

## 缺口

`stock.py` 的 `push_stock` 在 `desired_qty=0` 时**安静地推 0** —— 没有告警，
也不生成下架审批单。也就是说：**供应商断货了，卖家不会知道**，商品还挂在平台上
按原价卖，直到有人下单才发现发不出货。

## 边界（重要，不要越界）

本模块**只提醒 + 生成审批单**：

- **不自动下架** —— 下架是涉钱动作，必须过 `approvals` 八态闸门（AGENTS 规则 5）
- **不调用平台写接口** —— 审批单还要人工点「通过」才轮到执行
- **不自动改库存** —— 那是 `push_stock` 的职责，且要过写开关

换句话说：熔断在这里的含义是「**把风险摆到人面前**」，不是「替人做决定」。
"""

from __future__ import annotations

from typing import Any

from shopkeeper.workflow.alerts import CODE_STOCK_OUT_OF_STOCK
from shopkeeper.workflow.store import WorkflowStore

#: 下架建议的审批动作类型。与 `create_listing` 并列，走同一个八态闸门。
DELIST_ACTION = "delist_listing"

#: 「快没了」阈值：可用量**小于它**就先进 `low` 提醒（不判断货）。
#:
#: ⚠️ 必须 **> 1**。原先写 1 时，`qty <= 0` 已经吃掉 `out`，
#: `qty < 1` 就只剩 `qty == 0` —— 而它在上一条已经返回 `out` 了，
#: 于是 **`low` 对任何整数输入都不可达**，DESIGN v1.73 承诺的「三态判定」
#: 实际只有两态。测试全绿是因为只从 `None` / 非数字输入走到 `low`。
#: 取 3 = 剩 1~2 件时提醒补货，仍在「能卖」区间，语义与文档一致。
LOW_STOCK_THRESHOLD = 3

STATE_OK = "ok"
STATE_LOW = "low"
STATE_OUT = "out"


def evaluate_stock_state(desired_qty: int) -> str:
    """按可用量给出熔断状态。

    - `out`：0 或更少 —— 供应商已无货
    - `low`：低于阈值 —— 快没了，先提醒
    - `ok`：正常
    """
    try:
        qty = int(desired_qty)
    except (TypeError, ValueError):
        return STATE_LOW  # 拿不准时按「需要看一眼」处理，不假装正常
    if qty <= 0:
        return STATE_OUT
    if qty < LOW_STOCK_THRESHOLD:
        return STATE_LOW
    return STATE_OK


def should_alert(state: str) -> bool:
    """只有 `out` 才告警。`low` 进总览即可，避免频繁打扰。"""
    return state == STATE_OUT


def build_delist_payload(
    *,
    product_id: str,
    title: str = "",
    desired_qty: int = 0,
    reason: str = "",
    shop_id: str | None = None,
) -> dict[str, Any]:
    """构造下架审批单的载荷。纯函数，便于断言内容。"""
    return {
        "action_type": DELIST_ACTION,
        "entity_type": "product",
        "entity_id": str(product_id),
        "shop_id": shop_id or "",
        "payload": {
            "product_id": str(product_id),
            "title": str(title or ""),
            "desired_qty": int(desired_qty),
            "reason": str(reason or "供应商断货，建议下架以免超卖。"),
            # 明确写清这份审批单**不会自动执行**任何平台动作。
            "requires_human": True,
            "note": "审批通过后仍需写开关放行；本单不自动下架、不自动改库存。",
        },
    }


def propose_delist(
    store: WorkflowStore,
    *,
    product_id: str,
    title: str = "",
    desired_qty: int = 0,
    reason: str = "",
    shop_id: str | None = None,
    decide_before: str = "",
    payload_hash: str = "",
) -> dict[str, Any] | None:
    """为断货商品生成下架审批单。**不执行下架**。

    幂等：同一商品已有 pending 的下架单时**不再重复建单** —— 否则每次同步都堆一张，
    审批队列会被同一个商品刷满。
    """
    state = evaluate_stock_state(desired_qty)
    if state != STATE_OUT:
        return None

    # **先告警，再判幂等**。告警与建单是两件事：
    #   - 告警受 `alerts` 自身的节流控制，每次断货都该尝试发一次
    #   - 建单受幂等约束，避免审批队列被同一商品刷满
    # 若把告警放在幂等早退之后，第二次断货会被**静默吞掉** ——
    # 而「持续断货没人再提」恰恰是最危险的形态。实测踩到并修正。
    from shopkeeper.workflow.alerts import emit_alert

    emit_alert(CODE_STOCK_OUT_OF_STOCK, job_type="stock_circuit", order_id=str(product_id))

    existing = store.get_approval_for_entity("product", str(product_id), action_type=DELIST_ACTION)
    if existing is not None and str(existing.get("status")) == "pending":
        return existing

    built = build_delist_payload(
        product_id=product_id,
        title=title,
        desired_qty=desired_qty,
        reason=reason,
        shop_id=shop_id,
    )
    approve_hash = payload_hash or _stable_hash(built["payload"])
    return store.add_approval(
        {
            **built,
            "payload_hash": approve_hash,
            "decide_before": decide_before or _default_deadline(),
            "status": "pending",
            "channel": "console",
        }
    )


def _stable_hash(payload: dict[str, Any]) -> str:
    """稳定哈希：同一载荷必得同一 hash（键序无关）。"""
    import hashlib
    import json

    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _default_deadline() -> str:
    """默认 24 小时内定夺 —— 断货单不该无限期挂着。"""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
