"""只读发货 SLA 提醒（DESIGN §9 M2 / SCHEMA orders.ship_deadline）。

在已同步的订单上算临期/超时，供工作台本地展示。不做自动履约，不发飞书，
不改平台订单态。发货/退款仍由路由层 403。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

SlaLevel = Literal["ok", "due_soon", "overdue", "n/a"]

# TikTok 跨境常见发货窗：支付后 48h；临期窗口默认截止前 6h。
DEFAULT_SHIP_WINDOW = timedelta(hours=48)
DEFAULT_DUE_SOON = timedelta(hours=6)

# 仍需卖家在卖家中心发货的状态（大小写不敏感；平台原文也可能混用）。
_NEED_SHIP_STATUSES = frozenset(
    {
        "awaiting_shipment",
        "awaitingshipment",
        "paid",
        "fulfilling",
        "to_ship",
        "toship",
        "unshipped",
        "ready_to_ship",
        "readytoship",
        "processing",
    }
)

_DONE_STATUSES = frozenset(
    {
        "shipped",
        "partially_shipped",
        "partiallyshipped",
        "delivered",
        "completed",
        "cancelled",
        "canceled",
        "refunded",
        "closed",
    }
)


def parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        # 秒或毫秒时间戳
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return parse_dt(int(text))
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _norm_status(status: Any) -> str:
    return str(status or "").strip().lower().replace(" ", "_").replace("-", "_")


def needs_shipment(status: Any) -> bool:
    key = _norm_status(status)
    if not key:
        return False
    if key in _DONE_STATUSES:
        return False
    if key in _NEED_SHIP_STATUSES:
        return True
    # 未知但含 ship/fulfill 且非已发：保守视为待发，便于 mock 夹具。
    if "ship" in key and "shipped" not in key:
        return True
    return key in {"paid", "fulfilling"}


def resolve_ship_deadline(
    order: dict[str, Any],
    *,
    ship_window: timedelta = DEFAULT_SHIP_WINDOW,
) -> datetime | None:
    for key in ("ship_deadline", "sla_deadline", "ship_by", "fulfillment_sla_time"):
        deadline = parse_dt(order.get(key))
        if deadline is not None:
            return deadline
    paid = parse_dt(order.get("paid_at") or order.get("pay_time") or order.get("payment_time"))
    created = parse_dt(order.get("created_at") or order.get("create_time") or order.get("order_create_time"))
    anchor = paid or created
    if anchor is None:
        return None
    return anchor + ship_window


def classify_order_sla(
    order: dict[str, Any],
    *,
    now: datetime | None = None,
    ship_window: timedelta = DEFAULT_SHIP_WINDOW,
    due_soon: timedelta = DEFAULT_DUE_SOON,
) -> dict[str, Any]:
    """对单笔同步订单给出 SLA 级别。纯函数，可注入 now。"""
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    order_id = str(order.get("order_id") or order.get("id") or "")
    status = order.get("status")
    base = {
        "order_id": order_id,
        "status": status,
        "level": "n/a",
        "ship_deadline": None,
        "hours_left": None,
        "message": "",
        "needs_shipment": False,
    }
    if not needs_shipment(status):
        base["message"] = "无需发货提醒（已发/取消/完结，或状态不在待发范围）。"
        return base

    deadline = resolve_ship_deadline(order, ship_window=ship_window)
    base["needs_shipment"] = True
    if deadline is None:
        base["level"] = "n/a"
        base["message"] = "待发但缺少 ship_deadline / paid_at，无法算 SLA。"
        return base

    base["ship_deadline"] = deadline.isoformat()
    delta = deadline - now_utc
    hours_left = round(delta.total_seconds() / 3600.0, 2)
    base["hours_left"] = hours_left

    if delta <= timedelta(0):
        base["level"] = "overdue"
        overdue_h = abs(hours_left)
        base["message"] = f"发货已超时 {overdue_h:g} 小时。请在卖家中心打单；本系统不会自动发货。"
    elif delta <= due_soon:
        base["level"] = "due_soon"
        base["message"] = f"发货临期，还剩 {hours_left:g} 小时。请尽快在卖家中心处理。"
    else:
        base["level"] = "ok"
        base["message"] = f"发货窗口内，还剩 {hours_left:g} 小时。"
    return base


def evaluate_orders_sla(
    orders: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    ship_window: timedelta = DEFAULT_SHIP_WINDOW,
    due_soon: timedelta = DEFAULT_DUE_SOON,
) -> dict[str, Any]:
    """评估一批同步订单；提醒列表只含 due_soon / overdue。"""
    rows = [
        classify_order_sla(order, now=now, ship_window=ship_window, due_soon=due_soon)
        for order in orders
    ]
    reminders = [row for row in rows if row["level"] in {"due_soon", "overdue"}]
    reminders.sort(key=lambda r: (0 if r["level"] == "overdue" else 1, r.get("ship_deadline") or ""))
    return {
        "evaluated_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "orders": rows,
        "reminders": reminders,
        "overdue": sum(1 for r in rows if r["level"] == "overdue"),
        "due_soon": sum(1 for r in rows if r["level"] == "due_soon"),
        "ok": sum(1 for r in rows if r["level"] == "ok"),
        "note": "本地 SLA 提醒。飞书后置。不做自动履约；发货/退款仍须卖家中心。",
    }
