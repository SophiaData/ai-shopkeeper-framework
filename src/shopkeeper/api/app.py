"""网关 API（M1 最小面）：贴 1688 链接 → 商品入库 + 选品报告；任务状态与工具调用日志。

DESIGN §5 的端点子集：
  POST /products/from-1688   {url} → 触发选品官
  GET  /tasks/{id}           任务状态与产出（轮询）
  GET  /tasks/{id}/logs      工具调用日志（已落库的 tool_calls）
  GET  /tasks/{id}/logs/stream  SSE：推送同批日志，终态后结束
  GET  /tasks                任务列表（轮询视图）
  POST /products/manual      人工补录（A1 裁决的降级入口）

ProductSource 由 app factory 注入，真实/mock 切换不改本模块。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from shopkeeper.adapters.base import ImageClient, LlmClient, ProductSource
from shopkeeper.adapters.mock import MockImage, MockLLM
from shopkeeper.config import Settings
from shopkeeper.db.models import Task, ToolCall
from shopkeeper.db.repo import load_product_facts
from shopkeeper.models import ProductFacts
from shopkeeper.scout import ScoutPipeline
from shopkeeper.stylist import StylistPipeline

_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class From1688Request(BaseModel):
    url: str = Field(min_length=1)
    #: 目标售价（分）。DESIGN §3.2 要选品官出"差价"，而差价必须对着一个目标价才算得出来。
    #: 不传就是 needs_input —— 不替调用方编一个目标价（那是 §7 之外的自作主张）。
    target_price_cents: int | None = None
    #: 泰国站零售价（泰铢）。只用于类目参考价带，不参与差价公式，也不从成本换算。
    retail_price_thb: Any = None


class ManualRequest(BaseModel):
    source_url: str
    source_item_id: str
    title_raw: str
    price_cents: int
    currency: str = "CNY"
    moq: int | None = None
    main_images: list[str] = Field(default_factory=list)
    detail_images: list[str] = Field(default_factory=list)
    seller: str | None = None
    target_price_cents: int | None = None
    #: 泰国站零售价（泰铢）。只用于类目参考价带。不写入商品成本。
    retail_price_thb: Any = None


class GenerateListingRequest(BaseModel):
    """DESIGN §5: POST /listings/generate {product_id, target_site} → listing 草稿。"""

    product_id: str = Field(min_length=1)
    target_site: str | None = None
    langs: list[str] | None = None


def _task_view(task: Task) -> dict[str, Any]:
    """状态视图。轮询用，字段刻意精简（DESIGN §5 GET /tasks?status=&agent=）。"""
    return {
        "task_id": task.id,
        "agent": task.agent,
        "type": task.type,
        "status": task.status,
        "output": task.output,
        "error": task.error,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }


def _tool_call_view(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "adapter": call.adapter,
        "method": call.method,
        "status": call.status,
        "duration_ms": call.duration_ms,
        "request": call.request,
        "response": call.response,
        "created_at": call.created_at.isoformat() if call.created_at else None,
    }


def _list_tool_calls(session: Session, task_id: str, *, after_id: int = 0) -> list[ToolCall]:
    stmt = (
        select(ToolCall)
        .where(ToolCall.task_id == task_id, ToolCall.id > after_id)
        .order_by(ToolCall.id.asc())
    )
    return list(session.scalars(stmt).all())


def create_app(
    session_factory: Callable[[], Session],
    product_source_factory: Callable[[Session], ProductSource],
    *,
    shop_id: str | None = None,
    site: str = "SEA",
    llm_factory: Callable[[], LlmClient] | None = None,
    image_factory: Callable[[], ImageClient] | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """M1 网关。适配器一律注入，真实/mock 切换不改本模块（DESIGN §2.1 原则3）。

    缺省用 mock（原则4 mock 先行），这样 CI 不打真实网络也能跑完整链路。
    """
    app = FastAPI(title="ai-shopkeeper M1", version="0.1.0")
    settings = settings or Settings.from_env()
    llm_factory = llm_factory or (lambda: MockLLM())
    image_factory = image_factory or (lambda: MockImage())

    @app.post("/products/from-1688")
    def from_1688(req: From1688Request) -> dict[str, Any]:
        session = session_factory()
        try:
            pipe = ScoutPipeline(session, product_source_factory(session), shop_id=shop_id, site=site)
            res = pipe.run(
                req.url,
                target_price_cents=req.target_price_cents,
                retail_price_thb=req.retail_price_thb,
            )
            # 失败不是 HTTP 500：任务已建、原因已落库，调用方轮询 task_id 看详情
            return {"ok": res["status"] == "succeeded", **res}
        finally:
            session.close()

    @app.post("/listings/generate")
    def generate_listing(req: GenerateListingRequest) -> dict[str, Any]:
        """DESIGN §5 的链路第二跳：已入库商品 → 三语言 listing 草稿（纯生成，不发店铺）。

        之前这个端点根本没建，于是"贴链接 → 报告 → 文案"在 API 层是断的：
        /products/* 只能走到入库，出文案只能靠 CLI 内部直调管线（Review P-M5）。
        """
        session = session_factory()
        try:
            facts_row = load_product_facts(session, req.product_id)
            if facts_row is None:
                raise HTTPException(status_code=404, detail=f"商品不存在: {req.product_id}")
            facts = ProductFacts(**facts_row)
            pipe = StylistPipeline(
                session,
                llm_factory(),
                image_factory(),
                settings,
                shop_id=shop_id or "shop_m1",
            )
            res = pipe.run(facts, product_db_id=req.product_id)
            return {"ok": res["status"] == "succeeded", **res}
        finally:
            session.close()

    @app.post("/products/manual")
    def manual(req: ManualRequest) -> dict[str, Any]:
        session = session_factory()
        try:
            pipe = ScoutPipeline(session, product_source_factory(session), shop_id=shop_id, site=site)
            res = pipe.run_manual(req.model_dump())
            return {"ok": res["status"] == "succeeded", **res}
        finally:
            session.close()

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        session = session_factory()
        try:
            task = session.get(Task, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
            return _task_view(task)
        finally:
            session.close()

    @app.get("/tasks/{task_id}/logs")
    def get_task_logs(task_id: str, after_id: int = 0) -> dict[str, Any]:
        """工具调用日志。数据已在 tool_calls 表；本端点只读，不触发平台写。"""
        session = session_factory()
        try:
            task = session.get(Task, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
            calls = _list_tool_calls(session, task_id, after_id=max(0, after_id))
            return {
                "task_id": task_id,
                "task_status": task.status,
                "logs": [_tool_call_view(c) for c in calls],
                "count": len(calls),
            }
        finally:
            session.close()

    @app.get("/tasks/{task_id}/logs/stream")
    def stream_task_logs(task_id: str, after_id: int = 0) -> StreamingResponse:
        """SSE：先推已有日志，再短轮询增量，任务终态后发 done 并关闭。

        同步管线跑完后立刻终态，所以多数请求只推一轮；保留轮询是为了以后异步任务。
        """
        session = session_factory()
        try:
            if session.get(Task, task_id) is None:
                raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        finally:
            session.close()

        def events() -> Iterator[str]:
            cursor = max(0, after_id)
            idle_rounds = 0
            while True:
                session = session_factory()
                try:
                    task = session.get(Task, task_id)
                    if task is None:
                        yield _sse("error", {"detail": f"任务不存在: {task_id}"})
                        yield _sse("done", {"reason": "missing"})
                        return
                    calls = _list_tool_calls(session, task_id, after_id=cursor)
                    for call in calls:
                        cursor = call.id
                        yield _sse("log", _tool_call_view(call))
                    yield _sse("task", _task_view(task))
                    if task.status in _TERMINAL:
                        yield _sse("done", {"reason": "terminal", "status": task.status})
                        return
                    idle_rounds += 1 if not calls else 0
                    if idle_rounds >= 40:
                        yield _sse("done", {"reason": "timeout"})
                        return
                finally:
                    session.close()
                time.sleep(0.25)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/tasks")
    def list_tasks(status: str | None = None, agent: str | None = None, limit: int = 50) -> dict[str, Any]:
        session = session_factory()
        try:
            stmt = select(Task)
            if status:
                stmt = stmt.where(Task.status == status)
            if agent:
                stmt = stmt.where(Task.agent == agent)
            stmt = stmt.order_by(Task.created_at.desc()).limit(min(limit, 200))
            return {"tasks": [_task_view(t) for t in session.scalars(stmt).all()]}
        finally:
            session.close()

    return app


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
