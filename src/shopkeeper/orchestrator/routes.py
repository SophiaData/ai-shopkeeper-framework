"""编排器 REST API：启动 / 列表 / 详情 / 重试 / 取消 / 批准续跑。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from shopkeeper.orchestrator.definition import PIPELINE_V1
from shopkeeper.orchestrator.engine import Orchestrator, OrchestratorError
from shopkeeper.workflow.auth import actor_label
from shopkeeper.workflow.auth_middleware import get_auth_context
from shopkeeper.workflow.shops import ShopError, require_shop_id


class StartRunRequest(BaseModel):
    url: str = Field(min_length=1, description="1688 商品链接（mock 夹具即可）")
    flow_id: str = PIPELINE_V1.id
    target_price_cents: int | None = None
    force_qa_fail: bool = False
    force_tts_write_blocked: bool = False
    shop_id: str | None = None


class NoteRequest(BaseModel):
    note: str = ""


def _orch_shop_id(shop_id: str | None, *, required: bool = False) -> str | None:
    if shop_id is None or str(shop_id).strip() == "":
        if required:
            try:
                return require_shop_id(None)
            except ShopError as exc:
                raise HTTPException(
                    status_code=400, detail={"code": exc.code, "message": exc.message}
                ) from exc
        return None
    try:
        return require_shop_id(shop_id)
    except ShopError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc


def mount_orchestrator(app: FastAPI, orch: Orchestrator) -> None:
    @app.post("/api/orchestrator/runs")
    def start_run(req: StartRunRequest) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if req.force_qa_fail:
            extra["force_qa_fail"] = True
        if req.force_tts_write_blocked:
            extra["force_tts_write_blocked"] = True
        sid = _orch_shop_id(req.shop_id, required=True)
        try:
            return orch.start(
                url=req.url,
                flow_id=req.flow_id,
                target_price_cents=req.target_price_cents,
                extra_input=extra or None,
                shop_id=sid,
            )
        except OrchestratorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/orchestrator/runs")
    def list_runs(
        status: str | None = None,
        limit: int = 50,
        shop_id: str | None = None,
    ) -> dict[str, Any]:
        sid = _orch_shop_id(shop_id)
        return {"runs": orch.list_runs(status=status, limit=limit, shop_id=sid), "shop_id": sid}

    @app.get("/api/orchestrator/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        detail = orch.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run 不存在")
        return detail

    @app.post("/api/orchestrator/runs/{run_id}/advance")
    def advance_run(run_id: str) -> dict[str, Any]:
        try:
            return orch.advance(run_id)
        except OrchestratorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/orchestrator/runs/{run_id}/resume")
    def resume_run(run_id: str) -> dict[str, Any]:
        try:
            return orch.resume(run_id)
        except OrchestratorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/orchestrator/runs/{run_id}/approve")
    def approve_run(run_id: str, request: Request, req: NoteRequest | None = None) -> dict[str, Any]:
        decided_by = actor_label(get_auth_context(request))
        try:
            return orch.approve(run_id, note=(req.note if req else ""), decided_by=decided_by)
        except OrchestratorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/orchestrator/runs/{run_id}/reject")
    def reject_run(run_id: str, request: Request, req: NoteRequest | None = None) -> dict[str, Any]:
        note = (req.note if req else "") or ""
        if not note.strip():
            raise HTTPException(status_code=400, detail="打回必须填写原因。")
        decided_by = actor_label(get_auth_context(request))
        try:
            return orch.reject(run_id, note=note, decided_by=decided_by)
        except OrchestratorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/orchestrator/runs/{run_id}/steps/{step_id}/retry")
    def retry_step(run_id: str, step_id: str) -> dict[str, Any]:
        try:
            return orch.retry_step(run_id, step_id)
        except OrchestratorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/orchestrator/runs/{run_id}/cancel")
    def cancel_run(run_id: str, req: NoteRequest | None = None) -> dict[str, Any]:
        try:
            return orch.cancel(run_id, note=(req.note if req else ""))
        except OrchestratorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/orchestrator/flows")
    def list_flows() -> dict[str, Any]:
        from shopkeeper.orchestrator.definition import FLOWS

        return {
            "flows": [
                {
                    "id": f.id,
                    "version": f.version,
                    "qa_max_rewrite_rounds": f.qa_max_rewrite_rounds,
                    "steps": [
                        {
                            "id": s.id,
                            "depends_on": list(s.depends_on),
                            "requires_approval": s.requires_approval,
                            "kind": s.kind,
                            "description": s.description,
                        }
                        for s in f.steps
                    ],
                }
                for f in FLOWS.values()
            ]
        }
