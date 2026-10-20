"""平台商品审核状态轮询（P0-07）。

发布后 TikTok 侧要过一轮审核（AUDITING → APPROVED / FAILED）。此前
`GET /api/workflow/products/{id}/audit` 只做实时查询、不落库：离线或平台不可达
时最后一次已知状态就丢了，`/admin` 商品页也看不到。

本模块做三件事：

1. **只读轮询**：遍历已发布且带 `platform_product_id` 的 catalog 条目，调
   `platform.get_product_audit_status()`，把结果回写 `catalog_items` 的三列
   （v9 迁移）。**不改审批状态、不触发任何写通道、不开 TTS 真写。**
2. **限频**：同一条目在 `min_interval_seconds` 内不重复查，避免刷平台接口。
3. **FAILED 告警**：审核失败调 `alerts.emit_alert`，人工可介入。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shopkeeper.workflow.alerts import emit_alert
from shopkeeper.workflow.store import WorkflowStore

# 审核失败告警码（新增；见 docs/error-codes.md 的告警码段）。
CODE_PLATFORM_AUDIT_FAILED = "PLATFORM_AUDIT_FAILED"

# 平台返回的审核态归一化。TikTok 侧文案可能大小写不一。
_APPROVED = {"APPROVED", "APPROVE", "PASS", "PASSED", "SUCCESS", "ONLINE"}
_FAILED = {"FAILED", "FAIL", "REJECTED", "REJECT", "VIOLATION", "OFFLINE"}
_AUDITING = {"AUDITING", "AUDIT", "PENDING", "IN_REVIEW", "UNDER_REVIEW", "PROCESSING"}

DEFAULT_MIN_INTERVAL_SECONDS = 900


def normalize_audit_status(raw: Any) -> str:
    """把平台原始态归一到 AUDITING / APPROVED / FAILED / UNKNOWN。

    认不出的态归 UNKNOWN —— 不猜成 APPROVED（会把未审过的当已通过）。
    """
    text = str(raw or "").strip().upper()
    if not text:
        return "UNKNOWN"
    if text in _APPROVED:
        return "APPROVED"
    if text in _FAILED:
        return "FAILED"
    if text in _AUDITING:
        return "AUDITING"
    return "UNKNOWN"


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _extract(payload: dict[str, Any]) -> tuple[str, str]:
    """从适配器返回值里取 (归一状态, 失败原因)。

    适配器可能把状态放在顶层，也可能包在 `data` 里；两处都看。
    """
    nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    status_raw = (
        payload.get("audit_status")
        or payload.get("status")
        or nested.get("audit_status")
        or nested.get("status")
    )
    reason = str(
        payload.get("audit_reason")
        or payload.get("reason")
        or payload.get("message")
        or nested.get("audit_reason")
        or nested.get("reason")
        or ""
    )
    return normalize_audit_status(status_raw), reason


def poll_audit_statuses(
    store: WorkflowStore,
    platform: Any,
    *,
    now: datetime | None = None,
    min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
    limit: int = 50,
) -> dict[str, Any]:
    """轮询已发布商品的审核状态并回写。返回汇总（可断言）。

    平台不支持 `get_product_audit_status` 时直接返回 `supported=False`，
    不报错、不改库。
    """
    getter = getattr(platform, "get_product_audit_status", None)
    if getter is None and not callable(getattr(platform, "for_shop", None)):
        return {"supported": False, "checked": 0, "updated": 0, "skipped": 0, "failed": 0, "results": []}

    moment = now or datetime.now(timezone.utc)
    items = [
        item
        for item in store.list_catalog_items()
        if str(item.get("platform_product_id") or "").strip()
    ][:limit]

    checked = updated = skipped = failed = 0
    results: list[dict[str, Any]] = []

    for item in items:
        item_id = str(item["id"])
        product_id = str(item["platform_product_id"])

        last = _parse_dt(item.get("audit_checked_at"))
        if last is not None and (moment - last).total_seconds() < max(0, min_interval_seconds):
            skipped += 1
            results.append({"id": item_id, "product_id": product_id, "action": "skipped"})
            continue

        from shopkeeper.workflow.shops import resolve_platform
        from shopkeeper.workflow.drafts import DraftError

        try:
            getter = getattr(resolve_platform(platform, item.get("shop_id")), "get_product_audit_status", None)
        except DraftError:
            results.append({"id": item_id, "product_id": product_id, "action": "error", "code": "SHOP_CREDENTIALS_MISSING"})
            continue
        if not callable(getter):
            skipped += 1
            continue
        outcome = getter(product_id)
        checked += 1
        if not getattr(outcome, "ok", False) or not isinstance(getattr(outcome, "data", None), dict):
            err = getattr(outcome, "error", None)
            code = getattr(err, "code", "UNKNOWN")
            results.append({"id": item_id, "product_id": product_id, "action": "error", "code": code})
            # 查询失败不写状态（避免把「查不到」误记成「未通过」），只记结果。
            continue

        status, reason = _extract(dict(outcome.data))
        store.set_catalog_audit_status(
            item_id, status=status, reason=reason, checked_at=moment.isoformat()
        )
        updated += 1
        results.append(
            {"id": item_id, "product_id": product_id, "action": "updated", "status": status}
        )

        if status == "FAILED":
            failed += 1
            emit_alert(CODE_PLATFORM_AUDIT_FAILED, job_type="platform_audit", order_id=item_id)

    return {
        "supported": True,
        "checked": checked,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }
