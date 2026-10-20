"""一次缺数据不得**永久污染**整个桶（独立复核发现，已复现）。

## 缺陷

`_add_to_bucket` 里：

```python
for key in AMOUNT_KEYS:
    cur = bucket[key]
    val = line[key]
    if val == MISSING:
        bucket[key] = MISSING
        continue
    if cur == MISSING:
        continue          # ← 从此这笔之后的所有完整订单都不再累加
    bucket[key] = int(cur) + int(val)
```

只要**任何一笔**订单在某个字段上缺数据，该字段就变成 `缺数据`，
**后面所有完整订单都不再累加**。

### 实测（修复前）

```
A(完整, 10000) + NO-REV(缺数据) + C(完整, 50000)
  orders=3  complete_orders=2
  bucket["revenue"] == "缺数据"      ← 两笔完整单合计 60000 被静默丢弃
```

`complete_orders=2` 明确写着有 2 笔完整，却没有对应的金额可看。

「不编造」是对的（不能用 0 冒充未知），**但因此丢掉已知的完整小计是另一个方向的错**：
操作者需要同时看到「已知部分的合计」和「还差什么」。

## 修法

主键位**保持 `缺数据` 语义不变**（不编造），另加平行小计
`complete_revenue` / `complete_purchase_cost` / `complete_shipping` /
`complete_gross_profit`，只累加 `data_status == "ok"` 的行。
`

## 本文件钉住

- 缺数据订单**不阻止**后续完整订单进入平行小计
- 主键位**仍然是 `缺数据`**（不能为了「有数就填」而编造）
- 全是完整订单时，平行小计 == 主键位
- `complete_orders` 与实际累加笔数一致
"""

from __future__ import annotations

from shopkeeper.pricing.config import PricingConfig
from shopkeeper.workflow.profit import (
    MISSING,
    _add_to_bucket,
    _empty_bucket,
    compute_order_profit,
)

CFG = PricingConfig()


def _line(order_id: str, amount: int | None, *, complete: bool = True) -> dict:
    """造一条利润行。`complete=False` 时去掉运费 → 该行 `data_status != ok`。"""
    order = {
        "order_id": order_id,
        "shop_id": "default",
        "paid_at": "2026-10-09T03:00:00Z",
    }
    if amount is not None:
        order["total_amount"] = amount
    if complete:
        order["shipping_cny"] = 800
    return compute_order_profit(
        order,
        dropship={"payload": {"items": [{"unit_cost_cents": 2500}]}, "result": {}},
        config=CFG,
    )


def test_incomplete_row_does_not_discard_later_complete_rows():
    """**核心**：缺数据行不得让后续完整行的金额消失。

    修复前：`complete_revenue` 不存在，主键位 `revenue == 缺数据`，
    两笔完整单合计 60000 无从查看。
    """
    bucket = _empty_bucket("test")
    for order_id, amount, complete in [
        ("A", 100, True),
        ("NO-REV", None, True),
        ("C", 500, True),
    ]:
        _add_to_bucket(bucket, _line(order_id, amount, complete=complete))

    assert bucket["complete_orders"] == 2
    assert bucket["complete_revenue"] == 60000, "两笔完整单的合计必须可见"


def test_primary_field_still_reports_missing():
    """**反向**：主键位仍必须诚实报 `缺数据`，不能为了「有数」而编造。"""
    bucket = _empty_bucket("test")
    _add_to_bucket(bucket, _line("A", 100))
    _add_to_bucket(bucket, _line("NO-REV", None))
    assert bucket["revenue"] == MISSING


def test_all_complete_subtotal_equals_primary():
    """全是完整订单时，平行小计与主键位应当一致（平行小计不能少算）。"""
    bucket = _empty_bucket("test")
    for order_id, amount in [("A", 100), ("C", 500)]:
        _add_to_bucket(bucket, _line(order_id, amount))
    assert bucket["complete_revenue"] == bucket["revenue"] == 60000
    assert bucket["complete_orders"] == bucket["orders"] == 2


def test_complete_count_matches_accumulated_rows():
    """`complete_orders` 与实际累加进平行小计的笔数必须一致。"""
    bucket = _empty_bucket("test")
    rows = [("A", 100, True), ("B", 200, False), ("C", 300, True), ("D", 400, True)]
    for order_id, amount, complete in rows:
        _add_to_bucket(bucket, _line(order_id, amount, complete=complete))
    expected = sum(1 for *_x, complete in rows if complete)
    assert bucket["complete_orders"] == expected
    assert bucket["complete_revenue"] == (100 + 300 + 400) * 100
