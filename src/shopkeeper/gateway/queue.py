"""任务队列：内存实现（mock 回环）+ 可选 HTTP 客户端侧适配。

海外侧不暴露入站端口时，由 GatewayServer 主动 claim（长轮询简化版）。
"""

from __future__ import annotations

import threading
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from shopkeeper.gateway.auth import check_gateway_token
from shopkeeper.gateway.models import GatewayJob, GatewayResult


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobQueue(ABC):
    @abstractmethod
    def enqueue(self, method: str, args: dict[str, Any], *, token: str) -> GatewayJob: ...

    @abstractmethod
    def claim(self, *, token: str, wait_seconds: float = 0.0) -> GatewayJob | None: ...

    @abstractmethod
    def complete(self, result: GatewayResult, *, token: str) -> None: ...

    @abstractmethod
    def get_result(self, job_id: str, *, token: str) -> GatewayResult | None: ...

    @abstractmethod
    def get_job(self, job_id: str, *, token: str) -> GatewayJob | None: ...


class AuthError(Exception):
    """token 拒绝。"""


class MemoryJobQueue(JobQueue):
    """进程内队列：单测与 GATEWAY_MODE=remote 的本地回环共用。"""

    def __init__(self, *, token: str) -> None:
        self._token = (token or "").strip()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending: list[GatewayJob] = []
        self._jobs: dict[str, GatewayJob] = {}
        self._results: dict[str, GatewayResult] = {}

    def _require(self, token: str) -> None:
        if not check_gateway_token(expected=self._token, provided=token):
            raise AuthError("GATEWAY_AUTH_REJECTED")

    def enqueue(self, method: str, args: dict[str, Any], *, token: str) -> GatewayJob:
        self._require(token)
        job = GatewayJob(
            id=uuid.uuid4().hex,
            method=str(method),
            args=dict(args or {}),
            created_at=_now(),
            status="queued",
        )
        with self._cv:
            self._jobs[job.id] = job
            self._pending.append(job)
            self._cv.notify()
        return job

    def claim(self, *, token: str, wait_seconds: float = 0.0) -> GatewayJob | None:
        self._require(token)
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        with self._cv:
            while True:
                if self._pending:
                    job = self._pending.pop(0)
                    job.status = "claimed"
                    self._jobs[job.id] = job
                    return job.model_copy(deep=True)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=min(remaining, 0.05))

    def complete(self, result: GatewayResult, *, token: str) -> None:
        self._require(token)
        with self._cv:
            job = self._jobs.get(result.job_id)
            if job is None:
                raise KeyError(result.job_id)
            job.status = "done" if result.ok else "failed"
            finished = result.model_copy(update={"finished_at": result.finished_at or _now()})
            self._results[result.job_id] = finished
            self._jobs[job.id] = job
            self._cv.notify_all()

    def get_result(self, job_id: str, *, token: str) -> GatewayResult | None:
        self._require(token)
        with self._lock:
            r = self._results.get(job_id)
            return r.model_copy(deep=True) if r else None

    def get_job(self, job_id: str, *, token: str) -> GatewayJob | None:
        self._require(token)
        with self._lock:
            j = self._jobs.get(job_id)
            return j.model_copy(deep=True) if j else None

    def wait_result(
        self,
        job_id: str,
        *,
        token: str,
        timeout_seconds: float = 10.0,
    ) -> GatewayResult | None:
        self._require(token)
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._cv:
            while True:
                if job_id in self._results:
                    return self._results[job_id].model_copy(deep=True)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=min(remaining, 0.05))
