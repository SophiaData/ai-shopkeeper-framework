"""审批八态状态机（权威=SCHEMA §3.4 / DESIGN §4）。

八态：pending|approved|executing|executed|execute_failed|stale|rejected|expired

主路径：pending → approved → executing → executed | execute_failed
旁路：rejected / expired / stale
原子消费：approved → executing（仅此合法抢位）；executing 由 reaper 兜底 → execute_failed
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    EXECUTED = "executed"
    EXECUTE_FAILED = "execute_failed"
    STALE = "stale"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalEvent(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EXPIRE = "expire"
    CLAIM = "claim"  # 原子消费 approved → executing
    PREFLIGHT_FAIL = "preflight_fail"
    EXECUTE_OK = "execute_ok"
    EXECUTE_FAIL = "execute_fail"
    REAPER = "reaper"
    RESUBMIT = "resubmit"  # stale / execute_failed → pending 重新走审


# 目标态；None = ILLEGAL
MATRIX: dict[ApprovalStatus, dict[ApprovalEvent, ApprovalStatus | None]] = {
    ApprovalStatus.PENDING: {
        ApprovalEvent.APPROVE: ApprovalStatus.APPROVED,
        ApprovalEvent.REJECT: ApprovalStatus.REJECTED,
        ApprovalEvent.EXPIRE: ApprovalStatus.EXPIRED,
        ApprovalEvent.CLAIM: None,
        ApprovalEvent.PREFLIGHT_FAIL: None,
        ApprovalEvent.EXECUTE_OK: None,
        ApprovalEvent.EXECUTE_FAIL: None,
        ApprovalEvent.REAPER: None,
        ApprovalEvent.RESUBMIT: None,
    },
    ApprovalStatus.APPROVED: {
        ApprovalEvent.APPROVE: None,
        ApprovalEvent.REJECT: None,
        ApprovalEvent.EXPIRE: ApprovalStatus.EXPIRED,
        ApprovalEvent.CLAIM: ApprovalStatus.EXECUTING,
        ApprovalEvent.PREFLIGHT_FAIL: ApprovalStatus.STALE,
        ApprovalEvent.EXECUTE_OK: None,
        ApprovalEvent.EXECUTE_FAIL: None,
        ApprovalEvent.REAPER: None,
        # 本地再编辑：拉回待审（商品库编辑 / 载荷变更）
        ApprovalEvent.RESUBMIT: ApprovalStatus.PENDING,
    },
    ApprovalStatus.EXECUTING: {
        ApprovalEvent.APPROVE: None,
        ApprovalEvent.REJECT: None,
        ApprovalEvent.EXPIRE: None,
        ApprovalEvent.CLAIM: None,
        ApprovalEvent.PREFLIGHT_FAIL: ApprovalStatus.STALE,
        ApprovalEvent.EXECUTE_OK: ApprovalStatus.EXECUTED,
        ApprovalEvent.EXECUTE_FAIL: ApprovalStatus.EXECUTE_FAILED,
        ApprovalEvent.REAPER: ApprovalStatus.EXECUTE_FAILED,
        ApprovalEvent.RESUBMIT: ApprovalStatus.PENDING,
    },
    ApprovalStatus.EXECUTED: {
        ApprovalEvent.APPROVE: None,
        ApprovalEvent.REJECT: None,
        ApprovalEvent.EXPIRE: None,
        ApprovalEvent.CLAIM: None,
        ApprovalEvent.PREFLIGHT_FAIL: None,
        ApprovalEvent.EXECUTE_OK: None,
        ApprovalEvent.EXECUTE_FAIL: None,
        ApprovalEvent.REAPER: None,
        # 已执行后再编辑：新开待审（同单 RESUBMIT，审计靠 decision_note / 新 payload）
        ApprovalEvent.RESUBMIT: ApprovalStatus.PENDING,
    },
    ApprovalStatus.EXECUTE_FAILED: {
        ApprovalEvent.APPROVE: None,
        ApprovalEvent.REJECT: None,
        ApprovalEvent.EXPIRE: None,
        ApprovalEvent.CLAIM: None,
        ApprovalEvent.PREFLIGHT_FAIL: None,
        ApprovalEvent.EXECUTE_OK: None,
        ApprovalEvent.EXECUTE_FAIL: None,
        ApprovalEvent.REAPER: None,
        ApprovalEvent.RESUBMIT: ApprovalStatus.PENDING,
    },
    ApprovalStatus.STALE: {
        ApprovalEvent.APPROVE: None,
        ApprovalEvent.REJECT: None,
        ApprovalEvent.EXPIRE: ApprovalStatus.EXPIRED,
        ApprovalEvent.CLAIM: None,
        ApprovalEvent.PREFLIGHT_FAIL: None,
        ApprovalEvent.EXECUTE_OK: None,
        ApprovalEvent.EXECUTE_FAIL: None,
        ApprovalEvent.REAPER: None,
        ApprovalEvent.RESUBMIT: ApprovalStatus.PENDING,
    },
    ApprovalStatus.REJECTED: {
        ApprovalEvent.APPROVE: None,
        ApprovalEvent.REJECT: None,
        ApprovalEvent.EXPIRE: None,
        ApprovalEvent.CLAIM: None,
        ApprovalEvent.PREFLIGHT_FAIL: None,
        ApprovalEvent.EXECUTE_OK: None,
        ApprovalEvent.EXECUTE_FAIL: None,
        ApprovalEvent.REAPER: None,
        ApprovalEvent.RESUBMIT: ApprovalStatus.PENDING,
    },
    ApprovalStatus.EXPIRED: {
        ApprovalEvent.APPROVE: None,
        ApprovalEvent.REJECT: None,
        ApprovalEvent.EXPIRE: None,
        ApprovalEvent.CLAIM: None,
        ApprovalEvent.PREFLIGHT_FAIL: None,
        ApprovalEvent.EXECUTE_OK: None,
        ApprovalEvent.EXECUTE_FAIL: None,
        ApprovalEvent.REAPER: None,
        ApprovalEvent.RESUBMIT: ApprovalStatus.PENDING,
    },
}

TERMINAL: frozenset[ApprovalStatus] = frozenset(
    {
        ApprovalStatus.EXECUTED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXPIRED,
    }
)

# DESIGN §4 / SCHEMA 样例主路径
SAMPLE_PATH_LISTING_OK: tuple[ApprovalEvent, ...] = (
    ApprovalEvent.APPROVE,
    ApprovalEvent.CLAIM,
    ApprovalEvent.EXECUTE_OK,
)
SAMPLE_PATH_REJECT: tuple[ApprovalEvent, ...] = (ApprovalEvent.REJECT,)


@dataclass(frozen=True)
class TransitionOutcome:
    status: ApprovalStatus
    applied: bool
    illegal: bool = False
    reason: str = ""


def cell_of(status: ApprovalStatus, event: ApprovalEvent) -> ApprovalStatus | None:
    return MATRIX[status][event]


def apply_event(status: ApprovalStatus, event: ApprovalEvent) -> TransitionOutcome:
    """单格裁决。ILLEGAL → 不迁移、illegal=True（调用方映射为业务错误）。"""
    target = cell_of(status, event)
    if target is None:
        return TransitionOutcome(
            status=status,
            applied=False,
            illegal=True,
            reason=f"ILLEGAL: {status}+{event}",
        )
    return TransitionOutcome(status=target, applied=True, reason=f"{status}→{target} via {event}")


def walk(start: ApprovalStatus, events: tuple[ApprovalEvent, ...] | list[ApprovalEvent]) -> list[ApprovalStatus]:
    """沿事件序列走状态；任一步非法则抛 ValueError。"""
    cur = start
    path = [cur]
    for event in events:
        out = apply_event(cur, event)
        if not out.applied:
            raise ValueError(out.reason)
        cur = out.status
        path.append(cur)
    return path
