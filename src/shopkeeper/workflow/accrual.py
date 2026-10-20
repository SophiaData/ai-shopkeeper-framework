"""真空期订单台账 bootstrap + R3 两段闭合检查（工作台 SQLite 薄切片）。

SCHEMA §4.7：按平台快照生成 basis='accrual'、ref_type='bootstrap' 补记分录。
SCHEMA §4.3 R3：期初 + 期间变动 = 期末；差异进 recon_diffs(ledger_vs_settlement)，
只读列表，不自动改订单、不外呼平台写。
"""

from __future__ import annotations

from typing import Any

from shopkeeper.workflow.store import WorkflowStore

DEFAULT_SHOP_ID = "local"
DEFAULT_PERIOD_KEY = "vacuum"
DEFAULT_COMMISSION_BPS = 500  # 5% 预估佣金（mock；真实费率后置）
RECON_LINE = "ledger_vs_settlement"

# 平台已结清/取消：不再进真空期未结台账
_SETTLED_STATUSES = frozenset(
    {
        "cancelled",
        "canceled",
        "refunded",
        "closed",
    }
)


def is_open_order(order: dict[str, Any]) -> bool:
    status = str(order.get("status") or "").strip().lower().replace(" ", "_").replace("-", "_")
    return status not in _SETTLED_STATUSES and bool(
        str(order.get("order_id") or order.get("id") or "").strip()
    )


def order_amount_cents(order: dict[str, Any]) -> int:
    """结算基数（分）。优先显式分字段；否则把 total_amount 当主币单位 ×100。"""
    for key in ("settlement_base_amount", "amount_cents", "item_amount"):
        raw = order.get(key)
        if raw is None or raw == "":
            continue
        return int(raw)

    raw = order.get("total_amount")
    if raw is None:
        raw = order.get("amount")
    if raw is None or raw == "":
        return 0
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw * 100
    if isinstance(raw, float):
        return int(round(raw * 100))
    text = str(raw).strip().replace(",", "")
    if not text:
        return 0
    if "." in text:
        return int(round(float(text) * 100))
    return int(text) * 100


def order_currency(order: dict[str, Any]) -> str:
    return str(order.get("currency") or "THB").upper()


def estimate_commission_cents(sale_cents: int, *, commission_bps: int = DEFAULT_COMMISSION_BPS) -> int:
    """平台佣金预估（负支出）。"""
    if sale_cents <= 0 or commission_bps <= 0:
        return 0
    return -int(sale_cents * commission_bps // 10_000)


def net_accrual_for_order(
    order: dict[str, Any],
    *,
    commission_bps: int = DEFAULT_COMMISSION_BPS,
) -> int:
    sale = order_amount_cents(order)
    return sale + estimate_commission_cents(sale, commission_bps=commission_bps)


def _entry_key(entry_type: str, order_id: str) -> str:
    return f"{entry_type}:order={order_id}:bootstrap"


def bootstrap_accrual(
    store: WorkflowStore,
    *,
    shop_id: str = DEFAULT_SHOP_ID,
    period_key: str = DEFAULT_PERIOD_KEY,
    commission_bps: int = DEFAULT_COMMISSION_BPS,
    with_close_check: bool = True,
) -> dict[str, Any]:
    """首次/手动：把已同步的未结订单写入 accrual 台账快照（幂等）。"""
    orders = [o for o in store.list_synced_orders(platform="tiktok") if is_open_order(o)]
    created = 0
    skipped = 0
    order_ids: list[str] = []

    for order in orders:
        order_id = str(order.get("order_id") or order.get("id"))
        sale = order_amount_cents(order)
        currency = order_currency(order)
        commission = estimate_commission_cents(sale, commission_bps=commission_bps)

        for entry_type, amount in (
            ("sale", sale),
            ("platform_commission", commission),
        ):
            if amount == 0 and entry_type == "platform_commission":
                continue
            key = _entry_key(entry_type, order_id)
            before = store.get_ledger_entry_by_key(key)
            store.insert_ledger_entry(
                {
                    "shop_id": shop_id,
                    "entry_key": key,
                    "order_id": order_id,
                    "entry_type": entry_type,
                    "currency": currency,
                    "amount": amount,
                    "base_currency": "CNY",
                    "base_amount": amount,  # M2 薄切片：暂 1:1，两跳 fx 后置
                    "basis": "accrual",
                    "ref_type": "bootstrap",
                    "ref_id": order_id,
                    "memo": "vacuum-period bootstrap accrual",
                }
            )
            after = store.get_ledger_entry_by_key(key)
            if before is None and after is not None:
                created += 1
            else:
                skipped += 1
        order_ids.append(order_id)

    accruals = store.list_ledger_entries(shop_id=shop_id, basis="accrual")
    total = sum(int(e["amount"]) for e in accruals)
    store.upsert_accrual_snapshot(
        {
            "shop_id": shop_id,
            "kind": "opening",
            "period_key": period_key,
            "total_amount": total,
            "entry_count": len(accruals),
            "payload": {
                "order_ids": order_ids,
                "commission_bps": commission_bps,
                "source": "bootstrap",
            },
        }
    )

    status = accrual_status(
        store,
        shop_id=shop_id,
        period_key=period_key,
        commission_bps=commission_bps,
        with_close_check=with_close_check,
    )
    status["bootstrap"] = {
        "orders_considered": len(orders),
        "entries_created": created,
        "entries_skipped_idempotent": skipped,
        "order_ids": order_ids,
    }
    status["note"] = (
        "真空期台账 bootstrap：只写本地 accrual 快照与 recon 差异列表；"
        "不改订单、不发货、不退款、不外呼平台写。"
    )
    return status


def period_delta_cents(store: WorkflowStore, *, shop_id: str = DEFAULT_SHOP_ID) -> int:
    """期间变动 = settlement + correction 分录合计（不含 accrual）。"""
    total = 0
    for basis in ("settlement", "correction"):
        for entry in store.list_ledger_entries(shop_id=shop_id, basis=basis):
            total += int(entry["amount"])
    return total


def closing_from_platform(
    store: WorkflowStore,
    *,
    commission_bps: int = DEFAULT_COMMISSION_BPS,
) -> tuple[int, list[str]]:
    """期末：按当前已同步未结订单重算净额（平台侧真相，只读）。"""
    order_ids: list[str] = []
    total = 0
    for order in store.list_synced_orders(platform="tiktok"):
        if not is_open_order(order):
            continue
        order_ids.append(str(order.get("order_id") or order.get("id")))
        total += net_accrual_for_order(order, commission_bps=commission_bps)
    return total, order_ids


def run_close_check(
    store: WorkflowStore,
    *,
    shop_id: str = DEFAULT_SHOP_ID,
    period_key: str = DEFAULT_PERIOD_KEY,
    commission_bps: int = DEFAULT_COMMISSION_BPS,
) -> dict[str, Any]:
    """R3 两段闭合：期初 + 期间变动 = 期末。差异只进 recon_diffs，不改订单。"""
    opening_snap = store.get_accrual_snapshot(
        shop_id=shop_id, kind="opening", period_key=period_key
    )
    opening = int(opening_snap["total_amount"]) if opening_snap else 0
    delta = period_delta_cents(store, shop_id=shop_id)
    closing, closing_order_ids = closing_from_platform(store, commission_bps=commission_bps)

    store.upsert_accrual_snapshot(
        {
            "shop_id": shop_id,
            "kind": "closing",
            "period_key": period_key,
            "total_amount": closing,
            "entry_count": len(closing_order_ids),
            "payload": {"order_ids": closing_order_ids, "source": "platform_resync_readonly"},
        }
    )

    expected = opening + delta
    balanced = expected == closing
    diff_amount = closing - expected

    store.clear_open_recon_diffs(
        shop_id=shop_id,
        recon_line=RECON_LINE,
        ref_type="accrual_close",
    )
    diffs: list[dict[str, Any]] = []
    if not balanced:
        diff = store.insert_recon_diff(
            {
                "shop_id": shop_id,
                "recon_line": RECON_LINE,
                "ref_type": "accrual_close",
                "ref_id": period_key,
                "expected": expected,
                "actual": closing,
                "diff_amount": diff_amount,
                "severity": "critical" if abs(diff_amount) >= 100 else "warn",
                "state": "open",
                "resolution": None,
            }
        )
        diffs.append(diff)

    return {
        "balanced": balanced,
        "opening": opening,
        "period_delta": delta,
        "closing": closing,
        "expected_closing": expected,
        "diff_amount": diff_amount,
        "recon_diffs": diffs,
        "equation": "opening + period_delta = closing",
    }


def accrual_status(
    store: WorkflowStore,
    *,
    shop_id: str = DEFAULT_SHOP_ID,
    period_key: str = DEFAULT_PERIOD_KEY,
    commission_bps: int = DEFAULT_COMMISSION_BPS,
    with_close_check: bool = True,
) -> dict[str, Any]:
    accruals = store.list_ledger_entries(shop_id=shop_id, basis="accrual")
    opening_snap = store.get_accrual_snapshot(
        shop_id=shop_id, kind="opening", period_key=period_key
    )
    close: dict[str, Any]
    if with_close_check:
        close = run_close_check(
            store,
            shop_id=shop_id,
            period_key=period_key,
            commission_bps=commission_bps,
        )
    else:
        close = {
            "balanced": None,
            "opening": int(opening_snap["total_amount"]) if opening_snap else 0,
            "period_delta": period_delta_cents(store, shop_id=shop_id),
            "closing": None,
            "expected_closing": None,
            "diff_amount": None,
            "recon_diffs": [],
            "equation": "opening + period_delta = closing",
        }

    open_diffs = store.list_recon_diffs(
        shop_id=shop_id, state="open", recon_line=RECON_LINE
    )
    return {
        "shop_id": shop_id,
        "period_key": period_key,
        "bootstrapped": opening_snap is not None,
        "accrual_entry_count": len(accruals),
        "accrual_total": sum(int(e["amount"]) for e in accruals),
        "opening_snapshot": opening_snap,
        "close_check": close,
        "recon_diffs": open_diffs,
        "read_only": True,
        "note": "只读台账状态。修正须经 ledger_correction 审批；本端点不改订单。",
    }
