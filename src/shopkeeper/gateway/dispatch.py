"""把网关 method 映射到 PlatformAdapter；发货/退款硬拒。"""

from __future__ import annotations

from typing import Any

from shopkeeper.adapters.base import PlatformAdapter
from shopkeeper.errors import AdapterError, ErrorKind
from shopkeeper.models import AdapterResult

# 网关层永不执行（工作台 HTTP 403 的同口径）
FORBIDDEN_METHODS = frozenset(
    {
        "ship",
        "refund",
        "ship_order",
        "refund_order",
        "orders_ship",
        "orders_refund",
    }
)

ALLOWED_METHODS = frozenset(
    {
        "search_products",
        "get_orders",
        "create_listing",
        "update_stock",
        "update_tracking",
    }
)

WRITE_METHODS = frozenset({"create_listing", "update_stock", "update_tracking"})


def dispatch_platform_call(
    platform: PlatformAdapter,
    method: str,
    args: dict[str, Any] | None,
) -> AdapterResult[Any]:
    name = str(method or "").strip()
    payload = dict(args or {})

    if name in FORBIDDEN_METHODS:
        return AdapterResult.failure(
            AdapterError(
                code="GATEWAY_METHOD_FORBIDDEN",
                kind=ErrorKind.POLICY,
                message="不会自动发货或退款。请在卖家中心处理。",
                retryable=False,
                human_todo=True,
                detail={"method": name, "http_status": 403},
            ),
            getattr(platform, "name", "platform"),
        )

    if name not in ALLOWED_METHODS:
        return AdapterResult.failure(
            AdapterError(
                code="GATEWAY_METHOD_UNKNOWN",
                kind=ErrorKind.VALIDATION,
                message=f"未知网关方法: {name}",
                retryable=False,
                human_todo=False,
                detail={"method": name},
            ),
            getattr(platform, "name", "platform"),
        )

    if name == "search_products":
        return platform.search_products(str(payload.get("query") or ""))
    if name == "get_orders":
        return platform.get_orders(str(payload.get("since") or ""))
    if name == "create_listing":
        body = payload.get("payload")
        if not isinstance(body, dict):
            body = payload
        return platform.create_listing(body)
    if name == "update_stock":
        body = payload.get("payload")
        if not isinstance(body, dict):
            body = payload
        return platform.update_stock(body)
    if name == "update_tracking":
        return platform.update_tracking(
            str(payload.get("order_id") or ""),
            str(payload.get("tracking_no") or ""),
        )

    return AdapterResult.failure(
        AdapterError.validation("GATEWAY_METHOD_UNKNOWN", f"未分发: {name}"),
        getattr(platform, "name", "platform"),
    )
