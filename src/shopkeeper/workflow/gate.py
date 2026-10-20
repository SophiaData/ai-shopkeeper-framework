"""工作台上架闸门（DESIGN §2.1 原则 1 / §6 / SCHEMA §3.4 八态）。

草稿 API 兼容：pending→approved 后才允许 publish。
底层消费 `approvals`：approved → executing（原子 CLAIM）→ executed | execute_failed。
飞书卡片推送后置；发货/退款不在本模块；无「跳过本次」。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from shopkeeper.adapters.base import PlatformAdapter
from shopkeeper.domain.approval_state import ApprovalEvent
from shopkeeper.errors import AdapterError
from shopkeeper.logging_setup import get_logger
from shopkeeper.models import AdapterResult
from shopkeeper.side_effect import make_idempotency_key, run_side_effect
from shopkeeper.workflow.approvals import (
    ApprovalError,
    ApprovalStatus,
    apply_approval_event,
    claim_for_execute,
    ensure_listing_approval,
    payload_hash,
    sync_draft_projection,
)
from shopkeeper.workflow.catalog import upsert_from_draft
from shopkeeper.workflow.drafts import DraftError
from shopkeeper.workflow.order_sla import evaluate_orders_sla
from shopkeeper.workflow.store import WorkflowStore

log = get_logger("shopkeeper.workflow.gate")

APPROVAL_TTL = timedelta(days=7)
PUBLISH_ACTION = "publish_listing"


def _alert_execute_failed(code: str, *, order_id: str = "") -> None:
    from shopkeeper.workflow.alerts import CODE_APPROVAL_EXECUTE_FAILED, emit_alert

    emit_alert(code or CODE_APPROVAL_EXECUTE_FAILED, job_type="approval_execute", order_id=order_id)


def publish_listing(store: WorkflowStore, platform: PlatformAdapter, draft_id: str) -> dict[str, Any]:
    draft = store.get_draft(draft_id)
    if draft is None:
        raise DraftError("草稿不存在。")
    if draft["kind"] != "listing":
        raise DraftError("只有上架文案草稿可以走平台上架闸门。短视频和客服稿请人工处理。")
    if draft["status"] in {"published", "dry_run"}:
        view = dict(draft)
        view["published"] = draft["status"] == "published"
        view["sent"] = False
        view["dry_run"] = draft["status"] == "dry_run" or bool(draft.get("dry_run"))
        return view
    if draft["status"] == "rejected":
        raise DraftError("已拒绝的草稿不能上架。没有跳过本次。")
    if draft["status"] != "approved":
        raise DraftError("未审批，不能上架。须先人工通过，且没有跳过本次。")
    if _expired(draft):
        approval = store.get_approval_for_entity(
            "draft",
            draft_id,
            action_type="create_listing",
            statuses=("approved", "pending"),
        )
        if approval is not None:
            try:
                apply_approval_event(
                    store, approval["id"], ApprovalEvent.EXPIRE, note="审批超过 7 天未执行"
                )
            except ApprovalError:
                pass
        store.decide(draft_id, "expired", "审批超过 7 天未执行", allowed_from=("approved",))
        raise DraftError("审批已过期，不能上架。请重新生成并再审。")

    from shopkeeper.workflow.shops import resolve_platform

    platform = resolve_platform(platform, draft.get("shop_id"))
    approval = store.get_approval_for_entity(
        "draft",
        draft_id,
        action_type="create_listing",
        statuses=("approved", "pending", "executing"),
    )
    if approval is None:
        approval = ensure_listing_approval(store, draft)
        # 草稿已是 approved 投影，但审批单新建为 pending → 对齐为 approved
        if draft["status"] == "approved" and approval["status"] == "pending":
            try:
                approval = apply_approval_event(
                    store,
                    approval["id"],
                    ApprovalEvent.APPROVE,
                    decided_by="console",
                    note=draft.get("decision_note") or "",
                )
            except ApprovalError as exc:
                raise DraftError(str(exc)) from exc

    try:
        claimed_approval = claim_for_execute(store, approval["id"])
    except ApprovalError as exc:
        raise DraftError(str(exc)) from exc

    claimed = sync_draft_projection(
        store, draft_id, "executing", claimed_approval.get("decision_note") or ""
    )
    if claimed is None:
        current = store.get_draft(draft_id)
        if current is None:
            raise DraftError("草稿不存在。")
        if current["status"] in {"published", "dry_run"}:
            view = dict(current)
            view["published"] = current["status"] == "published"
            view["sent"] = False
            view["dry_run"] = current["status"] == "dry_run" or bool(current.get("dry_run"))
            return view
        raise DraftError("草稿状态已变，不能上架。")

    payload = _listing_payload(claimed)
    # SCHEMA §4.1：entity_id 与载荷业务 id 一致
    if str(payload.get("draft_id") or "") != draft_id or claimed_approval.get("entity_id") != draft_id:
        apply_approval_event(
            store,
            claimed_approval["id"],
            ApprovalEvent.EXECUTE_FAIL,
            execution_error="entity_id mismatch",
        )
        sync_draft_projection(store, draft_id, "execute_failed", "entity_id mismatch")
        log.error("approval execute failed code=GATE_PAYLOAD_MISMATCH draft_id=%s", draft_id)
        _alert_execute_failed("GATE_PAYLOAD_MISMATCH", order_id=draft_id)
        raise DraftError("审批实体与载荷不一致，拒绝执行。")

    adapter_name = getattr(platform, "name", "platform")
    # 含载荷哈希：同审批单 RESUBMIT 改价后再上架须新键，避免误回放旧结果
    idem_key = make_idempotency_key(
        adapter_name,
        "create_listing",
        draft_id,
        str(claimed_approval["id"]),
        payload_hash(payload),
    )

    def _create() -> AdapterResult[dict[str, Any]]:
        raw = platform.create_listing(payload)
        if not raw.ok or raw.data is None:
            return raw
        data = dict(raw.data)
        if not data.get("dry_run") and not data.get("platform_product_id"):
            return AdapterResult.failure(
                AdapterError.execution(
                    "APPROVAL_EXECUTE_FAILED",
                    "平台上架未返回 platform_product_id。",
                ),
                adapter_name,
            )
        return AdapterResult.success(data, adapter_name, raw.duration_ms)

    outcome = run_side_effect(
        store,
        adapter=adapter_name,
        operation="create_listing",
        idempotency_key=idem_key,
        request=payload,
        fn=_create,
        draft_id=draft_id,
        action=PUBLISH_ACTION,
    )
    result = outcome.result
    write = outcome.write
    if result is None or not result.ok or result.data is None:
        err = (
            result.error
            if result is not None and result.error is not None
            else AdapterError.policy("TTS_WRITE_BLOCKED", "平台写通道未返回结果")
        )
        # ⚠️ **回滚不分是不是回放**。
        #
        # 原先这里写 `if not outcome.replayed:` —— 于是「重复点击发布」这条**极常见**的路径
        # （第一次失败 → 第二次：审批单被重新 claim 成 executing、草稿被投影成 publishing，
        # 然后 `run_side_effect` 发现同幂等键已是 failed，**直接回放失败结果、不再调 fn()**）
        # 会跳过回滚，把草稿与审批单**双双永久卡死**：
        #   草稿 publishing + 审批单 executing → 之后每次发布都报「未审批，不能上架」，
        #   重新审批也不行（状态不是 pending）。
        #
        # 回放失败时并没有发生新的外呼（`run_side_effect` 直接返回了），
        # 所以回滚是安全的、也是必须的。
        _current = store.get_approval(claimed_approval["id"]) or {}
        already_rolled_back = (
            str(_current.get("status") or "") == ApprovalStatus.EXECUTE_FAILED.value
        )
        if not already_rolled_back:
            if not outcome.replayed:
                apply_approval_event(
                    store,
                    claimed_approval["id"],
                    ApprovalEvent.EXECUTE_FAIL,
                    execution_error=f"{err.code} {err.message}",
                )
                store.decide(
                    draft_id, "approved", claimed.get("decision_note") or "", allowed_from=("publishing",)
                )
            else:
                # 回放失败：审批单已被上面 claim 成 executing，草稿已被投影成 publishing，
                # 但**没有发生新的外呼**。要把两者都放回去。
                apply_approval_event(
                    store,
                    claimed_approval["id"],
                    ApprovalEvent.EXECUTE_FAIL,
                    execution_error=f"replayed: {err.code} {err.message}",
                )
                store.decide(
                    draft_id, "approved", claimed.get("decision_note") or "", allowed_from=("publishing",)
                )
            log.error("approval execute failed code=%s draft_id=%s", err.code, draft_id)
            _alert_execute_failed(err.code, order_id=draft_id)
        if err.code == "APPROVAL_EXECUTE_FAILED" and "platform_product_id" in err.message:
            raise DraftError(
                "平台上架未返回 platform_product_id。"
                "审批单已置为 execute_failed；**再次点击上架会重新开一张待审单**，"
                "确认平台侧确实没有建成商品后再重试。"
            )
        # ⚠️ 失败后的**恢复路径必须写在错误里**，否则操作者只看到「没执行」，
        # 不知道该做什么 —— 而实际路径是通畅的：
        #   execute_failed --(再次 ensure)--> pending --(owner 批准)--> approved --(CLAIM)--> executing
        # 实测确认这条链路可用（见 tests/workflow/test_execute_failed_recovery.py）。
        #
        # 另一件必须说清的事：此时**草稿是 pending、审批单是 execute_failed**，
        # 两者状态不一致是**有意为之**（草稿退回待审以便重新走闸门），
        # 但光看界面会以为「已经批过了」。所以这里显式说明。
        raise DraftError(
            f"平台上架未执行：{err.code} {err.message}。"
            "审批单已置为 execute_failed，草稿退回待审；"
            "再次点击上架会重新开一张待审单，需 owner 重新批准后才执行。"
            "请先到卖家中心确认本次是否其实已建成商品，避免重复上架。"
        )

    data = dict(result.data)
    platform_product_id = str(data.get("platform_product_id") or write.get("platform_product_id") or "") or None
    dry_run = bool(data.get("dry_run")) or str(write.get("status") or "") == "dry_run"

    saved = store.mark_published(
        draft_id,
        platform_product_id=platform_product_id,
        adapter=adapter_name,
        dry_run=dry_run,
        adapter_response=data,
    )
    if saved is None:
        # 幂等回放时草稿可能已是 published
        saved = store.get_draft(draft_id)
    if saved is None:
        raise DraftError("上架结果未能落库。")
    if not outcome.replayed:
        apply_approval_event(
            store,
            claimed_approval["id"],
            ApprovalEvent.EXECUTE_OK,
            execution_ref=str(write.get("id") or ""),
        )
    try:
        upsert_from_draft(store, saved)
    except ValueError:
        pass
    view = dict(saved)
    view["published"] = bool(saved.get("published"))
    view["sent"] = False
    view["dry_run"] = dry_run
    view["platform_product_id"] = platform_product_id
    view["approval_id"] = claimed_approval["id"]
    view["idempotent_replay"] = bool(outcome.replayed)
    return view


def sync_orders(
    store: WorkflowStore,
    platform: PlatformAdapter,
    *,
    since: str = "",
    shop_id: str | None = None,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """只读同步：完整读取后按平台+店原子 upsert，保留查询窗口外的历史订单。"""
    from shopkeeper.workflow.shops import normalize_platform, normalize_shop_id, resolve_platform

    sid = normalize_shop_id(shop_id)
    pkey = normalize_platform(platform_name or getattr(platform, "name", None))
    platform = resolve_platform(platform, sid)
    result = platform.get_orders(since)
    if not result.ok or result.data is None:
        err = result.error or AdapterError.execution("TTS_API_ERROR", "订单同步失败且无错误体")
        raise DraftError(f"订单同步失败：{err.code} {err.message}")
    rows = [dict(item) for item in result.data if isinstance(item, dict)]
    try:
        store.upsert_synced_orders(
            rows, source=getattr(platform, "name", "platform"), shop_id=sid, platform=pkey,
        )
    except ValueError as exc:
        raise DraftError(f"订单同步未提交：{exc}") from exc
    orders = store.list_synced_orders(shop_id=sid, platform=pkey)
    sla = evaluate_orders_sla(orders)
    return {
        "synced": len(rows),
        "since": since,
        "shop_id": sid,
        "platform": pkey,
        "source": getattr(platform, "name", "platform"),
        "orders": orders,
        "sla": sla,
        "note": "只读同步。发货、退款、改价不会因此发生。本地 SLA 提醒见 sla；飞书后置。",
    }


def _expired(draft: dict[str, Any]) -> bool:
    raw = draft.get("decided_at")
    if not raw:
        return False
    try:
        decided = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if decided.tzinfo is None:
        decided = decided.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - decided > APPROVAL_TTL


def _listing_payload(draft: dict[str, Any]) -> dict[str, Any]:
    """上架载荷 + 内容摘要。

    ## 为什么把**已批准的价格**也放进摘要

    原先 body 只有 title/body/input，**不含价格**。后果是：
    人工批准了偏离价（例如 21000），之后草稿 payload 里的 `pricing` 被改动，
    `payload_digest` **完全不变** —— 「批准了 A 价、实际按 B 价上架」这件事
    **无法被发现**。

    ## 为什么不把价格放进**发给平台**的字段

    查过两侧实现：`TikTokShopClient.create_listing` 只发
    `draft_id` + `payload_digest`（`tiktok_shop.py` 的 `_dispatch_signed`），
    平台侧价格由草稿本身决定。**加一个平台不认的 `price` 字段属于臆造外部契约**（§0），
    所以这里**不新增**外呼字段。

    做的是能确定做对的那件事：把已批准价格纳入**摘要**，
    使「批准价与上架时价格不一致」成为**可检出**的事实，
    而不是静默发生。`approved_price_thb` 是**审计用的伴随字段**，
    与 `payload_digest` 一起被 `gate` 记录，不参与平台调用。
    """
    payload = draft.get("payload") or {}
    pricing = payload.get("pricing") or {}
    # 价格只取**数值**参与摘要；缺失时用 None（而不是省略），
    # 这样「批准时有价、上架时没价」同样会改变摘要。
    approved_price = pricing.get("suggested_price_thb")
    body = {
        "draft_id": draft["id"],
        "title_th": draft.get("title") or "",
        "title_en": payload.get("title_en") or "",
        "body_th": draft.get("body_th") or "",
        "body_en": draft.get("body_en") or "",
        "input": payload.get("input") or {},
        "approved_price_thb": int(approved_price) if approved_price is not None else None,
        "price_source": pricing.get("price_source"),
    }
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    body["payload_digest"] = digest
    return body
