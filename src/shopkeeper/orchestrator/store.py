"""编排器 run / step / LLM 用量持久化（工作台 SQLite）。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from shopkeeper.orchestrator.budget import budget_local_date
from shopkeeper.orchestrator.definition import STEP_LABELS, FlowDefinition
from shopkeeper.workflow.shops import DEFAULT_SHOP_ID, normalize_shop_id
from shopkeeper.workflow.store import WorkflowStore

RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_AWAITING_APPROVAL = "awaiting_approval"
RUN_SUSPENDED = "suspended"
RUN_BUDGET_PAUSED = "budget_paused"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_AWAITING_APPROVAL = "awaiting_approval"
STEP_SUSPENDED = "suspended"

_TERMINAL_RUNS = frozenset({RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(obj: Any) -> str:
    return json.dumps(obj if obj is not None else {}, ensure_ascii=False)


def _loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return {} if default is None else default
    return json.loads(raw)


class OrchestratorStore:
    """薄封装：读写 orch_* 表，复用 WorkflowStore 连接与锁。"""

    def __init__(self, workflow: WorkflowStore) -> None:
        self.workflow = workflow

    def create_run(
        self,
        flow: FlowDefinition,
        input_payload: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        shop_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """创建 run。

        ``run_id`` 可传入确定性 id，用于订单履约 flow 的跨进程幂等启动：
        同一个 (flow, shop, order) 派生出同一个主键，第二个进程 INSERT 会撞主键。
        """
        run_id = str(run_id or uuid.uuid4().hex)
        created = datetime.now(timezone.utc)
        now = created.isoformat()
        ctx = dict(context or {})
        ctx.setdefault("rewrite_round", 0)
        sid = normalize_shop_id(shop_id)
        with self.workflow._lock:
            with self.workflow._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO orch_runs (
                        id, flow_id, status, input_json, context_json,
                        current_step, error_json, created_at, updated_at, finished_at, shop_id
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, ?)
                    """,
                    (run_id, flow.id, RUN_PENDING, _dumps(input_payload), _dumps(ctx), now, now, sid),
                )
                for index, step in enumerate(flow.steps):
                    # 同一微秒会让时间线退化为 step_id 字母序；按声明顺序错开。
                    stamp = (created + timedelta(microseconds=index)).isoformat()
                    conn.execute(
                        """
                        INSERT INTO orch_steps (
                            id, run_id, step_id, status, attempt, rewrite_round,
                            input_json, output_json, ai_trace_json, error_json,
                            started_at, finished_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 0, 0, '{}', '{}', '{}', NULL, NULL, NULL, ?, ?)
                        """,
                        (uuid.uuid4().hex, run_id, step.id, STEP_PENDING, stamp, stamp),
                    )
        return self.get_run(run_id)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.workflow._connect() as conn:
            row = conn.execute("SELECT * FROM orch_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            return self._run_view(row)

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        shop_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        clauses: list[str] = []
        params: list[Any] = []
        if shop_id is not None:
            clauses.append("shop_id = ?")
            params.append(normalize_shop_id(shop_id))
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.workflow._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM orch_runs {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [self._run_view(r) for r in rows]

    def get_steps(self, run_id: str) -> list[dict[str, Any]]:
        with self.workflow._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orch_steps WHERE run_id = ? ORDER BY created_at ASC, step_id ASC",
                (run_id,),
            ).fetchall()
            return [self._step_view(r) for r in rows]

    def get_step(self, run_id: str, step_id: str) -> dict[str, Any] | None:
        with self.workflow._connect() as conn:
            row = conn.execute(
                "SELECT * FROM orch_steps WHERE run_id = ? AND step_id = ?",
                (run_id, step_id),
            ).fetchone()
            if row is None:
                return None
            return self._step_view(row)

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        context: dict[str, Any] | None = None,
        current_step: str | None = ...,  # type: ignore[assignment]
        error: dict[str, Any] | None = ...,  # type: ignore[assignment]
        finished: bool = False,
    ) -> dict[str, Any]:
        now = _now()
        with self.workflow._lock:
            with self.workflow._connect() as conn:
                row = conn.execute("SELECT * FROM orch_runs WHERE id = ?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(run_id)
                new_status = status if status is not None else row["status"]
                new_ctx = _dumps(context) if context is not None else row["context_json"]
                if current_step is ...:
                    new_current = row["current_step"]
                else:
                    new_current = current_step
                if error is ...:
                    new_err = row["error_json"]
                else:
                    new_err = None if error is None else _dumps(error)
                finished_at = row["finished_at"]
                if finished or new_status in _TERMINAL_RUNS:
                    finished_at = finished_at or now
                conn.execute(
                    """
                    UPDATE orch_runs SET
                        status = ?, context_json = ?, current_step = ?,
                        error_json = ?, updated_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (new_status, new_ctx, new_current, new_err, now, finished_at, run_id),
                )
        return self.get_run(run_id)  # type: ignore[return-value]

    def update_step(
        self,
        run_id: str,
        step_id: str,
        *,
        status: str | None = None,
        attempt: int | None = None,
        rewrite_round: int | None = None,
        input_payload: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        ai_trace: dict[str, Any] | list[Any] | None = None,
        error: dict[str, Any] | None = ...,  # type: ignore[assignment]
        mark_started: bool = False,
        mark_finished: bool = False,
    ) -> dict[str, Any]:
        now = _now()
        with self.workflow._lock:
            with self.workflow._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM orch_steps WHERE run_id = ? AND step_id = ?",
                    (run_id, step_id),
                ).fetchone()
                if row is None:
                    raise KeyError(f"{run_id}/{step_id}")
                new_status = status if status is not None else row["status"]
                new_attempt = attempt if attempt is not None else int(row["attempt"])
                new_round = rewrite_round if rewrite_round is not None else int(row["rewrite_round"])
                new_in = _dumps(input_payload) if input_payload is not None else row["input_json"]
                new_out = _dumps(output) if output is not None else row["output_json"]
                new_trace = _dumps(ai_trace) if ai_trace is not None else row["ai_trace_json"]
                if error is ...:
                    new_err = row["error_json"]
                else:
                    new_err = None if error is None else _dumps(error)
                started = row["started_at"] or (now if mark_started else None)
                finished = row["finished_at"]
                if mark_finished:
                    finished = now
                elif status in {STEP_PENDING, STEP_RUNNING} and mark_started:
                    finished = None
                conn.execute(
                    """
                    UPDATE orch_steps SET
                        status = ?, attempt = ?, rewrite_round = ?,
                        input_json = ?, output_json = ?, ai_trace_json = ?,
                        error_json = ?, started_at = ?, finished_at = ?, updated_at = ?
                    WHERE run_id = ? AND step_id = ?
                    """,
                    (
                        new_status,
                        new_attempt,
                        new_round,
                        new_in,
                        new_out,
                        new_trace,
                        new_err,
                        started,
                        finished,
                        now,
                        run_id,
                        step_id,
                    ),
                )
        return self.get_step(run_id, step_id)  # type: ignore[return-value]

    def reset_step_for_rewrite(self, run_id: str, step_id: str, *, rewrite_round: int) -> None:
        """QA 打回：步骤回到 pending，清空产出，保留 attempt 历史在 ai_trace。"""
        step = self.get_step(run_id, step_id)
        if step is None:
            return
        traces = step.get("ai_trace") or []
        if isinstance(traces, dict):
            traces = [traces] if traces else []
        traces = list(traces)
        traces.append(
            {
                "event": "reset_for_rewrite",
                "from_status": step["status"],
                "previous_output": step.get("output") or {},
                "at": _now(),
            }
        )
        self.update_step(
            run_id,
            step_id,
            status=STEP_PENDING,
            rewrite_round=rewrite_round,
            output={},
            ai_trace=traces,
            error=None,
            mark_finished=False,
        )
        with self.workflow._lock:
            with self.workflow._connect() as conn:
                conn.execute(
                    "UPDATE orch_steps SET finished_at = NULL, updated_at = ? WHERE run_id = ? AND step_id = ?",
                    (_now(), run_id, step_id),
                )

    def recover_stale_running_steps(self, run_id: str) -> list[str]:
        """进程崩溃后的持久化边界：DB 中残留的 step ``running`` 视为未完成，收回为 ``pending``。

        权威状态只在 ``orch_steps`` 行；handler 必须可幂等重入（见 steps 模块约定）。
        返回被回收的 step_id 列表。
        """
        recovered: list[str] = []
        for step in self.get_steps(run_id):
            if step["status"] != STEP_RUNNING:
                continue
            traces = step.get("ai_trace") or []
            if isinstance(traces, dict):
                traces = [traces] if traces else []
            traces = list(traces)
            traces.append(
                {
                    "event": "recovered_from_stale_running",
                    "from_status": STEP_RUNNING,
                    "previous_attempt": int(step.get("attempt") or 0),
                    "at": _now(),
                }
            )
            self.update_step(
                run_id,
                step["step_id"],
                status=STEP_PENDING,
                ai_trace=traces,
                error=None,
                mark_finished=False,
            )
            with self.workflow._lock:
                with self.workflow._connect() as conn:
                    conn.execute(
                        "UPDATE orch_steps SET finished_at = NULL, updated_at = ? WHERE run_id = ? AND step_id = ?",
                        (_now(), run_id, step["step_id"]),
                    )
            recovered.append(step["step_id"])
        return recovered

    def record_llm_usage(
        self,
        *,
        run_id: str,
        step_id: str,
        model: str,
        prompt_version: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        rationale: str = "",
        local_date: str | None = None,
    ) -> dict[str, Any]:
        row_id = uuid.uuid4().hex
        now = _now()
        day = local_date or budget_local_date()
        with self.workflow._lock:
            with self.workflow._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO orch_llm_usage (
                        id, run_id, step_id, local_date, model, prompt_version,
                        prompt_tokens, completion_tokens, cost_usd, rationale, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        run_id,
                        step_id,
                        day,
                        model,
                        prompt_version,
                        int(prompt_tokens),
                        int(completion_tokens),
                        float(cost_usd or 0.0),
                        rationale or "",
                        now,
                    ),
                )
        return {
            "id": row_id,
            "run_id": run_id,
            "step_id": step_id,
            "local_date": day,
            "cost_usd": float(cost_usd or 0.0),
        }

    def spent_usd_today(self, *, local_date: str | None = None) -> float:
        day = local_date or budget_local_date()
        with self.workflow._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM orch_llm_usage WHERE local_date = ?",
                (day,),
            ).fetchone()
            return float(row["total"] if row is not None else 0.0)

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        steps = self.get_steps(run_id)
        enriched: list[dict[str, Any]] = []
        for step in steps:
            traces = step.get("ai_trace") or []
            if isinstance(traces, dict):
                traces = [traces] if traces else []
            cost = 0.0
            rationales: list[str] = []
            for t in traces:
                if isinstance(t, dict):
                    try:
                        cost += float(t.get("cost_usd") or 0)
                    except (TypeError, ValueError):
                        pass
                    if t.get("rationale"):
                        rationales.append(str(t["rationale"]))
            row = dict(step)
            row["cost_usd"] = round(cost, 6)
            row["rationale"] = rationales[-1] if rationales else ""
            row["label"] = STEP_LABELS.get(str(step.get("step_id") or ""), str(step.get("step_id") or ""))
            enriched.append(row)
        return {**run, "steps": enriched, "timeline": enriched}

    @staticmethod
    def _run_view(row: Any) -> dict[str, Any]:
        keys = row.keys() if hasattr(row, "keys") else []
        return {
            "id": row["id"],
            "shop_id": row["shop_id"] if "shop_id" in keys and row["shop_id"] else DEFAULT_SHOP_ID,
            "flow_id": row["flow_id"],
            "status": row["status"],
            "input": _loads(row["input_json"]),
            "context": _loads(row["context_json"]),
            "current_step": row["current_step"],
            "error": _loads(row["error_json"]) if row["error_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }

    @staticmethod
    def _step_view(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "step_id": row["step_id"],
            "status": row["status"],
            "attempt": int(row["attempt"]),
            "rewrite_round": int(row["rewrite_round"]),
            "input": _loads(row["input_json"]),
            "output": _loads(row["output_json"]),
            "ai_trace": _loads(row["ai_trace_json"], default=[]),
            "error": _loads(row["error_json"]) if row["error_json"] else None,
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
