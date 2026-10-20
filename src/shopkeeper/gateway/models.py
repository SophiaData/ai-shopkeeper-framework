"""网关任务 / 结果 DTO。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class GatewayJob(BaseModel):
    id: str
    method: str
    args: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    status: str = "queued"  # queued | claimed | done | failed | rejected


class GatewayResult(BaseModel):
    job_id: str
    ok: bool
    data: Any | None = None
    error: dict[str, Any] | None = None
    adapter: str = ""
    finished_at: str = ""
