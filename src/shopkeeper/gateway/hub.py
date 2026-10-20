"""可选 HTTPS 队列枢纽（主系统侧可托管）：token + enqueue / claim / result。

海外 GatewayServer 主动长轮询 claim，入站端口开在枢纽侧（简化版「海外不暴露端口」）。
单测优先用 MemoryJobQueue；本模块提供 HTTP 面供 remote 部署。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from shopkeeper.gateway.auth import extract_bearer
from shopkeeper.gateway.models import GatewayResult
from shopkeeper.gateway.queue import AuthError, MemoryJobQueue


class EnqueueBody(BaseModel):
    method: str
    args: dict[str, Any] = Field(default_factory=dict)


class ResultBody(BaseModel):
    ok: bool
    data: Any | None = None
    error: dict[str, Any] | None = None
    adapter: str = ""


def create_gateway_hub(
    queue: MemoryJobQueue,
    *,
    token: str,
    claim_wait_seconds: float = 25.0,
) -> FastAPI:
    """token 必须非空；与队列共用同一密钥。"""
    expected = (token or "").strip()
    if not expected:
        raise ValueError("create_gateway_hub 需要非空 token")

    app = FastAPI(title="shopkeeper-platform-gateway-hub", version="0.1.0")

    def _token(authorization: str | None, x_gateway_token: str | None) -> str:
        return extract_bearer(authorization) or (x_gateway_token or "").strip()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "role": "gateway-hub"}

    @app.post("/gateway/v1/jobs")
    def enqueue(
        body: EnqueueBody,
        authorization: str | None = Header(default=None),
        x_gateway_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            job = queue.enqueue(
                body.method, body.args, token=_token(authorization, x_gateway_token)
            )
        except AuthError as exc:
            raise HTTPException(status_code=401, detail="GATEWAY_AUTH_REJECTED") from exc
        return {"job": job.model_dump()}

    @app.get("/gateway/v1/jobs/claim")
    def claim(
        wait: float = 0.0,
        authorization: str | None = Header(default=None),
        x_gateway_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        wait_s = claim_wait_seconds if wait <= 0 else min(float(wait), 60.0)
        try:
            job = queue.claim(token=_token(authorization, x_gateway_token), wait_seconds=wait_s)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail="GATEWAY_AUTH_REJECTED") from exc
        return {"job": None if job is None else job.model_dump()}

    @app.post("/gateway/v1/jobs/{job_id}/result")
    def complete(
        job_id: str,
        body: ResultBody,
        authorization: str | None = Header(default=None),
        x_gateway_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        result = GatewayResult(
            job_id=job_id,
            ok=body.ok,
            data=body.data,
            error=body.error,
            adapter=body.adapter,
        )
        try:
            queue.complete(result, token=_token(authorization, x_gateway_token))
        except AuthError as exc:
            raise HTTPException(status_code=401, detail="GATEWAY_AUTH_REJECTED") from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        return {"ok": True, "job_id": job_id}

    @app.get("/gateway/v1/jobs/{job_id}")
    def job_status(
        job_id: str,
        authorization: str | None = Header(default=None),
        x_gateway_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        tok = _token(authorization, x_gateway_token)
        try:
            job = queue.get_job(job_id, token=tok)
            result = queue.get_result(job_id, token=tok)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail="GATEWAY_AUTH_REJECTED") from exc
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {
            "job": job.model_dump(),
            "result": None if result is None else result.model_dump(),
        }

    return app
