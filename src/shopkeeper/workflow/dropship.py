"""1688 代发草稿与执行（DESIGN §9 M3 / §6 必审）。

从已同步订单生成代发草稿 → 八态审批 action=dropship_order →
批准后：

- mock：execute 调 FulfillmentAdapter.place_dropship_order（本地假单号、不付款）。
- manual：execute 只打开可复制的下单信息，不调用下单接口；
  人在 1688 下单后 record_alibaba_order 回填单号。

物流单号见执行结果（人工通道在回填前没有物流单号）；回填见 workflow/tracking。
发货/退款不在本模块。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from shopkeeper.adapters.alibaba_dropship import (
    alibaba_order_detail_url,
    parse_alibaba_order_id,
    shipping_lines_for_copy,
)
from shopkeeper.adapters.base import FulfillmentAdapter
from shopkeeper.domain.approval_state import ApprovalEvent, ApprovalStatus
from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult
from shopkeeper.side_effect import make_idempotency_key, run_side_effect
from shopkeeper.workflow.approvals import (
    ApprovalError,
    apply_approval_event,
    claim_for_execute,
    evaluate_gate_policy,
    payload_hash,
)
from shopkeeper.workflow.drafts import DraftError
from shopkeeper.workflow.store import WorkflowStore

DROPSHIP_ACTION = "dropship_order"
DECIDE_TTL = timedelta(days=7)
EXECUTE_ACTION = "dropship_place"


def create_dropship_draft(
    store: WorkflowStore,
    fulfillment: FulfillmentAdapter,
    order_id: str,
) -> dict[str, Any]:
    """从已同步订单生成代发草稿并建 pending 审批单。不外呼、不付款。"""
    oid = str(order_id or "").strip()
    if not oid:
        raise DraftError("order_id 不能为空。")
    order = store.get_synced_order(oid)
    if order is None:
        raise DraftError("订单不存在。请先同步订单。")
    from shopkeeper.workflow.order_eligibility import order_is_fulfillable

    if not order_is_fulfillable(order):
        raise DraftError(f"订单状态 {order.get('status')!r} 不可代发；只处理已支付且未发货订单。")

    existing = store.get_dropship_draft(oid)
    if existing and existing.get("status") == "executed":
        return {
            "draft": existing,
            "approval": store.get_approval(existing["approval_id"]) if existing.get("approval_id") else None,
            "note": "该订单代发已执行，返回既有结果。",
        }
    if existing and existing.get("status") == "awaiting_manual_po":
        return {
            "draft": existing,
            "approval": store.get_approval(existing["approval_id"]) if existing.get("approval_id") else None,
            "note": "人工下单信息已生成，返回既有草稿。未付款。",
        }

    result = fulfillment.build_dropship_draft(order)
    if not result.ok or result.data is None:
        err = result.error or AdapterError.execution("DROPSHIP_DRAFT_FAILED", "代发草稿生成失败")
        raise DraftError(f"代发草稿失败：{err.code} {err.message}")

    draft_payload = dict(result.data)
    approval = _ensure_dropship_approval(store, oid, draft_payload)
    saved = store.upsert_dropship_draft(
        {
            "order_id": oid,
            "status": "pending_approval" if approval["status"] == "pending" else approval["status"],
            "payload": draft_payload,
            "approval_id": approval["id"],
            "external_po_id": draft_payload.get("external_po_id"),
            "result": None,
        }
    )
    if str(draft_payload.get("mode") or "") == "manual":
        note = "已生成人工下单草稿并进入审批。批准后复制信息到 1688 自行下单；本系统不付款。"
    else:
        note = "已生成代发草稿并进入审批。默认不调用真实 1688、不付款。"
    return {
        "draft": saved,
        "approval": approval,
        "note": note,
    }


def execute_dropship(
    store: WorkflowStore,
    fulfillment: FulfillmentAdapter,
    order_id: str,
) -> dict[str, Any]:
    """审批通过后 CLAIM → mock 下单 → executed。未审批 → DraftError（HTTP 409）。"""
    oid = str(order_id or "").strip()
    if not oid:
        raise DraftError("order_id 不能为空。")
    draft = store.get_dropship_draft(oid)
    if draft is None:
        raise DraftError("代发草稿不存在。请先 POST …/dropship/draft。")
    from shopkeeper.workflow.order_eligibility import order_is_fulfillable

    order = store.get_synced_order(oid)
    if order is None:
        raise DraftError("订单不存在。请先同步订单。")
    if not order_is_fulfillable(order):
        raise DraftError(f"订单状态 {order.get('status')!r} 已不可代发；执行前复核失败。")

    approval = None
    if draft.get("approval_id"):
        approval = store.get_approval(str(draft["approval_id"]))
    if approval is None:
        approval = store.get_approval_for_entity(
            "order",
            oid,
            action_type=DROPSHIP_ACTION,
            statuses=("approved", "pending", "executing", "executed"),
        )
    if approval is None:
        raise DraftError("未审批，不能代发下单。须先人工通过，且没有跳过本次。")
    if (approval.get("action_type") != DROPSHIP_ACTION
            or approval.get("entity_type") != "order"
            or approval.get("entity_id") != oid):
        raise DraftError("代发审批与订单不匹配，不能执行。")
    from shopkeeper.security.redaction import guard_payload

    guarded_draft, _guard_report = guard_payload(
        dict(draft.get("payload") or {}), row_key="dropship.approval", sniff=True,
    )
    if payload_hash(guarded_draft) != approval.get("payload_hash"):
        raise DraftError(
            "代发草稿与批准快照不一致，请重新生成草稿并审批；不能用旧批准执行新内容。"
        )
    frozen_payload = dict(approval.get("payload") or {})
    frozen_payload["order_id"] = oid
    if approval["status"] == ApprovalStatus.EXECUTED.value:
        return {
            "draft": draft,
            "approval": approval,
            "adapter": draft.get("result") or {},
            "note": "审批单已 executed。",
        }
    if approval["status"] == ApprovalStatus.PENDING.value:
        raise DraftError("未审批，不能代发下单。须先人工通过，且没有跳过本次。")
    if approval["status"] == ApprovalStatus.REJECTED.value:
        raise DraftError("已拒绝，不能代发下单。没有跳过本次。")
    if approval["status"] == ApprovalStatus.EXPIRED.value:
        raise DraftError("审批已过期，不能代发下单。请重新生成草稿并再审。")
    if approval["status"] not in {
        ApprovalStatus.APPROVED.value,
        ApprovalStatus.EXECUTING.value,
    }:
        raise DraftError(f"当前审批状态 {approval['status']} 不能代发下单。")

    payload = frozen_payload
    if _is_manual_dropship(fulfillment, payload):
        return open_manual_order_sheet(store, draft, approval, payload)
    if getattr(fulfillment, "manual_only", False):
        raise DraftError(
            "DROPSHIP_NOT_MANUAL 当前是人工下单通道，但这张草稿不是 manual。"
            "请重新生成草稿。本系统不付款。"
        )

    try:
        claimed = claim_for_execute(store, approval["id"])
    except ApprovalError as exc:
        text = str(exc)
        if "未审批" in text or "过期" in text:
            raise DraftError(
                text.replace("不能上架", "不能代发下单").replace("请重新生成并再审", "请重新生成草稿并再审")
            ) from exc
        raise DraftError(str(exc)) from exc

    adapter_name = getattr(fulfillment, "name", "fulfillment")
    idem_key = make_idempotency_key(adapter_name, "place_dropship", oid, str(claimed["id"]))

    def _place() -> AdapterResult[dict[str, Any]]:
        return fulfillment.place_dropship_order(payload)

    outcome = run_side_effect(
        store,
        adapter=adapter_name,
        operation="place_dropship",
        idempotency_key=idem_key,
        request=payload,
        fn=_place,
        draft_id=None,
        action=EXECUTE_ACTION,
    )
    result = outcome.result
    write = outcome.write
    if result is None or not result.ok or result.data is None:
        err = (
            result.error
            if result is not None and result.error is not None
            else AdapterError.execution("DROPSHIP_PLACE_FAILED", "代发下单失败")
        )
        if str(err.code) == "EXT_IDEMPOTENCY_CONFLICT":
            raise DraftError(
                "代发下单结果未知：同一幂等键仍在执行。"
                "已保留执行中状态，请人工核对后再处理；不会自动重试。"
            )
        # 本次失败与同一审批重审后回放的历史失败都要落到 execute_failed，
        # 不能因为 replayed 就停在 executing。
        apply_approval_event(
            store,
            claimed["id"],
            ApprovalEvent.EXECUTE_FAIL,
            execution_error=f"{err.code} {err.message}",
        )
        store.upsert_dropship_draft(
            {
                "order_id": oid,
                "status": "execute_failed",
                "payload": payload,
                "approval_id": claimed["id"],
                "external_po_id": None,
                "result": err.model_dump(),
            }
        )
        raise DraftError(f"代发下单未执行：{err.code} {err.message}")

    data = dict(result.data)
    external_po_id = str(data.get("external_po_id") or "") or None
    # 幂等回放同样是已确认结果：必须推进审批到 executed。
    apply_approval_event(
        store,
        claimed["id"],
        ApprovalEvent.EXECUTE_OK,
        execution_ref=str(write.get("id") or ""),
    )
    saved = store.upsert_dropship_draft(
        {
            "order_id": oid,
            "status": "executed",
            "payload": payload,
            "approval_id": claimed["id"],
            "external_po_id": external_po_id,
            "result": data,
        }
    )
    return {
        "draft": saved,
        "approval": store.get_approval(claimed["id"]),
        "adapter": data,
        "paid": bool(data.get("paid")),
        "idempotent_replay": bool(outcome.replayed),
        "note": data.get("note")
        or "mock 代发已落本地。未付款；物流单号见 result；回填见 …/tracking。发货/退款仍禁止。",
    }


def _ensure_dropship_approval(
    store: WorkflowStore,
    order_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """确保代发审批单与冻结业务快照一致。

    待审草稿可刷新载荷与哈希；已批准/执行中/已执行快照不可改写。
    """
    from shopkeeper.security.redaction import guard_payload

    # **先脱敏，再算 hash** —— 顺序不能反；否则行内 payload 与 hash 不一致。
    guarded, _guard_report = guard_payload(payload, row_key="dropship.approval", sniff=True)
    new_hash = payload_hash(guarded)

    existing = store.get_approval_for_entity(
        "order",
        order_id,
        action_type=DROPSHIP_ACTION,
        statuses=(
            "pending",
            "approved",
            "executing",
            "executed",
            "stale",
            "execute_failed",
        ),
    )
    if existing:
        same = existing.get("payload_hash") == new_hash and existing.get("payload") == guarded
        if existing["status"] == "pending":
            if same:
                return existing
            updated = store.update_pending_approval_snapshot(
                existing["id"], guarded, hash_value=new_hash,
                expected_hash=existing.get("payload_hash"),
            )
            if updated is None:
                raise DraftError("代发草稿状态已变，请刷新后重试。")
            return updated
        if existing["status"] in {"approved", "executing", "executed"}:
            if same:
                return existing
            raise DraftError(
                "代发草稿已批准或已执行，不能改写已冻结的业务快照。"
                "请先完成或取消当前审批；不要用旧批准执行新内容。"
            )
        if existing["status"] in {"stale", "execute_failed"}:
            # 旧执行已有独立身份。这里**不能**把旧审批 RESUBMIT 回 pending：
            # 迟到的旧执行结果可能在同一 ID 上写 EXECUTE_OK，覆盖新草稿。
            # 直接落到下面创建一张新审批单；旧单保持失败/过期，供审计。
            pass

    policy = store.get_gate_policy(DROPSHIP_ACTION) or store.get_gate_policy("place_po")
    decision = evaluate_gate_policy(policy, action_type=DROPSHIP_ACTION)
    store.add_gate_decision(
        approval_id=None,
        policy_id=str(policy["id"] if policy else "missing"),
        policy_version=int(policy["version"] if policy else 0),
        input_snapshot={"order_id": order_id, "action": DROPSHIP_ACTION},
        decision=decision["decision"],
        reason=decision["reason"],
    )
    now = datetime.now(timezone.utc)
    order = store.get_synced_order(order_id) or {}
    return store.add_approval(
        {
            "shop_id": order.get("shop_id"),
            "action_type": DROPSHIP_ACTION,
            "entity_type": "order",
            "entity_id": order_id,
            "payload": guarded,
            "payload_hash": new_hash,
            "status": ApprovalStatus.PENDING.value,
            "channel": "feishu",
            "policy_id": (policy or {}).get("id"),
            "decide_before": (now + DECIDE_TTL).isoformat(),
            "execute_within_seconds": int(DECIDE_TTL.total_seconds()),
        }
    )


def apply_event_resubmit(store: WorkflowStore, existing: dict[str, Any]) -> dict[str, Any] | None:
    from shopkeeper.domain.approval_state import apply_event

    out = apply_event(ApprovalStatus(existing["status"]), ApprovalEvent.RESUBMIT)
    if not out.applied:
        return None
    return store.transition_approval(
        existing["id"],
        to_status=out.status.value,
        from_statuses=(existing["status"],),
        decision_note="重新进入待审",
        clear_executing=True,
    )


def _is_manual_dropship(_fulfillment: FulfillmentAdapter, payload: dict[str, Any]) -> bool:
    """人工草稿只看 payload.mode，避免换一个 mock 适配器就把假单号写进去。"""
    return str(payload.get("mode") or "") == "manual"


def _no_pay() -> dict[str, Any]:
    return {
        "paid": False,
        "auto_pay": False,
        "payment_url": None,
        "payment_ref": None,
        "called_alibaba": False,
    }


def manual_order_view(store: WorkflowStore, order_id: str) -> dict[str, Any]:
    """可复制的下单信息。收货行只出现在返回值里，不写回草稿。"""
    oid = str(order_id or "").strip()
    if not oid:
        raise DraftError("order_id 不能为空。")
    draft = store.get_dropship_draft(oid)
    if draft is None:
        raise DraftError("代发草稿不存在。请先 POST …/dropship/draft。")
    payload = dict(draft.get("payload") or {})
    if str(payload.get("mode") or "") != "manual":
        raise DraftError(
            "DROPSHIP_NOT_MANUAL 该草稿不是人工下单通道。mock 单不能当成 1688 单号。"
        )
    sheet = dict(payload.get("manual_order") or {})
    copy = str(sheet.get("copy_text") or "")
    order = store.get_synced_order(oid) or {}
    shipping = shipping_lines_for_copy(order)
    if shipping:
        copy = copy + "\n" + "\n".join(shipping)
    else:
        copy = copy + "\n收货信息：未随订单同步，请在卖家中心核对该订单后再下单。"
    approval = store.get_approval(draft["approval_id"]) if draft.get("approval_id") else None
    status = str((approval or {}).get("status") or "")
    po = str(draft.get("external_po_id") or "") or None
    ready = status in {ApprovalStatus.APPROVED.value, ApprovalStatus.EXECUTING.value} and not po
    if po:
        note = "已回填 1688 单号。本系统未付款、未发货。"
    elif ready:
        note = "可以复制下面的信息，到 1688 买家后台自行下单。本系统不付款。"
    else:
        note = "审批通过前不要去 1688 下单。"
    detail_url = None
    if po:
        detail_url = alibaba_order_detail_url(po)
    body = {
        "order_id": oid,
        "status": draft.get("status"),
        "ready_to_order": ready,
        "copy_text": copy,
        "links": list(sheet.get("links") or []),
        "lines": list(sheet.get("lines") or []),
        "external_request_id": sheet.get("external_request_id") or payload.get("external_request_id"),
        "alibaba_order_id": po,
        "order_detail_url": detail_url,
        "note": note,
    }
    body.update(_no_pay())
    return body


def open_manual_order_sheet(
    store: WorkflowStore,
    draft: dict[str, Any],
    approval: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """批准后只把草稿标成待人工下单。不 claim、不写 external_writes、不付款。"""
    oid = str(payload.get("order_id") or draft.get("order_id") or "").strip()
    saved = store.upsert_dropship_draft(
        {
            "order_id": oid,
            "status": "awaiting_manual_po",
            "payload": payload,
            "approval_id": approval.get("id") or draft.get("approval_id"),
            "external_po_id": None,
            "result": None,
        }
    )
    view = manual_order_view(store, oid)
    return {
        "draft": saved,
        "approval": approval,
        "manual_order": view,
        "note": "人工下单信息已可复制。请到 1688 买家后台自行下单；本系统不调用下单接口、不付款。下单后回填单号。",
        **_no_pay(),
    }


def _recorded_response(
    store: WorkflowStore,
    draft: dict[str, Any],
    *,
    replayed: bool,
) -> dict[str, Any]:
    approval = store.get_approval(draft["approval_id"]) if draft.get("approval_id") else None
    po = str(draft.get("external_po_id") or "")
    return {
        "draft": draft,
        "approval": approval,
        "alibaba_order_id": po,
        "order_detail_url": alibaba_order_detail_url(po) if po else None,
        "idempotent_replay": replayed,
        "note": "已回填 1688 单号。本系统未下单、未付款、未发货。",
        **_no_pay(),
    }


def record_alibaba_order(
    store: WorkflowStore,
    order_id: str,
    alibaba_order_id: str,
) -> dict[str, Any]:
    """人工回填 1688 单号。不调用下单或付款接口，也不写 external_writes。"""
    oid = str(order_id or "").strip()
    if not oid:
        raise DraftError("order_id 不能为空。")
    draft = store.get_dropship_draft(oid)
    if draft is None:
        raise DraftError("代发草稿不存在。请先 POST …/dropship/draft。")
    payload = dict(draft.get("payload") or {})
    if str(payload.get("mode") or "") != "manual":
        raise DraftError(
            "DROPSHIP_NOT_MANUAL 该草稿不是人工下单通道。mock 单不能当成 1688 单号。"
        )
    parsed = parse_alibaba_order_id(alibaba_order_id)
    if not parsed:
        raise DraftError(
            "DROPSHIP_PO_INVALID 1688 单号须为 16 到 20 位数字，或 1688 订单详情页链接。"
            "付款链接不能回填。"
        )
    approval = None
    if draft.get("approval_id"):
        approval = store.get_approval(str(draft["approval_id"]))
    if approval is None:
        approval = store.get_approval_for_entity(
            "order",
            oid,
            action_type=DROPSHIP_ACTION,
            statuses=("approved", "pending", "executing", "executed"),
        )
    if approval is None:
        raise DraftError("未审批，不能回填 1688 单号。须先人工通过，且没有跳过本次。")
    if (approval.get("action_type") != DROPSHIP_ACTION
            or approval.get("entity_type") != "order"
            or approval.get("entity_id") != oid):
        raise DraftError("代发审批与订单不匹配，不能回填 1688 单号。")
    from shopkeeper.security.redaction import guard_payload

    guarded_draft, _guard_report = guard_payload(
        dict(draft.get("payload") or {}), row_key="dropship.approval", sniff=True,
    )
    if payload_hash(guarded_draft) != approval.get("payload_hash"):
        raise DraftError(
            "代发草稿与批准快照不一致，请重新生成草稿并审批；不能用旧批准回填新内容。"
        )
    payload = dict(approval.get("payload") or {})
    existing_po = str(draft.get("external_po_id") or "")
    if existing_po:
        if existing_po == parsed:
            return _recorded_response(store, draft, replayed=True)
        raise DraftError(
            "DROPSHIP_PO_CONFLICT 已回填过不同的 1688 单号，不能覆盖。未付款。"
        )
    if approval["status"] == ApprovalStatus.PENDING.value:
        raise DraftError("未审批，不能回填 1688 单号。须先人工通过，且没有跳过本次。")
    if approval["status"] == ApprovalStatus.REJECTED.value:
        raise DraftError("已拒绝，不能回填 1688 单号。没有跳过本次。")
    if approval["status"] == ApprovalStatus.EXPIRED.value:
        raise DraftError("审批已过期，不能回填 1688 单号。请重新生成草稿并再审。")
    if approval["status"] == ApprovalStatus.EXECUTED.value:
        raise DraftError("审批已结束但没有 1688 单号。请重新生成草稿并再审。未付款。")
    if approval["status"] != ApprovalStatus.APPROVED.value:
        raise DraftError(f"当前审批状态 {approval['status']} 不能回填 1688 单号。")

    try:
        claimed = claim_for_execute(store, approval["id"])
    except ApprovalError as exc:
        text = str(exc)
        if "未审批" in text or "过期" in text:
            raise DraftError(
                text.replace("不能上架", "不能回填 1688 单号").replace(
                    "请重新生成并再审", "请重新生成草稿并再审"
                )
            ) from exc
        raise DraftError(str(exc)) from exc

    result = {
        "order_id": oid,
        "alibaba_order_id": parsed,
        "external_po_id": parsed,
        "external_request_id": payload.get("external_request_id"),
        "order_detail_url": alibaba_order_detail_url(parsed),
        "state": "RECORDED",
        "manual": True,
        "carrier": None,
        "tracking_no": None,
        "note": "已回填 1688 单号。本系统未调用 1688、未付款、未发货。",
        **_no_pay(),
    }
    apply_approval_event(
        store,
        claimed["id"],
        ApprovalEvent.EXECUTE_OK,
        execution_ref=parsed,
    )
    saved = store.upsert_dropship_draft(
        {
            "order_id": oid,
            "status": "executed",
            "payload": payload,
            "approval_id": claimed["id"],
            "external_po_id": parsed,
            "result": result,
        }
    )
    return _recorded_response(store, saved, replayed=False)
