"""有副作用写操作的幂等审计（工作台 ``external_writes`` 薄切片）。

口径对齐 SCHEMA §3.4 / §4.1：先按 ``idempotency_key`` 占位再外呼；
同键重入若已 ``confirmed``/``dry_run`` 则直接回放，不二次执行。
执行类失败**不**经 ``retry.call_with_retry`` 自动重试（DESIGN §3.3）。

本模块不解锁 ``TTS_WRITE_*``；真实平台写仍受既有闸门约束。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult
from shopkeeper.workflow.store import WorkflowStore

# 终态：可安全回放
_DONE_STATUSES = frozenset({"confirmed", "dry_run", "failed"})
_SUCCESS_STATUSES = frozenset({"confirmed", "dry_run"})


@dataclass(frozen=True)
class SideEffectOutcome:
    """幂等副作用执行结果。"""

    write: dict[str, Any]
    replayed: bool
    result: AdapterResult[dict[str, Any]] | None = None


def make_idempotency_key(adapter: str, operation: str, *parts: str) -> str:
    """构造稳定幂等键：``adapter:operation:part…``（空段丢弃）。"""
    chunks = [str(adapter).strip(), str(operation).strip()]
    for part in parts:
        text = str(part or "").strip()
        if text:
            chunks.append(text)
    return ":".join(chunks)


def run_side_effect(
    store: WorkflowStore,
    *,
    adapter: str,
    operation: str,
    idempotency_key: str,
    request: dict[str, Any],
    fn: Callable[[], AdapterResult[dict[str, Any]]],
    draft_id: str | None = None,
    action: str | None = None,
) -> SideEffectOutcome:
    """占位 → 执行 → 回写；同键已成功则回放。

    ``fn`` 只应被调用一次（占位成功且非回放时）。并发占位失败且对方仍
    ``pending`` → 返回 ``EXT_IDEMPOTENCY_CONFLICT``，不执行 ``fn``。
    """
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("idempotency_key 不能为空")
    op_action = action or operation

    existing = store.get_external_write_by_idempotency(key)
    if existing is not None:
        status = str(existing.get("status") or "")
        if status in _SUCCESS_STATUSES:
            return SideEffectOutcome(
                write=existing,
                replayed=True,
                result=_result_from_write(existing, adapter=adapter),
            )
        if status == "failed":
            # 失败不自动重试；调用方若要人工重试须换 attempt / 新键
            return SideEffectOutcome(
                write=existing,
                replayed=True,
                result=_result_from_write(existing, adapter=adapter),
            )
        if status == "pending":
            return SideEffectOutcome(
                write=existing,
                replayed=True,
                result=AdapterResult.failure(
                    AdapterError.execution(
                        "EXT_IDEMPOTENCY_CONFLICT",
                        f"同幂等键进行中：{key}",
                        idempotency_key=key,
                    ),
                    adapter,
                ),
            )

    claimed = store.claim_external_write(
        action=op_action,
        draft_id=draft_id,
        request=request,
        idempotency_key=key,
        adapter=adapter,
        operation=operation,
    )
    if claimed is None:
        again = store.get_external_write_by_idempotency(key)
        if again is not None and str(again.get("status") or "") in _DONE_STATUSES:
            return SideEffectOutcome(
                write=again,
                replayed=True,
                result=_result_from_write(again, adapter=adapter),
            )
        conflict = again or {
            "id": "",
            "action": op_action,
            "draft_id": draft_id,
            "status": "pending",
            "request": request,
            "response": {},
            "platform_product_id": None,
            "idempotency_key": key,
            "adapter": adapter,
            "operation": operation,
        }
        return SideEffectOutcome(
            write=conflict,
            replayed=True,
            result=AdapterResult.failure(
                AdapterError.execution(
                    "EXT_IDEMPOTENCY_CONFLICT",
                    f"同幂等键并发占位失败：{key}",
                    idempotency_key=key,
                ),
                adapter,
            ),
        )

    result = fn()
    if result.ok and result.data is not None:
        data = dict(result.data)
        dry = bool(data.get("dry_run"))
        status = "dry_run" if dry else "confirmed"
        pid = data.get("platform_product_id")
        write = store.complete_external_write(
            str(claimed["id"]),
            status=status,
            response=data,
            platform_product_id=str(pid) if pid else None,
        )
        return SideEffectOutcome(write=write or claimed, replayed=False, result=result)

    err = result.error or AdapterError.execution(
        "APPROVAL_EXECUTE_FAILED",
        "副作用执行失败且无错误体",
    )
    write = store.complete_external_write(
        str(claimed["id"]),
        status="failed",
        response=err.model_dump(),
        platform_product_id=None,
    )
    return SideEffectOutcome(write=write or claimed, replayed=False, result=result)


def _result_from_write(
    write: dict[str, Any],
    *,
    adapter: str,
) -> AdapterResult[dict[str, Any]]:
    status = str(write.get("status") or "")
    response = write.get("response") or {}
    if status in _SUCCESS_STATUSES:
        data = dict(response) if isinstance(response, dict) else {"raw": response}
        data.setdefault("idempotent_replay", True)
        return AdapterResult.success(data, adapter)
    if isinstance(response, dict) and response.get("code"):
        kind = str(response.get("kind") or "execution")
        retryable = bool(response.get("retryable", False))
        human_todo = bool(response.get("human_todo", True))
        from shopkeeper.errors import ErrorKind

        try:
            ek = ErrorKind(kind)
        except ValueError:
            ek = ErrorKind.EXECUTION
        return AdapterResult.failure(
            AdapterError(
                code=str(response.get("code")),
                kind=ek,
                message=str(response.get("message") or "replayed failure"),
                retryable=retryable,
                human_todo=human_todo,
                detail=response.get("detail"),
            ),
            adapter,
        )
    return AdapterResult.failure(
        AdapterError.execution(
            "APPROVAL_EXECUTE_FAILED",
            "幂等回放：先前执行失败",
            idempotency_key=str(write.get("idempotency_key") or ""),
        ),
        adapter,
    )
