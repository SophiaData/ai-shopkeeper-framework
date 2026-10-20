"""SCHEMA §3.4 审批八态矩阵单测。"""

from __future__ import annotations

import pytest

from shopkeeper.domain.approval_state import (
    MATRIX,
    SAMPLE_PATH_LISTING_OK,
    SAMPLE_PATH_REJECT,
    ApprovalEvent,
    ApprovalStatus,
    apply_event,
    cell_of,
    walk,
)


def test_matrix_covers_all_cells():
    events = list(ApprovalEvent)
    for status in ApprovalStatus:
        assert status in MATRIX
        for event in events:
            assert event in MATRIX[status]
            target = cell_of(status, event)
            assert target is None or isinstance(target, ApprovalStatus)


def test_sample_path_listing_ok_to_executed():
    path = walk(ApprovalStatus.PENDING, SAMPLE_PATH_LISTING_OK)
    assert path[-1] == ApprovalStatus.EXECUTED
    assert path == [
        ApprovalStatus.PENDING,
        ApprovalStatus.APPROVED,
        ApprovalStatus.EXECUTING,
        ApprovalStatus.EXECUTED,
    ]


def test_sample_path_reject():
    path = walk(ApprovalStatus.PENDING, SAMPLE_PATH_REJECT)
    assert path[-1] == ApprovalStatus.REJECTED


def test_execute_fail_and_reaper():
    assert walk(
        ApprovalStatus.PENDING,
        (ApprovalEvent.APPROVE, ApprovalEvent.CLAIM, ApprovalEvent.EXECUTE_FAIL),
    )[-1] == ApprovalStatus.EXECUTE_FAILED
    assert walk(
        ApprovalStatus.PENDING,
        (ApprovalEvent.APPROVE, ApprovalEvent.CLAIM, ApprovalEvent.REAPER),
    )[-1] == ApprovalStatus.EXECUTE_FAILED


def test_stale_and_expire_bypass():
    assert walk(
        ApprovalStatus.PENDING,
        (ApprovalEvent.APPROVE, ApprovalEvent.PREFLIGHT_FAIL),
    )[-1] == ApprovalStatus.STALE
    assert walk(
        ApprovalStatus.PENDING,
        (ApprovalEvent.APPROVE, ApprovalEvent.EXPIRE),
    )[-1] == ApprovalStatus.EXPIRED


def test_illegal_claim_from_pending():
    out = apply_event(ApprovalStatus.PENDING, ApprovalEvent.CLAIM)
    assert out.illegal and not out.applied
    with pytest.raises(ValueError, match="ILLEGAL"):
        walk(ApprovalStatus.PENDING, (ApprovalEvent.CLAIM,))


def test_resubmit_from_failed_and_executed():
    assert walk(
        ApprovalStatus.EXECUTE_FAILED,
        (ApprovalEvent.RESUBMIT, ApprovalEvent.APPROVE),
    )[-1] == ApprovalStatus.APPROVED
    assert walk(
        ApprovalStatus.EXECUTED,
        (ApprovalEvent.RESUBMIT,),
    )[-1] == ApprovalStatus.PENDING
