"""订单是否可进入履约/代发的唯一判定。

只接受已支付且尚未发货的状态；未知状态一律 fail-closed。
"""

from __future__ import annotations

from typing import Any

FULFILLABLE_STATUSES = frozenset({
    "PAID",
    "AWAITING_SHIPMENT",
    "READY_TO_SHIP",
    "AWAITING_COLLECTION",
    "PROCESSING",
})


def order_status_key(order: dict[str, Any] | None) -> str:
    raw = str((order or {}).get("status") or "").strip().upper()
    return raw.replace(" ", "_").replace("-", "_")


def order_is_fulfillable(order: dict[str, Any] | None) -> bool:
    return order_status_key(order) in FULFILLABLE_STATUSES
