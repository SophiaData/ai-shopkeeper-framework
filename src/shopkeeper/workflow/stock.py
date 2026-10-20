"""库存同步缓冲推送（DESIGN §5 stock-push / §6 缓冲系数）。

工作台薄切片：本地 `stock_sync_state` 存 desired_qty / buffer_factor；
推送量 = floor(desired_qty × buffer_factor)。未审批 listing 不得 push。
完整 SCHEMA `stock_sync_state`（listing_item_id / circuit_state）后置。
"""

from __future__ import annotations

import math
from typing import Any

from shopkeeper.adapters.base import PlatformAdapter
from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult
from shopkeeper.side_effect import make_idempotency_key, run_side_effect
from shopkeeper.workflow.drafts import DraftError
from shopkeeper.workflow.store import WorkflowStore

DEFAULT_BUFFER_FACTOR = 0.8
STOCK_PUSH_ACTION = "stock_push"
# 缓冲系数内免审；越界须审批（DESIGN §6）。本切片拒绝越界而非另开审批单。
BUFFER_FACTOR_MIN = 0.0
BUFFER_FACTOR_MAX = 1.0


def compute_buffered_qty(
    desired_qty: int,
    buffer_factor: float,
    *,
    daily_cap: int | None = None,
) -> int:
    """平台推送量 = min(floor(desired_qty × buffer_factor), daily_cap)。"""
    if desired_qty < 0:
        raise DraftError("desired_qty 不能为负。")
    if buffer_factor <= BUFFER_FACTOR_MIN or buffer_factor > BUFFER_FACTOR_MAX:
        raise DraftError(
            f"buffer_factor 须在 ({BUFFER_FACTOR_MIN}, {BUFFER_FACTOR_MAX}] 内（缓冲系数内免审；越界须审批）。"
        )
    qty = math.floor(desired_qty * buffer_factor)
    if daily_cap is not None:
        if daily_cap < 0:
            raise DraftError("daily_cap 不能为负。")
        qty = min(qty, daily_cap)
    return max(0, int(qty))


def push_stock(
    store: WorkflowStore,
    platform: PlatformAdapter,
    listing_id: str,
    *,
    desired_qty: int,
    buffer_factor: float | None = None,
    daily_cap: int | None = None,
) -> dict[str, Any]:
    """审批闸门通过后按缓冲系数推库存。发货/退款不在此路径。"""
    draft = store.get_draft(listing_id)
    if draft is None:
        raise DraftError("草稿不存在。")
    if draft.get("kind") != "listing":
        raise DraftError("只有 listing 可以推送库存。")
    status = str(draft.get("status") or "")
    if status in {"pending", "needs_revision"}:
        raise DraftError("未审批，不能推送库存。须先人工通过 listing。")
    if status == "rejected":
        raise DraftError("已拒绝的 listing 不能推送库存。")
    if status == "expired":
        raise DraftError("审批已过期，不能推送库存。")
    if status not in {"approved", "published", "dry_run", "publishing"}:
        raise DraftError(f"当前状态 {status} 不能推送库存。")

    from shopkeeper.workflow.shops import resolve_platform

    platform = resolve_platform(platform, draft.get("shop_id"))
    existing = store.get_stock_sync_state(listing_id)
    factor = (
        float(buffer_factor)
        if buffer_factor is not None
        else float(existing["buffer_factor"] if existing else DEFAULT_BUFFER_FACTOR)
    )
    try:
        pushed_qty = compute_buffered_qty(int(desired_qty), factor, daily_cap=daily_cap)
    except DraftError:
        store.upsert_stock_sync_state(
            listing_id,
            desired_qty=int(desired_qty),
            buffer_factor=factor,
            last_error="buffer_factor 或 desired_qty 非法",
            shop_id=draft.get("shop_id"),
        )
        raise

    payload = {
        "listing_id": listing_id,
        "platform_product_id": draft.get("platform_product_id"),
        "quantity": pushed_qty,
        "desired_qty": int(desired_qty),
        "buffer_factor": factor,
    }
    adapter_name = getattr(platform, "name", "platform")
    # 业务代次：A→B→A 的第三次不是第一次请求的 retry，不能回放旧成功。
    # last_pushed_at 在每次成功推送后推进；同一请求重试则保持不变，仍可幂等回放。
    revision = str((existing or {}).get("last_pushed_at") or "initial")
    idem_key = make_idempotency_key(
        adapter_name,
        "update_stock",
        listing_id,
        str(pushed_qty),
        str(int(desired_qty)),
        str(factor),
        revision,
    )

    def _push() -> AdapterResult[dict[str, Any]]:
        return platform.update_stock(payload)

    outcome = run_side_effect(
        store,
        adapter=adapter_name,
        operation="update_stock",
        idempotency_key=idem_key,
        request=payload,
        fn=_push,
        draft_id=listing_id,
        action=STOCK_PUSH_ACTION,
    )
    result = outcome.result
    if result is None or not result.ok or result.data is None:
        err = (
            result.error
            if result is not None and result.error is not None
            else AdapterError.policy("TTS_WRITE_BLOCKED", "库存推送未返回结果")
        )
        detail = f"{err.code} {err.message}"
        state = store.upsert_stock_sync_state(
            listing_id,
            desired_qty=int(desired_qty),
            buffer_factor=factor,
            last_error=detail,
            shop_id=draft.get("shop_id"),
        )
        raise DraftError(f"库存推送未执行：{detail}")

    data = dict(result.data)
    dry_run = bool(data.get("dry_run"))
    state = store.upsert_stock_sync_state(
        listing_id,
        desired_qty=int(desired_qty),
        buffer_factor=factor,
        last_error="",
        mark_pushed=True,
        shop_id=draft.get("shop_id"),
    )
    return {
        "listing_id": listing_id,
        "desired_qty": int(desired_qty),
        "buffer_factor": factor,
        "pushed_qty": pushed_qty,
        "platform_product_id": draft.get("platform_product_id"),
        "dry_run": dry_run,
        "idempotent_replay": bool(outcome.replayed),
        "stock_sync_state": state,
        "adapter": data,
        "note": (
            "已按缓冲系数推送库存。"
            if not dry_run
            else "干跑：未外呼平台库存 API。"
        ),
    }
