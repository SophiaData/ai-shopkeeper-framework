"""客服话术草稿 + 白名单自动回复 stub（DESIGN §6 / §9 M3）。

入站有两条路：卖家本地录入（``POST /api/workflow/cs/inbox``），以及只读拉取
（``pull_cs_inbox``）。本仓库 TikTok 适配器没有客服消息读接口契约，拉取不外呼、
不臆造端点。回复仍是草稿：白名单免审后 stub 发送；退款/赔偿/差价/投诉等必审
``action=auto_reply``（八态）；未审批 → 409。发送恒为本地 stub，不外呼。
发货/退款不在本模块；不上架闸门 publish_listing；不付款。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from shopkeeper.domain.approval_state import ApprovalEvent, ApprovalStatus
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
from shopkeeper.workflow.dropship import apply_event_resubmit
from shopkeeper.workflow.store import WorkflowStore

CS_ACTION = "auto_reply"
EXECUTE_ACTION = "send_message"
DECIDE_TTL = timedelta(days=7)
CS_INBOX_NO_CONTRACT = "CS_INBOX_NO_CONTRACT"

# 这些名字只用于拒绝「看起来像要外呼」的开关，不是 TikTok 路径。
_LIVE_PULL_ENV_KEYS = (
    "CS_INBOX_LIVE",
    "CS_INBOX_PULL",
    "CS_INBOX_URL",
    "CS_INBOX_ENDPOINT",
    "TTS_WRITE_ENABLED",
)

# 默认白名单：仅低风险物流/时效类（DESIGN §6）
DEFAULT_AUTO_REPLY_CATEGORIES = frozenset(
    {
        "logistics_query",
        "tracking_status",
        "sla_reminder",
    }
)

# 涉钱/投诉类：即便误配进 CS_AUTO_REPLY_CATEGORIES 也强制必审
NEVER_AUTO_CATEGORIES = frozenset(
    {
        "refund",
        "compensation",
        "price_match",
        "complaint",
        "price_change",
    }
)


def auto_reply_categories(env: dict[str, str] | None = None) -> frozenset[str]:
    """白名单类目：CS_AUTO_REPLY_CATEGORIES 逗号分隔；未设则用默认。"""
    source = env if env is not None else os.environ
    raw = str(source.get("CS_AUTO_REPLY_CATEGORIES") or "").strip()
    if not raw:
        return DEFAULT_AUTO_REPLY_CATEGORIES
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return frozenset(parts) if parts else DEFAULT_AUTO_REPLY_CATEGORIES


def is_whitelist_category(category: str, env: dict[str, str] | None = None) -> bool:
    cat = str(category or "").strip().lower()
    if not cat or cat in NEVER_AUTO_CATEGORIES:
        return False
    return cat in auto_reply_categories(env)


def outbound_send_calls_platform(env: dict[str, str] | None = None) -> bool:
    """回复发送是否外呼平台。恒为 False。

    没有客服写接口契约。``TTS_WRITE_ENABLED`` 与任何客服开关都不能把 stub 改成外呼。
    """
    del env
    return False


def pull_cs_inbox(
    store: WorkflowStore | None = None,
    *,
    shop_id: str | None = None,
    platform: Any | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """只读拉取 TikTok 客服收件箱。

    ``adapters/tiktok_shop.py`` 的已签名路径是授权店铺、商品、库存、订单与令牌刷新，
    没有会话或消息读接口。不构造 HTTP 客户端，不调用 ``platform``，不把「没读」写成
    ``pulled=0``（0 会被当成收件箱是空的）。不写入 ``cs_messages``。
    """
    del store, platform
    source = env if env is not None else os.environ
    # 读开关只为了确认它们存在时也不改行为；值本身（可能是 URL 或令牌）不进响应。
    _ = [source.get(key) for key in _LIVE_PULL_ENV_KEYS]
    return {
        "ok": False,
        "connected": False,
        "platform_called": False,
        "endpoint": None,
        "endpoint_invented": False,
        "code": CS_INBOX_NO_CONTRACT,
        "pulled": None,
        "inserted": 0,
        "shop_id": shop_id,
        "source": "stub",
        "note": (
            "未臆造端点：本仓库 TikTok 适配器没有客服消息只读契约"
            "（已签名路径只覆盖授权店铺、商品、库存、订单与令牌刷新）。"
            "不构造 HTTP 客户端，不把空结果当成收件箱是空的。"
            "本地入站仍走 POST /api/workflow/cs/inbox。"
            "回复仍是草稿：白名单 stub 发送，其余须审批；发送不外呼。"
            "接通需要 owner 提供可核对的官方客服读接口文档。"
        ),
    }


def ingest_inbound(
    store: WorkflowStore,
    *,
    conversation_id: str,
    body: str,
    category: str,
    order_id: str | None = None,
    lang: str = "th",
    shop_id: str | None = None,
) -> dict[str, Any]:
    """写入本地 mock 入站消息。不外呼平台。"""
    cid = str(conversation_id or "").strip()
    text = str(body or "").strip()
    cat = str(category or "").strip().lower()
    if not cid:
        raise DraftError("conversation_id 不能为空。")
    if not text:
        raise DraftError("body 不能为空。")
    if not cat:
        raise DraftError("category 不能为空。")
    lang_code = str(lang or "th").strip().lower() or "th"
    oid = str(order_id or "").strip() or None
    return store.add_cs_message(
        {
            "shop_id": shop_id,
            "conversation_id": cid,
            "order_id": oid,
            "category": cat,
            "body": text,
            "lang": lang_code,
            "direction": "inbound",
        }
    )


def create_cs_draft(
    store: WorkflowStore,
    *,
    message_id: str | None = None,
    conversation_id: str | None = None,
    category: str | None = None,
    body: str | None = None,
    order_id: str | None = None,
    lang: str | None = None,
    auto_send: bool = False,
    env: dict[str, str] | None = None,
    shop_id: str | None = None,
) -> dict[str, Any]:
    """根据入站消息（或内联字段）生成泰/英话术草稿。白名单免审；其余建审批。"""
    inbound: dict[str, Any] | None = None
    mid = str(message_id or "").strip()
    if mid:
        inbound = store.get_cs_message(mid)
        if inbound is None:
            raise DraftError("入站消息不存在。请先 POST /api/workflow/cs/inbox。")
    else:
        if not conversation_id or not body or not category:
            raise DraftError("须提供 message_id，或同时提供 conversation_id / category / body。")
        inbound = ingest_inbound(
            store,
            conversation_id=conversation_id,
            body=body,
            category=category,
            order_id=order_id,
            lang=lang or "th",
            shop_id=shop_id,
        )

    cat = str(category or inbound["category"]).strip().lower()
    cid = str(conversation_id or inbound["conversation_id"]).strip()
    oid = str(order_id or inbound.get("order_id") or "").strip() or None
    lang_code = str(lang or inbound.get("lang") or "th").strip().lower() or "th"
    buyer_body = str(inbound.get("body") or "")
    sid = shop_id or inbound.get("shop_id")

    order_ctx = store.get_synced_order(oid) if oid else None
    reply = _render_reply(cat, buyer_body, order_ctx, lang_code)

    # 涉钱/投诉硬闸：永不免审
    whitelist = is_whitelist_category(cat, env)
    if cat in NEVER_AUTO_CATEGORIES:
        whitelist = False

    policy = store.get_gate_policy(CS_ACTION)
    decision = evaluate_gate_policy(
        policy,
        action_type=CS_ACTION,
        context={"category": cat, "whitelist": whitelist},
    )
    require_approval = bool(decision.get("require_approval"))
    if cat in NEVER_AUTO_CATEGORIES:
        require_approval = True
        whitelist = False

    if require_approval:
        saved = store.upsert_cs_draft(
            {
                "shop_id": sid,
                "conversation_id": cid,
                "inbound_message_id": inbound["id"],
                "order_id": oid,
                "category": cat,
                "lang": lang_code,
                "body_th": reply["body_th"],
                "body_en": reply["body_en"],
                "status": "pending_approval",
                "whitelist": False,
                "approval_id": None,
                "sent_stub": False,
                "result": None,
            }
        )
        approval = _ensure_cs_approval(store, saved["id"], saved, decision)
        saved = store.upsert_cs_draft(
            {
                "id": saved["id"],
                "shop_id": sid,
                "conversation_id": cid,
                "inbound_message_id": inbound["id"],
                "order_id": oid,
                "category": cat,
                "lang": lang_code,
                "body_th": reply["body_th"],
                "body_en": reply["body_en"],
                "status": "pending_approval" if approval["status"] == "pending" else approval["status"],
                "whitelist": False,
                "approval_id": approval["id"],
                "sent_stub": False,
                "result": None,
            }
        )
        out: dict[str, Any] = {
            "draft": saved,
            "approval": approval,
            "inbound": inbound,
            "gate": decision,
            "whitelist": False,
            "note": "非白名单类目：已建 auto_reply 审批。未批准不得发送。不外呼平台。",
        }
        if auto_send:
            out["note"] += " auto_send 已忽略（须审批）。"
        return out

    saved = store.upsert_cs_draft(
        {
            "shop_id": sid,
            "conversation_id": cid,
            "inbound_message_id": inbound["id"],
            "order_id": oid,
            "category": cat,
            "lang": lang_code,
            "body_th": reply["body_th"],
            "body_en": reply["body_en"],
            "status": "ready",
            "whitelist": True,
            "approval_id": None,
            "sent_stub": False,
            "result": None,
        }
    )
    store.add_gate_decision(
        approval_id=None,
        policy_id=str(policy["id"] if policy else "missing"),
        policy_version=int(policy["version"] if policy else 0),
        input_snapshot={"draft_id": saved["id"], "category": cat, "action": CS_ACTION},
        decision="auto_allow",
        reason=decision.get("reason") or f"白名单类目 {cat} 免审。",
    )
    out = {
        "draft": saved,
        "approval": None,
        "inbound": inbound,
        "gate": {**decision, "decision": "auto_allow", "require_approval": False},
        "whitelist": True,
        "note": "白名单类目免审。可 POST …/cs/drafts/{id}/send 本地 stub 发送（不外呼）。",
    }
    if auto_send:
        sent = send_cs_draft(store, saved["id"], env=env)
        out["send"] = sent
        out["draft"] = sent["draft"]
        out["note"] = "白名单类目已自动 stub 发送（sent_stub / dry_run，不外呼平台）。"
    return out


def send_cs_draft(
    store: WorkflowStore,
    draft_id: str,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """发送 stub：白名单直接落本地；非白名单须八态 approved。不外呼平台。"""
    did = str(draft_id or "").strip()
    if not did:
        raise DraftError("draft_id 不能为空。")
    draft = store.get_cs_draft(did)
    if draft is None:
        raise DraftError("客服草稿不存在。请先 POST /api/workflow/cs/draft。")
    if draft.get("sent_stub") or draft.get("status") == "sent_stub":
        return {
            "draft": draft,
            "approval": store.get_approval(draft["approval_id"]) if draft.get("approval_id") else None,
            "sent_stub": True,
            "dry_run": True,
            "note": "已 stub 发送过，返回本地结果。未外呼平台。",
        }

    cat = str(draft.get("category") or "")
    whitelist = bool(draft.get("whitelist")) or is_whitelist_category(cat, env)
    if cat in NEVER_AUTO_CATEGORIES:
        whitelist = False

    approval: dict[str, Any] | None = None
    if not whitelist:
        if draft.get("approval_id"):
            approval = store.get_approval(str(draft["approval_id"]))
        if approval is None:
            approval = store.get_approval_for_entity(
                "cs_draft",
                did,
                action_type=CS_ACTION,
                statuses=("approved", "pending", "executing", "executed"),
            )
        if approval is None:
            raise DraftError("未审批，不能发送客服回复。须先人工通过，且没有跳过本次。")
        if approval["status"] == ApprovalStatus.EXECUTED.value:
            return {
                "draft": draft,
                "approval": approval,
                "sent_stub": bool(draft.get("sent_stub")),
                "note": "审批单已 executed。",
            }
        if approval["status"] == ApprovalStatus.PENDING.value:
            raise DraftError("未审批，不能发送客服回复。须先人工通过，且没有跳过本次。")
        if approval["status"] == ApprovalStatus.REJECTED.value:
            raise DraftError("已拒绝，不能发送客服回复。没有跳过本次。")
        if approval["status"] == ApprovalStatus.EXPIRED.value:
            raise DraftError("审批已过期，不能发送客服回复。请重新生成草稿并再审。")
        if approval["status"] not in {
            ApprovalStatus.APPROVED.value,
            ApprovalStatus.EXECUTING.value,
        }:
            raise DraftError(f"当前审批状态 {approval['status']} 不能发送客服回复。")
        try:
            approval = claim_for_execute(store, approval["id"])
        except ApprovalError as exc:
            text = str(exc)
            if "未审批" in text or "过期" in text:
                raise DraftError(
                    text.replace("不能上架", "不能发送客服回复").replace(
                        "请重新生成并再审", "请重新生成草稿并再审"
                    )
                ) from exc
            raise DraftError(str(exc)) from exc

    payload = {
        "draft_id": did,
        "conversation_id": draft["conversation_id"],
        "order_id": draft.get("order_id"),
        "category": draft["category"],
        "body_th": draft["body_th"],
        "body_en": draft["body_en"],
        "lang": draft.get("lang") or "th",
        "whitelist": whitelist,
    }
    calls_platform = outbound_send_calls_platform(env)
    stub_result = {
        "sent_stub": True,
        "dry_run": True,
        "platform_called": calls_platform,
        "note": "本地 stub 发送。未外呼 TikTok 客服 API。",
    }
    if calls_platform:
        raise DraftError("不会外呼发送客服回复。本仓库没有客服发送端点，未臆造。")
    idem_key = make_idempotency_key("cs_stub", "send_message", did)

    def _send_stub() -> AdapterResult[dict[str, Any]]:
        return AdapterResult.success(dict(stub_result), "cs_stub")

    outcome = run_side_effect(
        store,
        adapter="cs_stub",
        operation="send_message",
        idempotency_key=idem_key,
        request=payload,
        fn=_send_stub,
        draft_id=did,
        action=EXECUTE_ACTION,
    )
    write = outcome.write
    result = dict(stub_result)
    result["idempotent_replay"] = bool(outcome.replayed)
    if not outcome.replayed:
        store.add_cs_message(
            {
                "conversation_id": draft["conversation_id"],
                "order_id": draft.get("order_id"),
                "category": draft["category"],
                "body": draft["body_th"] if (draft.get("lang") or "th") == "th" else draft["body_en"],
                "lang": draft.get("lang") or "th",
                "direction": "outbound",
            }
        )
        if approval is not None:
            apply_approval_event(
                store,
                approval["id"],
                ApprovalEvent.EXECUTE_OK,
                execution_ref=str(write.get("id") or ""),
            )
    saved = store.upsert_cs_draft(
        {
            "id": did,
            "conversation_id": draft["conversation_id"],
            "inbound_message_id": draft["inbound_message_id"],
            "order_id": draft.get("order_id"),
            "category": draft["category"],
            "lang": draft.get("lang") or "th",
            "body_th": draft["body_th"],
            "body_en": draft["body_en"],
            "status": "sent_stub",
            "whitelist": whitelist,
            "approval_id": approval["id"] if approval else draft.get("approval_id"),
            "sent_stub": True,
            "result": {**result, "external_write_id": write.get("id")},
        }
    )
    return {
        "draft": saved,
        "approval": store.get_approval(approval["id"]) if approval else None,
        "external_write": write,
        "sent_stub": True,
        "dry_run": True,
        "note": result["note"],
    }


def _ensure_cs_approval(
    store: WorkflowStore,
    draft_id: str,
    draft: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    existing = store.get_approval_for_entity(
        "cs_draft",
        draft_id,
        action_type=CS_ACTION,
        statuses=(
            "pending",
            "approved",
            "executing",
            "executed",
            "stale",
            "execute_failed",
        ),
    )
    payload = {
        "draft_id": draft_id,
        "conversation_id": draft["conversation_id"],
        "category": draft["category"],
        "order_id": draft.get("order_id"),
        "body_th": draft["body_th"],
        "body_en": draft["body_en"],
    }
    if existing and existing["status"] == "pending":
        return store.update_approval_payload(existing["id"], payload) or existing
    if existing and existing["status"] in {"approved", "executing", "executed"}:
        return existing
    if existing and existing["status"] in {"stale", "execute_failed"}:
        out = apply_event_resubmit(store, existing)
        if out is not None:
            return out

    policy = store.get_gate_policy(CS_ACTION)
    store.add_gate_decision(
        approval_id=None,
        policy_id=str(policy["id"] if policy else "missing"),
        policy_version=int(policy["version"] if policy else 0),
        input_snapshot={"draft_id": draft_id, "category": draft["category"], "action": CS_ACTION},
        decision=decision["decision"],
        reason=decision["reason"],
    )
    now = datetime.now(timezone.utc)
    return store.add_approval(
        {
            "shop_id": draft.get("shop_id"),
            "action_type": CS_ACTION,
            "entity_type": "cs_draft",
            "entity_id": draft_id,
            "payload": payload,
            "payload_hash": payload_hash(payload),
            "status": ApprovalStatus.PENDING.value,
            "channel": "console",
            "policy_id": (policy or {}).get("id"),
            "decide_before": (now + DECIDE_TTL).isoformat(),
            "execute_within_seconds": int(DECIDE_TTL.total_seconds()),
        }
    )


def _render_reply(
    category: str,
    buyer_body: str,
    order: dict[str, Any] | None,
    lang: str,
) -> dict[str, str]:
    """确定性泰/英模板（不强制真 LLM）。"""
    del lang  # 同时产出 th/en；发送时按草稿 lang 选用
    oid = str((order or {}).get("order_id") or (order or {}).get("id") or "")
    status = str((order or {}).get("status") or "")
    order_bit_th = f" ออเดอร์ {oid}" if oid else ""
    order_bit_en = f" (order {oid})" if oid else ""
    status_th = f" สถานะปัจจุบัน: {status}" if status else ""
    status_en = f" Current status: {status}." if status else ""

    templates: dict[str, tuple[str, str]] = {
        "logistics_query": (
            f"สวัสดีค่ะ ได้รับข้อความเรื่องการจัดส่งแล้วค่ะ{order_bit_th}{status_th} "
            "กำลังตรวจสอบเลขพัสดุให้ โปรดรอสักครู่ค่ะ",
            f"Hello, we received your shipping inquiry{order_bit_en}.{status_en} "
            "We are checking the tracking number for you.",
        ),
        "tracking_status": (
            f"สวัสดีค่ะ อัปเดตสถานะพัสดุให้แล้วค่ะ{order_bit_th}{status_th} "
            "หากเลขพัสดุพร้อมจะแจ้งในแชทนี้อีกครั้งค่ะ",
            f"Hello, here is a tracking update{order_bit_en}.{status_en} "
            "We will share the tracking number in this chat when ready.",
        ),
        "sla_reminder": (
            f"สวัสดีค่ะ คำสั่งซื้อ{order_bit_th} อยู่ในช่วงเวลาจัดส่งตามเงื่อนไขร้านค้าค่ะ{status_th} "
            "หากเกินกำหนดจะรีบอัปเดตให้ค่ะ",
            f"Hello, your order{order_bit_en} is within the store shipping SLA.{status_en} "
            "We will update you promptly if it runs late.",
        ),
        "refund": (
            f"สวัสดีค่ะ ได้รับคำขอคืนเงินแล้วค่ะ{order_bit_th} "
            "เรื่องการคืนเงินต้องให้เจ้าหน้าที่ตรวจสอบก่อน ส่งร่างนี้ให้แอดมินอนุมัติค่ะ",
            f"Hello, we received your refund request{order_bit_en}. "
            "Refunds require human review; this draft awaits admin approval.",
        ),
        "compensation": (
            f"สวัสดีค่ะ ได้รับเรื่องค่าชดเชยแล้วค่ะ{order_bit_th} "
            "ต้องรอการอนุมัติจากแอดมินก่อนตอบกลับลูกค้าค่ะ",
            f"Hello, we received your compensation request{order_bit_en}. "
            "Compensation replies require admin approval before send.",
        ),
        "price_match": (
            f"สวัสดีค่ะ ได้รับคำขอจับคู่ราคาแล้วค่ะ{order_bit_th} "
            "การปรับราคาต้องผ่านการอนุมัติ ไม่สามารถตอบอัตโนมัติได้ค่ะ",
            f"Hello, we received your price-match request{order_bit_en}. "
            "Price changes require approval and cannot auto-send.",
        ),
        "complaint": (
            f"สวัสดีค่ะ ขออภัยในความไม่สะดวกค่ะ{order_bit_th} "
            "ทีมงานจะตรวจสอบเรื่องร้องเรียนนี้หลังจากได้รับการอนุมัติค่ะ",
            f"Hello, sorry for the inconvenience{order_bit_en}. "
            "Complaints require human approval before we reply.",
        ),
    }
    th, en = templates.get(
        category,
        (
            f"สวัสดีค่ะ ได้รับข้อความแล้วค่ะ{order_bit_th} ทีมงานจะตรวจสอบและตอบกลับโดยเร็วค่ะ",
            f"Hello, we received your message{order_bit_en}. Our team will review and reply soon.",
        ),
    )
    snippet = buyer_body.strip().replace("\n", " ")[:80]
    if snippet and category in NEVER_AUTO_CATEGORIES:
        th = f"{th}\n(อ้างอิงข้อความลูกค้า: {snippet})"
        en = f"{en}\n(Buyer quote: {snippet})"
    return {"body_th": th, "body_en": en}
