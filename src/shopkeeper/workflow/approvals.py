"""工作台侧 SCHEMA 八态审批适配（approvals / gate_policies）。

现有草稿 API（approve/reject/publish）薄封装到八态：
- listing 生成且质检通过 → 建 create_listing 审批单（pending）并尝试发飞书卡片
- approve/reject → 审批单 + 草稿投影同步
- publish → 原子 CLAIM(executing) → 外呼 → executed | execute_failed

飞书卡片默认 FEISHU_NOTIFY=0 写 artifacts stub；回调见 workflow/feishu.py。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from shopkeeper.domain.approval_state import ApprovalEvent, ApprovalStatus, apply_event
from shopkeeper.workflow.drafts import DraftError
from shopkeeper.workflow.store import WorkflowStore

DECIDE_TTL = timedelta(days=7)
DEFAULT_EXECUTE_WITHIN = 900  # SCHEMA 默认 15 分钟；工作台 publish 紧随 approve，足够


class ApprovalError(DraftError):
    """审批闸门错误（复用 DraftError 的 HTTP 映射）。"""


def payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def evaluate_gate_policy(
    policy: dict[str, Any] | None,
    *,
    action_type: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """闸门裁决纯函数：是否必须人工审批（DESIGN §3.1 / §6）。"""
    context = context or {}
    if policy is None:
        return {
            "decision": "require_approval",
            "require_approval": True,
            "reason": f"无 gate_policies 覆盖 {action_type}，默认必审。",
        }
    threshold = dict(policy.get("threshold") or {})
    if action_type == "update_price":
        delta = abs(float(context.get("price_delta_pct") or 0))
        limit = float(threshold.get("price_delta_pct") or 5)
        if delta <= limit:
            return {
                "decision": "auto_allow",
                "require_approval": False,
                "reason": f"改价 Δ{delta}% ≤ 阈值 {limit}%，缓冲内免审。",
            }
        return {
            "decision": "require_approval",
            "require_approval": True,
            "reason": f"改价 Δ{delta}% 超阈值 {limit}%，须审批。",
        }
    if action_type == "pricing_override":
        delta = abs(float(context.get("price_deviation_pct") or 0))
        limit = float(threshold.get("price_deviation_pct") or 10)
        if delta <= limit:
            return {
                "decision": "auto_allow",
                "require_approval": False,
                "reason": f"定价偏离 {delta}% ≤ 阈值 {limit}%，免审。",
            }
        return {
            "decision": "require_approval",
            "require_approval": True,
            "reason": f"定价偏离公式 {delta}% 超阈值 {limit}%，须审批。",
        }
    if action_type == "update_stock":
        factor = float(context.get("buffer_factor") or 0)
        max_factor = float(threshold.get("buffer_max") or 1.0)
        if 0 < factor <= max_factor and not policy.get("require_approval"):
            return {
                "decision": "auto_allow",
                "require_approval": False,
                "reason": f"库存推送 buffer_factor={factor} 在阈值内，免审。",
            }
        return {
            "decision": "require_approval",
            "require_approval": True,
            "reason": "库存推送越界或策略要求审批。",
        }
    if action_type == "auto_reply":
        # DESIGN §6：白名单类目免审；其余必审。threshold.whitelist_only 为文档标记。
        if context.get("whitelist"):
            cat = str(context.get("category") or "")
            return {
                "decision": "auto_allow",
                "require_approval": False,
                "reason": f"客服自动回复白名单类目 {cat or '(unset)'} 免审。",
            }
        return {
            "decision": "require_approval",
            "require_approval": True,
            "reason": "客服回复非白名单类目，须人工审批（auto_reply）。",
        }
    if policy.get("require_approval"):
        return {
            "decision": "require_approval",
            "require_approval": True,
            "reason": f"{action_type} 策略要求人工审批。",
        }
    return {
        "decision": "auto_allow",
        "require_approval": False,
        "reason": f"{action_type} 策略免审。",
    }


def ensure_listing_approval(store: WorkflowStore, draft: dict[str, Any]) -> dict[str, Any]:
    """为 listing 草稿确保有一条 pending/可续审审批单。"""
    if draft.get("kind") != "listing":
        raise ApprovalError("只有 listing 走 create_listing 八态审批。")
    existing = store.get_approval_for_entity(
        "draft",
        draft["id"],
        action_type="create_listing",
        statuses=(
            "pending",
            "approved",
            "executing",
            "executed",
            "stale",
            "execute_failed",
        ),
    )
    if existing and existing["status"] == "pending":
        return existing
    if existing and existing["status"] != "pending":
        out = apply_event(ApprovalStatus(existing["status"]), ApprovalEvent.RESUBMIT)
        if out.applied:
            reset = store.transition_approval(
                existing["id"],
                to_status=out.status.value,
                from_statuses=(existing["status"],),
                decision_note="重新进入待审",
                clear_executing=True,
            )
            if reset:
                _notify_feishu_card(store, reset, draft)
                return reset

    policy = store.get_gate_policy("create_listing")
    decision = evaluate_gate_policy(policy, action_type="create_listing")
    store.add_gate_decision(
        approval_id=None,
        policy_id=str(policy["id"] if policy else "missing"),
        policy_version=int(policy["version"] if policy else 0),
        input_snapshot={"draft_id": draft["id"], "kind": draft.get("kind")},
        decision=decision["decision"],
        reason=decision["reason"],
    )
    payload = {
        "draft_id": draft["id"],
        "title": draft.get("title"),
        "listing_version": (draft.get("payload") or {}).get("listing_id"),
        "qa_ok": bool((draft.get("qa") or {}).get("ok")),
    }
    now = datetime.now(timezone.utc)
    # 工作台 listing：审批通过后 7 天内可 publish（兼容原薄闸门 TTL；SCHEMA 默认 900s 留给紧耦合执行器）
    # channel：控制台 + 飞书卡片双通道（卡片默认 stub）
    created = store.add_approval(
        {
            "shop_id": draft.get("shop_id"),
            "action_type": "create_listing",
            "entity_type": "draft",
            "entity_id": draft["id"],
            "payload": payload,
            "payload_hash": payload_hash(payload),
            "status": ApprovalStatus.PENDING.value,
            "channel": "feishu",
            "policy_id": (policy or {}).get("id"),
            "decide_before": (now + DECIDE_TTL).isoformat(),
            "execute_within_seconds": int(DECIDE_TTL.total_seconds()),
        }
    )
    _notify_feishu_card(store, created, draft)
    return created


def _notify_feishu_card(store: WorkflowStore, approval: dict[str, Any], draft: dict[str, Any]) -> None:
    """发飞书卡片；缺配置 / 关闭通知时写 stub，绝不抛出。"""
    try:
        from shopkeeper.workflow.feishu import send_approval_card

        send_approval_card(store, approval, draft=draft)
    except Exception:  # noqa: BLE001
        return


def apply_approval_event(
    store: WorkflowStore,
    approval_id: str,
    event: ApprovalEvent,
    *,
    decided_by: str = "console",
    note: str = "",
    execution_ref: str | None = None,
    execution_error: str | None = None,
    preflight_result: str | None = None,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    approval = store.get_approval(approval_id)
    if approval is None:
        raise ApprovalError("审批单不存在。")
    expected = str(expected_hash or "").strip()
    if expected and str(approval.get("payload_hash") or "") != expected:
        raise ApprovalError("审批内容已变化，请刷新后重新核对。")
    current = ApprovalStatus(approval["status"])
    outcome = apply_event(current, event)
    if not outcome.applied:
        raise ApprovalError(f"审批状态不允许：{outcome.reason}")

    mark_decided = event in {ApprovalEvent.APPROVE, ApprovalEvent.REJECT}
    mark_executing = event == ApprovalEvent.CLAIM
    mark_executed = event == ApprovalEvent.EXECUTE_OK
    clear_executing = event in {
        ApprovalEvent.EXECUTE_OK,
        ApprovalEvent.EXECUTE_FAIL,
        ApprovalEvent.REAPER,
        ApprovalEvent.PREFLIGHT_FAIL,
        ApprovalEvent.RESUBMIT,
    }
    saved = store.transition_approval(
        approval_id,
        to_status=outcome.status.value,
        from_statuses=(current.value,),
        decided_by=decided_by if mark_decided else None,
        decision_note=note if note or mark_decided else None,
        execution_ref=execution_ref,
        execution_error=execution_error if execution_error is not None else None,
        preflight_result=preflight_result,
        mark_decided=mark_decided,
        mark_executing=mark_executing,
        mark_executed=mark_executed,
        clear_executing=clear_executing and not mark_executing,
    )
    if saved is None:
        raise ApprovalError("审批状态已变，不能重复决定。")
    return saved


#: 判定 `executing` 卡死的宽限期（秒）。
#:
#: 取 `DEFAULT_EXECUTE_WITHIN` 之上再加一段，避免把「正在执行、只是慢」的单子误判 ——
#: 误判会把一张真的在跑的单子标成失败，反过来制造重复执行的风险。
STALE_EXECUTING_GRACE_SECONDS = 900


def reap_stale_executing(
    store: WorkflowStore,
    *,
    now: datetime | None = None,
    grace_seconds: int = STALE_EXECUTING_GRACE_SECONDS,
) -> list[str]:
    """把卡死在 `executing` 的审批单收回成 `execute_failed`，返回被收回的 id。

    ## 为什么需要它

    `executing` 只能通过 `EXECUTE_OK` / `EXECUTE_FAIL` / `REAPER` / `RESUBMIT` 离开。
    但**没有任何地方触发过 `REAPER`** —— 它只存在于状态机表里。
    于是「执行到一半进程死了 / 抛了异常」的单子会**永久停在 `executing`**：

    - `claim_for_execute` 要求状态是 `approved` → 永远拒绝
    - 用户看到的是「未审批，不能上架」，而那张单子明明已经批过了
    - 重新生成审批单也不一定能救（草稿可能同时卡在 `publishing`）

    实测踩到过：`input.quantity` 非整数导致异常逃逸，
    草稿卡 `publishing`、审批单卡 `executing`、审计一行不留。

    ## 为什么是「宽限 + 收回」而不是「自动重试」

    执行到一半的单子**可能已经在平台上生效了**（例如商品其实建出来了）。
    自动重试会**重复上架**。所以这里只做一件事：**把单子从死状态里放出来**，
    标成 `execute_failed` 交由人工核对与重新审批。

    ⚠️ 注意 `REAPER` 的目标状态是 `EXECUTE_FAILED` 而**不是** `APPROVED` ——
    不会自动回到「可执行」，因此不会绕过人工闸门。
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(seconds=int(grace_seconds))
    reaped: list[str] = []
    for approval in store.list_approvals(status=ApprovalStatus.EXECUTING.value):
        since_raw = str(approval.get("executing_since") or approval.get("decided_at") or "")
        since = _parse_dt(since_raw) if since_raw else None
        if since is None or since > cutoff:
            continue
        try:
            apply_approval_event(
                store,
                str(approval["id"]),
                ApprovalEvent.REAPER,
                decided_by="reaper",
                note=f"执行超过 {grace_seconds} 秒未回执，已收回为失败待人工核对。",
                execution_error="stale executing reclaimed by reaper",
            )
        except ApprovalError:
            # 状态在扫描与收回之间变了（正常竞态）—— 跳过，不视为错误。
            continue
        # ⚠️ **必须一并放回草稿**。审批单与草稿是两行状态：
        # 只把审批单从 executing 收回，草稿会**仍停在 publishing** ——
        # 下一次发布依然报「未审批，不能上架」，操作者还是卡着。
        # `EXECUTE_FAILED` 的投影规则正是 `publishing → approved`（见 sync_draft_projection）。
        # 草稿类审批的 `entity_type` 是 `"draft"`（不是 `"listing"` —— 后者是 action_type）。
        # 写成 `"listing"` 会让投影永远不触发，等于没修。
        entity_type = str(approval.get("entity_type") or "")
        if entity_type == "draft":
            try:
                sync_draft_projection(
                    store,
                    str(approval.get("entity_id") or ""),
                    ApprovalStatus.EXECUTE_FAILED.value,
                    note="reaper 收回卡死的执行",
                )
            except Exception:  # noqa: BLE001 - 投影失败不该让整批收回中断
                pass
        reaped.append(str(approval["id"]))
    return reaped


def claim_for_execute(store: WorkflowStore, approval_id: str) -> dict[str, Any]:
    """原子消费：approved → executing；校验 decide_before 与执行窗口。"""
    approval = store.get_approval(approval_id)
    if approval is None:
        raise ApprovalError("审批单不存在。")
    if approval["status"] != ApprovalStatus.APPROVED.value:
        raise ApprovalError("未审批，不能上架。须先人工通过，且没有跳过本次。")
    now = datetime.now(timezone.utc)
    if _parse_dt(approval["decide_before"]) <= now:
        apply_approval_event(store, approval_id, ApprovalEvent.EXPIRE, note="审批超时未决定/未执行")
        raise ApprovalError("审批已过期，不能上架。请重新生成并再审。")
    decided_at = _parse_dt(approval.get("decided_at") or "")
    window = int(approval.get("execute_within_seconds") or DEFAULT_EXECUTE_WITHIN)
    if decided_at and (now - decided_at).total_seconds() > window:
        apply_approval_event(store, approval_id, ApprovalEvent.EXPIRE, note="执行窗口已过")
        raise ApprovalError("审批已过期，不能上架。请重新生成并再审。")
    return apply_approval_event(store, approval_id, ApprovalEvent.CLAIM, note=approval.get("decision_note") or "")


def _parse_dt(raw: str) -> datetime:
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def sync_draft_projection(store: WorkflowStore, draft_id: str, approval_status: str, note: str = "") -> dict[str, Any] | None:
    """把八态投影回草稿队列字段，保持 /api/workflow/drafts 兼容。"""
    mapping = {
        ApprovalStatus.PENDING.value: ("pending", ("needs_revision", "pending", "approved", "rejected", "expired")),
        ApprovalStatus.APPROVED.value: ("approved", ("pending",)),
        ApprovalStatus.REJECTED.value: ("rejected", ("pending", "needs_revision")),
        ApprovalStatus.EXPIRED.value: ("expired", ("pending", "approved")),
        ApprovalStatus.EXECUTING.value: ("publishing", ("approved",)),
        ApprovalStatus.EXECUTED.value: None,  # mark_published 负责
        ApprovalStatus.EXECUTE_FAILED.value: ("approved", ("publishing",)),
        ApprovalStatus.STALE.value: ("pending", ("approved", "publishing")),
    }
    spec = mapping.get(approval_status)
    if spec is None:
        return store.get_draft(draft_id)
    target, allowed = spec
    return store.decide(draft_id, target, note, allowed_from=allowed)
