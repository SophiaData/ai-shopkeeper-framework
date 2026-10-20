"""金额守恒式（SCHEMA §4.6）—— 纯函数，无 IO。

## 为什么单独建这个模块

SCHEMA §4.6 把 C1–C8 写成**可执行**的守恒式，并明确校验落点：

> 「OrderService 写入时校验 + 对账任务每日全量复核（违例 → `recon_diffs(critical)`）」

但**此前没有任何代码实现它们** —— `recon_diffs` 表、`insert_recon_diff()`
都在，只有「期初 + 期间变动 = 期末」的**期间闭合**检查（`accrual.run_close_check`）
在跑。也就是说：**单笔订单内部的金额对不上，无人发现**。

实测：写入一条 `item_amount=10000, seller_discount_amount=2000,
buyer_shipping_fee=500` 但 `settlement_base_amount=99999`（按 C4 应为 8500）
的订单，**全库 `recon_diffs` 仍然是 0 行**。

这属于「文档承诺了可执行的不变量，代码没有执行」——
与另一条落点（对账任务）不同，**写入时校验能挡在污染扩散之前**，
所以先补这一半。

## 口径

- 金额单位一律 **minor（分/satang）**，与 SCHEMA 一致
- 只校验**字段齐全**时能判定等式；字段为 NULL 的订单**不报违例**
  （缺失是「不知道」，不是「不相等」——把缺失报成违例会制造噪音，
  最终没人看告警）
- `gross_profit` 等派生量不在此校验：它们由公式算出，不是独立存的两份真相
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 违例严重度。与 `recon_diffs.severity` 同域。
CRITICAL = "critical"
WARN = "warn"


@dataclass(frozen=True)
class Violation:
    """一条守恒式违例。"""

    rule: str          #: "C1" / "C4" ...
    detail: str        #: 人可读的说明（含实际数字）
    expected: int
    actual: int
    severity: str = CRITICAL

    @property
    def diff(self) -> int:
        return self.actual - self.expected

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "detail": self.detail,
            "expected": self.expected,
            "actual": self.actual,
            "diff": self.diff,
            "severity": self.severity,
        }


def _int_or_none(value: Any) -> int | None:
    """宽松取整；`None`/空串/不可解析都返回 None（= 不知道）。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def check_order_conservation(
    order: dict[str, Any],
    items: list[dict[str, Any]] | None = None,
) -> list[Violation]:
    """校验单笔订单满足 SCHEMA §4.6 的 C1 / C4。

    C1：`item_amount = Σ order_items.subtotal`
    C4：`settlement_base_amount = item_amount − seller_discount_amount + buyer_shipping_fee`

    **只在校验所需字段都存在时判定**。缺字段返回空列表（不臆造违例）。
    """
    out: list[Violation] = []

    item_amount = _int_or_none(order.get("item_amount"))
    seller_discount = _int_or_none(order.get("seller_discount_amount"))
    buyer_shipping = _int_or_none(order.get("buyer_shipping_fee"))
    settlement_base = _int_or_none(order.get("settlement_base_amount"))

    # ---- C1：商品总额 = 各明细行小计之和 ----
    if item_amount is not None and items:
        subtotals = [_int_or_none(it.get("subtotal")) for it in items if isinstance(it, dict)]
        known = [s for s in subtotals if s is not None]
        # 只有在**所有**有值的行都能读到 subtotal 时才判定（否则缺的是「不知道」）
        if known and len(known) == len(subtotals):
            total = sum(known)
            if total != item_amount:
                out.append(
                    Violation(
                        rule="C1",
                        detail=(
                            f"item_amount={item_amount} 但 Σ order_items.subtotal={total}"
                            f"（{len(known)} 行）"
                        ),
                        expected=item_amount,
                        actual=total,
                    )
                )

    # ---- C4：结算基数 = 商品额 − 卖家折扣 + 买家运费 ----
    if (
        item_amount is not None
        and seller_discount is not None
        and buyer_shipping is not None
        and settlement_base is not None
    ):
        expected = item_amount - seller_discount + buyer_shipping
        if settlement_base != expected:
            out.append(
                Violation(
                    rule="C4",
                    detail=(
                        f"settlement_base_amount={settlement_base} 但按 "
                        f"{item_amount} − {seller_discount} + {buyer_shipping} = {expected}"
                    ),
                    expected=expected,
                    actual=settlement_base,
                )
            )

    return out
