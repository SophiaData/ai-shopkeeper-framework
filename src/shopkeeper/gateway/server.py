"""海外侧 GatewayServer：长轮询领任务 → 调 PlatformAdapter → 回传结果。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shopkeeper.adapters.base import PlatformAdapter
from shopkeeper.gateway.dispatch import dispatch_platform_call
from shopkeeper.gateway.models import GatewayResult
from shopkeeper.gateway.queue import AuthError, JobQueue


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GatewayServer:
    """海外进程 worker。mock 回环时可与 Client 共进程、共 MemoryJobQueue。"""

    def __init__(
        self,
        *,
        platform: PlatformAdapter,
        queue: JobQueue,
        token: str,
        claim_wait_seconds: float = 2.0,
    ) -> None:
        self.platform = platform
        self.queue = queue
        self.token = (token or "").strip()
        self.claim_wait_seconds = float(claim_wait_seconds)
        self.processed: list[str] = []

    def poll_once(self) -> GatewayResult | None:
        """领最多 1 个任务并执行。无任务返回 None。"""
        try:
            job = self.queue.claim(token=self.token, wait_seconds=self.claim_wait_seconds)
        except AuthError as exc:
            raise AuthError("GATEWAY_AUTH_REJECTED") from exc
        if job is None:
            return None
        adapter_res = dispatch_platform_call(self.platform, job.method, job.args)
        if adapter_res.ok:
            result = GatewayResult(
                job_id=job.id,
                ok=True,
                data=adapter_res.data,
                error=None,
                adapter=adapter_res.adapter or getattr(self.platform, "name", "platform"),
                finished_at=_now(),
            )
        else:
            err = adapter_res.error
            result = GatewayResult(
                job_id=job.id,
                ok=False,
                data=None,
                error=(
                    {
                        "code": err.code,
                        "kind": err.kind.value,
                        "message": err.message,
                        "retryable": err.retryable,
                        "human_todo": err.human_todo,
                        "detail": err.detail or {},
                    }
                    if err
                    else {"code": "GATEWAY_REMOTE_FAILED", "message": "no error body"}
                ),
                adapter=adapter_res.adapter or getattr(self.platform, "name", "platform"),
                finished_at=_now(),
            )
        self.queue.complete(result, token=self.token)
        self.processed.append(job.id)
        return result

    def run_until_idle(self, *, max_jobs: int = 100) -> list[GatewayResult]:
        """连续处理直到队列暂时空（单测用）。"""
        out: list[GatewayResult] = []
        for _ in range(max_jobs):
            # 空队列时 claim 不等待，避免测试卡住
            prev_wait = self.claim_wait_seconds
            self.claim_wait_seconds = 0.0
            try:
                got = self.poll_once()
            finally:
                self.claim_wait_seconds = prev_wait
            if got is None:
                break
            out.append(got)
        return out

    def serve_forever(self) -> None:
        """海外进程主循环：长轮询。"""
        while True:
            self.poll_once()
